import argparse
import atexit
import collections
import time

from loguru import logger
from ruamel.yaml import YAML

from traffictoll.net import ProcessFilterPredicate, filter_net_connections
from traffictoll.tc import INGRESS_QDISC_PARENT_ID, tc_add_class, tc_add_filter, tc_remove_filter, tc_remove_qdisc, \
    tc_setup

CONFIG_ENCODING = 'UTF-8'
argument_parser = argparse.ArgumentParser()
argument_parser.add_argument('device')
argument_parser.add_argument('config')
argument_parser.add_argument('--delay', '-d', type=float, default=1)


def _clean_up(ingress_interface, egress_interface):
    logger.info('Cleaning up QDiscs')
    tc_remove_qdisc(ingress_interface)
    tc_remove_qdisc(egress_interface)
    tc_remove_qdisc(egress_interface, INGRESS_QDISC_PARENT_ID)


def cli_main():
    arguments = argument_parser.parse_args()

    try:
        main(arguments)
    except KeyboardInterrupt:
        logger.info('Aborted')


def main(arguments):
    with open(arguments.config, 'r', encoding=CONFIG_ENCODING) as file:
        config = YAML().load(file)

    download_rate = config.get('download')
    upload_rate = config.get('upload')
    ((ingress_interface, ingress_qdisc_id, ingress_root_class_id),
     (egress_interface, egress_qdisc_id, egress_root_class_id)) = tc_setup(arguments.device, download_rate, upload_rate)

    atexit.register(_clean_up, ingress_interface, egress_interface)

    process_filter_predicates = []
    class_ids = {'ingress': {}, 'egress': {}}
    for name, process in config['processes'].items():
        # Prepare process filter predicates to match network connections
        conditions = [list(match.items())[0] for match in process.get('match', [])]
        predicate = ProcessFilterPredicate(name, conditions)
        process_filter_predicates.append(predicate)

        # Set up classes for download/upload limiting
        download_rate = process.get('download')
        upload_rate = process.get('upload')
        if download_rate:
            egress_class_id = tc_add_class(ingress_interface, ingress_qdisc_id, ingress_root_class_id, download_rate)
            class_ids['ingress'][name] = egress_class_id
        if upload_rate:
            ingress_class_id = tc_add_class(egress_interface, egress_qdisc_id, egress_root_class_id, upload_rate)
            class_ids['egress'][name] = ingress_class_id

    port_to_filter_id = {'ingress': {}, 'egress': {}}

    def add_ingress_filter(port):
        filter_id = tc_add_filter(ingress_interface, f'match ip dport {port} 0xffff', ingress_qdisc_id,
                                  ingress_class_id)
        port_to_filter_id['ingress'][port] = filter_id

    def add_egress_filter(port):
        filter_id = tc_add_filter(egress_interface, f'match ip sport {port} 0xffff', egress_qdisc_id, egress_class_id)
        port_to_filter_id['egress'][port] = filter_id

    def remove_filters(port):
        ingress_filter_id = port_to_filter_id['ingress'][port]
        if ingress_filter_id:
            tc_remove_filter(ingress_interface, ingress_filter_id, ingress_qdisc_id)
            del port_to_filter_id['ingress'][port]

        egress_filter_id = port_to_filter_id['egress'][port]
        if egress_filter_id:
            tc_remove_filter(egress_interface, egress_filter_id, egress_qdisc_id)
            del port_to_filter_id['egress'][port]

    filtered_ports = collections.defaultdict(set)
    while True:
        filtered_connections = filter_net_connections(process_filter_predicates)
        for name, connections in filtered_connections.items():
            ports = set(connection.laddr.port for connection in connections)
            ingress_class_id = class_ids['ingress'].get(name)
            egress_class_id = class_ids['egress'].get(name)

            # Add new port filters
            new_ports = sorted(ports.difference(filtered_ports[name]))
            if new_ports:
                logger.info('Filtering traffic for {!r} on local ports {}', name, ', '.join(map(str, new_ports)))
                for port in new_ports:
                    if ingress_class_id:
                        add_ingress_filter(port)
                    if egress_class_id:
                        add_egress_filter(port)

            # Remove old port filters
            freed_ports = sorted(filtered_ports[name].difference(ports))
            if freed_ports:
                logger.info('Removing filters for {!r} on local ports {}', name, ', '.join(map(str, freed_ports)))
                for port in freed_ports:
                    logger.info('Removing filters for {!r} on local port {}', name, port)
                    remove_filters(port)

            filtered_ports[name] = ports

        # Remove freed ports for unmatched processes (process died or predicate conditions stopped matching)
        for name in set(filtered_ports).difference(filtered_connections):
            freed_ports = sorted(filtered_ports[name])
            if freed_ports:
                logger.info('Removing filters for {!r} on local ports {}', name, ', '.join(map(str, freed_ports)))
                for port in freed_ports:
                    remove_filters(port)
            del filtered_ports[name]

        time.sleep(arguments.delay)
