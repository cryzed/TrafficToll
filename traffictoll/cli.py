import argparse
import atexit
import collections
import time

from loguru import logger
from ruamel.yaml import YAML

from traffictoll.tc import INGRESS_QDISC_ID, tc_add_class, tc_add_filter, tc_remove_filter, tc_remove_qdisc, tc_setup
from traffictoll.utils import ProcessPredicate, filter_net_connections

ENCODING = 'UTF-8'
argument_parser = argparse.ArgumentParser()
argument_parser.add_argument('device')
argument_parser.add_argument('config')
argument_parser.add_argument('--delay', '-d', type=float, default=1)


def _clean_up(ingress_interface, egress_interface):
    tc_remove_qdisc(ingress_interface)

    # TODO: Do this smarter
    tc_remove_qdisc(egress_interface, INGRESS_QDISC_ID)


def cli_main():
    arguments = argument_parser.parse_args()

    try:
        main(arguments)
    except KeyboardInterrupt:
        logger.info('Aborted.')


def main(arguments):
    with open(arguments.config, 'r', encoding=ENCODING) as file:
        config = YAML().load(file)

    download_rate = config.get('download')
    upload_rate = config.get('upload')
    ((ingress_interface, ingress_qdisc_id, ingress_root_class_id),
     (egress_interface, egress_qdisc_id, egress_root_class_id)) = tc_setup(arguments.device, download_rate, upload_rate)

    atexit.register(_clean_up, ingress_interface, egress_interface)

    process_predicates = []
    class_ids = {}
    for name, process in config['processes'].items():
        predicate = ProcessPredicate(name, [list(match.items())[0] for match in process.get('match', [])])
        process_predicates.append(predicate)

        # Set up classes for the process traffic
        download_rate = process['download']
        class_ids[name] = tc_add_class(ingress_interface, ingress_qdisc_id, ingress_root_class_id, download_rate)

    filtered_ports = collections.defaultdict(set)
    port_to_filter_id = {}
    while True:
        filtered_connections = filter_net_connections(process_predicates)
        for name, connections in filtered_connections.items():
            class_id = class_ids[name]
            ports = set(connection.laddr.port for connection in connections)

            # Add new ports
            new_ports = ports.difference(filtered_ports[name])
            if new_ports:
                logger.info('Filtering traffic for {!r} on local ports {}', name,
                            ', '.join(map(str, sorted(new_ports))))

            for port in new_ports:
                match_predicate = f'match ip dport {port} 0xffff'
                filter_id = tc_add_filter(ingress_interface, match_predicate, ingress_qdisc_id, class_id)
                port_to_filter_id[port] = filter_id

            # Remove old port filters
            freed_ports = filtered_ports[name].difference(ports)
            if freed_ports:
                logger.info('Removing filters for {!r} on local ports {}', name,
                            ', '.join(map(str, sorted(freed_ports))))

            for port in freed_ports:
                filter_id = port_to_filter_id[port]
                tc_remove_filter(ingress_interface, filter_id, ingress_qdisc_id)
                del port_to_filter_id[port]

            filtered_ports[name] = ports

        # Remove freed ports for dead processes
        for name in set(filtered_ports).difference(filtered_connections):
            freed_ports = filtered_ports[name]
            logger.info('Removing filters for {!r} on local ports {}', name, ', '.join(map(str, sorted(freed_ports))))
            for port in freed_ports:
                filter_id = port_to_filter_id[port]
                tc_remove_filter(ingress_interface, filter_id, ingress_qdisc_id)
                del port_to_filter_id[port]

            del filtered_ports[name]

        time.sleep(arguments.delay)
