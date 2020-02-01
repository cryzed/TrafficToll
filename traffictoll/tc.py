import atexit
import collections
import re
import subprocess
from typing import Iterable, Optional, Tuple, Set, Union

import psutil
from loguru import logger

from .utils import run

MIN_RATE = 8
# "TC store rates as a 32-bit unsigned integer in bps internally, so we can specify a
# max rate of 4294967295 bps" (source: `$ man tc`)
MAX_RATE = 4294967295
IFB_REGEX = re.compile(r"ifb\d+")
FILTER_ID_REGEX = re.compile(r"filter .*? fh ([a-z0-9]+::[a-z0-9]+?)(?:\s|$)")
QDISC_ID_REGEX = re.compile(r"qdisc .+? ([a-z0-9]+?):")
CLASS_ID_REGEX = re.compile(
    r"class .+? (?P<qdisc_id>[a-z0-9]+?):(?P<class_id>[a-z0-9]+)"
)

# This ID seems to be fixed for the ingress QDisc
INGRESS_QDISC_PARENT_ID = "ffff:fff1"

QDisc: Tuple[str, int, int] = collections.namedtuple(
    "QDisc", ["device", "id", "root_class_id"]
)


def _clean_up(
    remove_ifb_device: bool = False, shutdown_ifb_device: Optional[str] = None
) -> None:
    logger.info("Cleaning up IFB device")
    if remove_ifb_device:
        run("rmmod ifb")
    if shutdown_ifb_device:
        run(f"ip link set dev {shutdown_ifb_device} down")


def _activate_device(name: str) -> None:
    run(f"ip link set dev {name} up")


def _create_ifb_device() -> str:
    before = set(psutil.net_if_stats())
    run("modprobe ifb numifbs=1")
    after = set(psutil.net_if_stats())

    # It doesn't matter if the created IFB device is ambiguous, any will do
    name = after.difference(before).pop()
    _activate_device(name)
    return name


def _acquire_ifb_device() -> str:
    devices = psutil.net_if_stats()
    for device_name, device in devices.items():
        if not IFB_REGEX.match(device_name):
            continue

        if not device.isup:
            _activate_device(device_name)
            # Deactivate existing IFB device if it wasn't activated
            atexit.register(_clean_up, shutdown_ifb_device=device_name)

        return device_name

    # Clean up IFB device if it was created
    atexit.register(_clean_up, remove_ifb_device=True)
    return _create_ifb_device()


def _find_free_id(ids: Iterable[int]) -> int:
    if not isinstance(ids, set):
        ids = set(ids)

    current = 1
    while current in ids:
        current += 1
    return current


def _get_free_qdisc_id(device: str) -> int:
    process = run(
        f"tc qdisc show dev {device}", stdout=subprocess.PIPE, universal_newlines=True,
    )

    ids = set()
    for line in process.stdout.splitlines():
        match = QDISC_ID_REGEX.match(line)
        if not match:
            logger.warning("Failed to parse line: {!r}", line)
            continue

        id_string = match.group(1)
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


def _get_free_class_id(device: str, qdisc_id: int) -> int:
    process = run(
        f"tc class show dev {device}", stdout=subprocess.PIPE, universal_newlines=True,
    )

    ids = set()
    for line in process.stdout.splitlines():
        match = CLASS_ID_REGEX.match(line)
        if not match:
            logger.warning("Failed to parse line: {!r}", line)
            continue

        groups = match.groupdict()
        if int(groups["qdisc_id"]) == qdisc_id:
            ids.add(int(groups["class_id"]))

    return _find_free_id(ids)


def tc_setup(
    device: str,
    download_rate: Union[int, str] = MAX_RATE,
    download_minimum_rate: Union[int, str] = MIN_RATE,
    upload_rate: Union[int, str] = MAX_RATE,
    upload_minimum_rate: Union[int, str] = MIN_RATE,
    default_priority: int = 0,
) -> Tuple[QDisc, QDisc]:
    # Set up IFB device
    run(f"tc qdisc add dev {device} handle ffff: ingress")
    ifb_device = _acquire_ifb_device()
    run(
        f"tc filter add dev {device} parent ffff: protocol ip u32 match u32 0 0 action "
        f"mirred egress redirect dev {ifb_device}"
    )

    # Create IFB device QDisc and root class limited at download_rate
    ifb_device_qdisc_id = _get_free_qdisc_id(ifb_device)
    run(f"tc qdisc add dev {ifb_device} root handle {ifb_device_qdisc_id}: htb")
    ifb_device_root_class_id = _get_free_class_id(ifb_device, ifb_device_qdisc_id)
    run(
        f"tc class add dev {ifb_device} parent {ifb_device_qdisc_id}: classid "
        f"{ifb_device_qdisc_id}:{ifb_device_root_class_id} htb rate {download_rate}"
    )

    ingress_qdisc = QDisc(ifb_device, ifb_device_qdisc_id, ifb_device_root_class_id)
    # Create default class that all traffic is routed through that doesn't match any
    # other filter
    ifb_default_class_id = tc_add_htb_class(
        ingress_qdisc, download_rate, download_minimum_rate, default_priority,
    )
    run(
        f"tc filter add dev {ifb_device} parent {ifb_device_qdisc_id}: prio 2 protocol "
        f"ip u32 match u32 0 0 flowid {ifb_device_qdisc_id}:{ifb_default_class_id}"
    )

    # Create device QDisc and root class limited at upload_rate
    device_qdisc_id = _get_free_qdisc_id(device)
    run(f"tc qdisc add dev {device} root handle {device_qdisc_id}: htb")
    device_root_class_id = _get_free_class_id(device, device_qdisc_id)
    run(
        f"tc class add dev {device} parent {device_qdisc_id}: classid "
        f"{device_qdisc_id}:{device_root_class_id} htb rate {upload_rate}"
    )
    egress_qdisc = QDisc(device, device_qdisc_id, device_root_class_id)

    # Create default class that all traffic is routed through that doesn't match any
    # other filter
    device_default_class_id = tc_add_htb_class(
        egress_qdisc, upload_rate, upload_minimum_rate, default_priority,
    )
    run(
        f"tc filter add dev {device} parent {device_qdisc_id}: prio 2 protocol ip u32 "
        f"match u32 0 0 flowid {device_qdisc_id}:{device_default_class_id}"
    )

    return ingress_qdisc, egress_qdisc


def tc_add_htb_class(
    qdisc: QDisc,
    ceil: Union[int, str] = MAX_RATE,
    rate: Union[int, str] = MIN_RATE,
    priority: int = 0,
) -> int:
    class_id = _get_free_class_id(qdisc.device, qdisc.id)
    # rate of 1byte/s is the lowest we can specify. All classes added this way should
    # only be allowed to borrow from the parent class, otherwise it's possible to
    # specify a rate higher than the global rate
    run(
        f"tc class add dev {qdisc.device} parent {qdisc.id}:{qdisc.root_class_id} "
        f"classid {qdisc.id}:{class_id} htb rate {rate} ceil {ceil} prio {priority}"
    )
    return class_id


def _get_filter_ids(device: str) -> Set[str]:
    process = run(
        f"tc filter show dev {device}", stdout=subprocess.PIPE, universal_newlines=True,
    )
    ids = set()
    for line in process.stdout.splitlines():
        match = FILTER_ID_REGEX.match(line)
        if match:
            ids.add(match.group(1))

    return ids


def tc_add_u32_filter(qdisc: QDisc, predicate: str, class_id: int,) -> str:
    before = _get_filter_ids(qdisc.device)
    run(
        f"tc filter add dev {qdisc.device} protocol ip parent {qdisc.id}: prio 1 u32 "
        f"{predicate} flowid {qdisc.id}:{class_id}"
    )
    after = _get_filter_ids(qdisc.device)

    difference = after.difference(before)
    if len(difference) > 1:
        logger.warning("Parsed ambiguous filter handle: {}", difference)
    return difference.pop()


def tc_remove_u32_filter(qdisc: QDisc, filter_id: str) -> None:
    run(
        f"tc filter del dev {qdisc.device} parent {qdisc.id}: handle {filter_id} prio "
        "1 protocol ip u32"
    )


def tc_remove_qdisc(device: str, parent: str = "root") -> None:
    run(f"tc qdisc del dev {device} parent {parent}")
