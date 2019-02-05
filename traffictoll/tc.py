import os
import re
import shlex
import shutil
import subprocess
import tempfile

import psutil
from loguru import logger

ENCODING = 'UTF-8'
MODPROBE_PATH = shutil.which('modprobe')
IP_PATH = shutil.which('ip')
TC_PATH = shutil.which('tc')
IFB_REGEX = r'ifb\d+'
FILTER_ID_REGEX = 'filter .*? fh ([a-z0-9]+::[a-z0-9]+)'
QDISC_ID_REGEX = 'qdisc .+? ([a-z0-9]+):'
CLASS_ID_REGEX = 'class .+? (?P<qdisc_id>[a-z0-9]+):(?P<class_id>[a-z0-9]+)'

# "TC store rates as a 32-bit unsigned integer in bps internally, so we can specify a max rate of 4294967295 bps"
# (source: `$ man tc`)
MAX_RATE = 4294967295

# TODO: Setup egress qdisc/root class
# TODO: Is the ffff handle something special, or can we just generate our own?
TC_SETUP = '''
# Configure IFB device for ingress shaping: World -> IFB -> QDisc (egress shaping) -> Interface
qdisc add dev {interface} handle ffff: ingress
filter add dev {interface} parent ffff: protocol ip u32 match u32 0 0 action mirred egress redirect dev {ifb_device}

qdisc add dev {ifb_device} root handle {ifb_device_qdisc_id}: htb
class add dev {ifb_device} parent {ifb_device_qdisc_id}: classid {ifb_device_qdisc_id}:{ifb_root_class_id} htb rate {download_rate}
'''


def _activate_interface(name):
    command = [IP_PATH, 'link', 'set', 'dev', name, 'up']
    logger.debug(' '.join(command))
    subprocess.run(command)


def _create_ifb_device():
    before = set(psutil.net_if_stats())

    command = [MODPROBE_PATH, 'ifb', 'numifbs=1']
    logger.debug(' '.join(command))
    subprocess.run([MODPROBE_PATH, 'ifb', 'numifbs=1'])

    after = set(psutil.net_if_stats())
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

        return interface_name
    return _create_ifb_device()


def _find_free_id(ids):
    if not isinstance(ids, set):
        ids = set(ids)

    current = 1
    while current in ids:
        current += 1
    return current


def _get_free_qdisc_id(interface):
    command = [TC_PATH, 'qdisc', 'show', 'dev', interface]
    logger.debug(' '.join(command))
    process = subprocess.run(command, stdout=subprocess.PIPE, universal_newlines=True)

    ids = set()
    for line in process.stdout.splitlines():
        match = re.match(QDISC_ID_REGEX, line)
        if match:
            id_ = int(match.group(1))
            ids.add(id_)

    return _find_free_id(ids)


def _get_free_class_id(interface, qdisc_id):
    command = [TC_PATH, 'class', 'show', 'dev', interface]
    logger.debug(' '.join(command))
    process = subprocess.run(command, stdout=subprocess.PIPE, universal_newlines=True)

    ids = set()
    for line in process.stdout.splitlines():
        match = re.match(CLASS_ID_REGEX, line).groupdict()
        if int(match['qdisc_id']) == qdisc_id:
            ids.add(int(match['class_id']))

    return _find_free_id(ids)


def tc_setup(interface, download_rate=None, upload_rate=None):
    download_rate = download_rate or MAX_RATE
    upload_rate = upload_rate or MAX_RATE

    ifb_device = _acquire_ifb_device()
    ifb_device_qdisc_id = _get_free_qdisc_id(interface)
    ifb_root_class_id = _get_free_class_id(interface, ifb_device_qdisc_id)
    instructions = TC_SETUP.format(
        interface=interface,
        ifb_device=ifb_device,
        ifb_device_qdisc_id=ifb_device_qdisc_id,
        ifb_root_class_id=ifb_root_class_id,
        download_rate=download_rate).strip()

    fd, path = tempfile.mkstemp()
    # noinspection PyArgumentList
    with os.fdopen(fd, 'w', encoding=ENCODING) as file:
        file.write(instructions)

    logger.debug('{}:\n{}', path, instructions)
    command = [TC_PATH, '-batch', path]
    logger.debug(' '.join(command))
    subprocess.run(command)
    return (ifb_device, ifb_device_qdisc_id, ifb_root_class_id), (interface, None, None)


def tc_add_class(interface, parent_qdisc_id, parent_class_id, rate):
    class_id = _get_free_class_id(interface, parent_qdisc_id)
    command = [TC_PATH, 'class', 'add', 'dev', interface, 'parent', f'{parent_qdisc_id}:{parent_class_id}', 'classid',
               f'{parent_qdisc_id}:{class_id}', 'htb', 'rate', rate]
    logger.debug(' '.join(command))
    subprocess.run(command)
    return class_id


def _parse_filter_handles(interface):
    command = [TC_PATH, 'filter', 'show', 'dev', interface]
    logger.debug(' '.join(command))
    process = subprocess.run(command, stdout=subprocess.PIPE, universal_newlines=True)

    handles = []
    for line in process.stdout.splitlines():
        match = re.match(FILTER_ID_REGEX, line)
        if match:
            handles.append(match.group(1))

    return handles


def tc_add_filter(interface, predicate, parent_qdisc_id, class_id):
    command = [TC_PATH, 'filter', 'add', 'dev', interface, 'protocol', 'ip', 'parent', f'{parent_qdisc_id}:', 'prio',
               '1', 'u32', *shlex.split(predicate), 'flowid', f'{parent_qdisc_id}:{class_id}']
    logger.debug(' '.join(command))

    before = set(_parse_filter_handles(interface))
    subprocess.run(command)
    after = set(_parse_filter_handles(interface))

    difference = after.difference(before)
    if len(difference) > 1:
        logger.warning('Parsed ambiguous filter handle: {}', difference)
    return difference.pop()


def tc_remove_filter(interface, filter_id, parent_qdisc_id):
    command = [TC_PATH, 'filter', 'del', 'dev', interface, 'parent', f'{parent_qdisc_id}:', 'handle', filter_id, 'prio',
               '1', 'protocol', 'ip', 'u32']
    logger.debug(' '.join(command))
    subprocess.run(command)
