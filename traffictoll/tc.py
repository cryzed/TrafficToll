import atexit
import re
import subprocess

import psutil
from loguru import logger

from .utils import run

# "TC store rates as a 32-bit unsigned integer in bps internally, so we can specify a
# max rate of 4294967295 bps"
# (source: `$ man tc`)
MAX_RATE = 4294967295
IFB_REGEX = r"ifb\d+"
FILTER_ID_REGEX = r"filter .*? fh ([a-z0-9]+::[a-z0-9]+?)(?:\s|$)"
QDISC_ID_REGEX = r"qdisc .+? ([a-z0-9]+?):"
CLASS_ID_REGEX = r"class .+? (?P<qdisc_id>[a-z0-9]+?):(?P<class_id>[a-z0-9]+)"

# This ID seems to be fixed for the ingress QDisc
INGRESS_QDISC_PARENT_ID = "ffff:fff1"


def _clean_up(remove_ifb_device=False, shutdown_ifb_device=None):
    logger.info("Cleaning up IFB device")
    if remove_ifb_device:
        run("rmmod ifb")
    if shutdown_ifb_device:
        run(f"ip link set dev {shutdown_ifb_device} down")


def _activate_interface(name):
    run(f"ip link set dev {name} up")


def _create_ifb_device():
    before = set(psutil.net_if_stats())
    run("modprobe ifb numifbs=1")
    after = set(psutil.net_if_stats())

    # It doesn't matter if the created IFB device is ambiguous, any will do
    name = after.difference(before).pop()
    _activate_interface(name)
    return name


def _acquire_ifb_device():
    interfaces = psutil.net_if_stats()
    for interface_name, interface in interfaces.items():
        if not re.match(IFB_REGEX, interface_name):
            continue

        if not interface.isup:
            _activate_interface(interface_name)
            # Deactivate existing IFB device if it wasn't activated
            atexit.register(_clean_up, shutdown_ifb_device=interface_name)

        return interface_name

    # Clean up IFB device if it was created
    atexit.register(_clean_up, remove_ifb_device=True)
    return _create_ifb_device()


def _find_free_id(ids):
    if not isinstance(ids, set):
        ids = set(ids)

    current = 1
    while current in ids:
        current += 1
    return current


def _get_free_qdisc_id(interface):
    process = run(
        f"tc qdisc show dev {interface}",
        stdout=subprocess.PIPE,
        universal_newlines=True,
    )

    ids = set()
    for line in process.stdout.splitlines():
        id_string = re.match(QDISC_ID_REGEX, line).group(1)
        try:
            id_ = int(id_string)
        except ValueError:
            # This should only happen for the ingress QDisc
            logger.debug(
                "Failed to parse QDisc ID as base 10 integer on line: {!r}", line
            )
            id_ = int(id_string, 16)

        ids.add(id_)

    return _find_free_id(ids)


def _get_free_class_id(interface, qdisc_id):
    process = run(
        f"tc class show dev {interface}",
        stdout=subprocess.PIPE,
        universal_newlines=True,
    )

    ids = set()
    for line in process.stdout.splitlines():
        match = re.match(CLASS_ID_REGEX, line).groupdict()
        if int(match["qdisc_id"]) == qdisc_id:
            ids.add(int(match["class_id"]))

    return _find_free_id(ids)


def tc_setup(interface, download_rate=None, upload_rate=None):
    download_rate = download_rate or MAX_RATE
    upload_rate = upload_rate or MAX_RATE

    # Set up IFB device
    run(f"tc qdisc add dev {interface} handle ffff: ingress")
    ifb_device = _acquire_ifb_device()
    run(
        f"tc filter add dev {interface} parent ffff: protocol ip u32 match u32 0 0 "
        f"action mirred egress redirect dev {ifb_device}"
    )

    # Create IFB device QDisc and root class limited at download_rate
    ifb_device_qdisc_id = _get_free_qdisc_id(ifb_device)
    run(f"tc qdisc add dev {ifb_device} root handle {ifb_device_qdisc_id}: htb")
    ifb_device_root_class_id = _get_free_class_id(ifb_device, ifb_device_qdisc_id)
    run(
        f"tc class add dev {ifb_device} parent {ifb_device_qdisc_id}: classid "
        f"{ifb_device_qdisc_id}:{ifb_device_root_class_id} htb rate {download_rate}"
    )

    # Create default class that all traffic is routed through that doesn't match any
    # other filter
    ifb_default_class_id = tc_add_htb_class(
        ifb_device, ifb_device_qdisc_id, ifb_device_root_class_id, download_rate
    )
    run(
        f"tc filter add dev {ifb_device} parent {ifb_device_qdisc_id}: prio 2 protocol "
        f"ip u32 match u32 0 0 flowid {ifb_device_qdisc_id}:{ifb_default_class_id}"
    )

    # Create interface QDisc and root class limited at upload_rate
    interface_qdisc_id = _get_free_qdisc_id(interface)
    run(f"tc qdisc add dev {interface} root handle {interface_qdisc_id}: htb")
    interface_root_class_id = _get_free_class_id(interface, interface_qdisc_id)
    run(
        f"tc class add dev {interface} parent {interface_qdisc_id}: classid "
        f"{interface_qdisc_id}:{interface_root_class_id} htb rate {upload_rate}"
    )

    # Create default class that all traffic is routed through that doesn't match any
    # other filter
    interface_default_class_id = tc_add_htb_class(
        interface, interface_qdisc_id, interface_root_class_id, upload_rate
    )
    run(
        f"tc filter add dev {interface} parent {interface_qdisc_id}: prio 2 protocol ip"
        f" u32 match u32 0 0 flowid {interface_qdisc_id}:{interface_default_class_id}"
    )

    return (
        (ifb_device, ifb_device_qdisc_id, ifb_device_root_class_id),
        (interface, interface_qdisc_id, interface_root_class_id),
    )


def tc_add_htb_class(interface, parent_qdisc_id, parent_class_id, rate):
    class_id = _get_free_class_id(interface, parent_qdisc_id)
    # rate of 1byte/s is the lowest we can specify. All classes added this way should
    # only be allowed to borrow from the parent class, otherwise it's possible to
    # specify a rate higher than the global rate
    run(
        f"tc class add dev {interface} parent {parent_qdisc_id}:{parent_class_id} "
        f"classid {parent_qdisc_id}:{class_id} htb rate 8 ceil {rate}"
    )
    return class_id


def _get_filter_ids(interface):
    process = run(
        f"tc filter show dev {interface}",
        stdout=subprocess.PIPE,
        universal_newlines=True,
    )
    ids = set()
    for line in process.stdout.splitlines():
        match = re.match(FILTER_ID_REGEX, line)
        if match:
            ids.add(match.group(1))

    return ids


def tc_add_u32_filter(interface, predicate, parent_qdisc_id, class_id):
    before = _get_filter_ids(interface)
    run(
        f"tc filter add dev {interface} protocol ip parent {parent_qdisc_id}: prio 1 "
        f"u32 {predicate} flowid {parent_qdisc_id}:{class_id}"
    )
    after = _get_filter_ids(interface)

    difference = after.difference(before)
    if len(difference) > 1:
        logger.warning("Parsed ambiguous filter handle: {}", difference)
    return difference.pop()


def tc_remove_u32_filter(interface, filter_id, parent_qdisc_id):
    run(
        f"tc filter del dev {interface} parent {parent_qdisc_id}: handle {filter_id} "
        "prio 1 protocol ip u32"
    )


def tc_remove_qdisc(interface, parent="root"):
    run(f"tc qdisc del dev {interface} parent {parent}")
