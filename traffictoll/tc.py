import atexit
import re
import subprocess

import psutil
from loguru import logger

from traffictoll.utils import _run

# "TC store rates as a 32-bit unsigned integer in bps internally, so we can specify a max rate of 4294967295 bps"
# (source: `$ man tc`)
MAX_RATE = 4294967295
IFB_REGEX = r'ifb\d+'
FILTER_ID_REGEX = r'filter .*? fh ([a-z0-9]+::[a-z0-9]+?)(?:\s|$)'
QDISC_ID_REGEX = r'qdisc .+? ([a-z0-9]+?):'
CLASS_ID_REGEX = r'class .+? (?P<qdisc_id>[a-z0-9]+?):(?P<class_id>[a-z0-9]+)'

# This ID seems to be fixed for the ingress QDisc
INGRESS_QDISC_PARENT_ID = 'ffff:fff1'


def _clean_up():
    logger.info('Cleaning up IFB devices')
    _run('rmmod ifb')


def _activate_interface(name):
    _run(f'ip link set dev {name} up')


def _create_ifb_device():
    before = set(psutil.net_if_stats())
    _run('modprobe ifb numifbs=1')
    after = set(psutil.net_if_stats())

    name = after.difference(before).pop()
    _activate_interface(name)
    return name


# TODO: Also shut down interface if it was down?
def _acquire_ifb_device():
    interfaces = psutil.net_if_stats()
    for interface_name, interface in interfaces.items():
        if not re.match(IFB_REGEX, interface_name):
            continue
        if not interface.isup:
            _activate_interface(interface_name)

        return interface_name

    # Clean up IFB devices if they were created
    atexit.register(_clean_up)
    return _create_ifb_device()


def _find_free_id(ids):
    if not isinstance(ids, set):
        ids = set(ids)

    current = 1
    while current in ids:
        current += 1
    return current


def _get_free_qdisc_id(interface):
    process = _run(f'tc qdisc show dev {interface}', stdout=subprocess.PIPE, universal_newlines=True)

    ids = set()
    for line in process.stdout.splitlines():
        id_string = re.match(QDISC_ID_REGEX, line).group(1)
        try:
            id_ = int(id_string)
        except ValueError:
            # This should only happen for the ingress QDisc
            logger.warning('Failed to parse QDisc ID as base 10 integer on line: {!r}', line)
            id_ = int(id_string, 16)

        ids.add(id_)

    return _find_free_id(ids)


def _get_free_class_id(interface, qdisc_id):
    process = _run(f'tc class show dev {interface}', stdout=subprocess.PIPE, universal_newlines=True)

    ids = set()
    for line in process.stdout.splitlines():
        match = re.match(CLASS_ID_REGEX, line).groupdict()
        if int(match['qdisc_id']) == qdisc_id:
            ids.add(int(match['class_id']))

    return _find_free_id(ids)


def tc_setup(interface, download_rate=None, upload_rate=None):
    download_rate = download_rate or MAX_RATE
    upload_rate = upload_rate or MAX_RATE

    _run(f'tc qdisc add dev {interface} handle ffff: ingress')

    ifb_device = _acquire_ifb_device()
    _run((f'tc filter add dev {interface} parent ffff: protocol ip u32 match u32 0 0 action mirred egress'
          f' redirect dev {ifb_device}'))

    ifb_device_qdisc_id = _get_free_qdisc_id(interface)
    _run(f'tc qdisc add dev {ifb_device} root handle {ifb_device_qdisc_id}: htb')

    ifb_root_class_id = _get_free_class_id(interface, ifb_device_qdisc_id)
    _run((f'tc class add dev {ifb_device} parent {ifb_device_qdisc_id}: classid '
          f'{ifb_device_qdisc_id}:{ifb_root_class_id} htb rate {download_rate}'))

    interface_qdisc_id = _get_free_qdisc_id(interface)
    _run(f'tc qdisc add dev {interface} root handle {interface_qdisc_id}: htb')

    interface_root_class_id = _get_free_class_id(interface, interface_qdisc_id)
    _run((f'tc class add dev {interface} parent {interface_qdisc_id}: classid '
          f'{interface_qdisc_id}:{interface_root_class_id} htb rate {upload_rate}'))

    return (
        (ifb_device, ifb_device_qdisc_id, ifb_root_class_id),
        (interface, interface_qdisc_id, interface_root_class_id))


def tc_add_class(interface, parent_qdisc_id, parent_class_id, rate):
    class_id = _get_free_class_id(interface, parent_qdisc_id)
    _run((f'tc class add dev {interface} parent {parent_qdisc_id}:{parent_class_id} classid '
          f'{parent_qdisc_id}:{class_id} htb rate {rate}'))
    return class_id


def _get_filter_ids(interface):
    process = _run(f'tc filter show dev {interface}', stdout=subprocess.PIPE, universal_newlines=True)
    handles = []
    for line in process.stdout.splitlines():
        match = re.match(FILTER_ID_REGEX, line)
        if match:
            handles.append(match.group(1))

    return handles


def tc_add_filter(interface, predicate, parent_qdisc_id, class_id):
    before = set(_get_filter_ids(interface))
    _run((f'tc filter add dev {interface} protocol ip parent {parent_qdisc_id}: prio 1 u32 {predicate} flowid '
          f'{parent_qdisc_id}:{class_id}'))
    after = set(_get_filter_ids(interface))

    difference = after.difference(before)
    if len(difference) > 1:
        logger.warning('Parsed ambiguous filter handle: {}', difference)
    return difference.pop()


def tc_remove_filter(interface, filter_id, parent_qdisc_id):
    _run(f'tc filter del dev {interface} parent {parent_qdisc_id}: handle {filter_id} prio 1 protocol ip u32')


def tc_remove_qdisc(interface, parent='root'):
    _run(f'tc qdisc del dev {interface} parent {parent}')
