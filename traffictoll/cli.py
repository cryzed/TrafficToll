import argparse
import atexit
import collections
import enum
import time
from typing import Dict, DefaultDict, Set

from loguru import logger
from ruamel.yaml import YAML

from .net import ProcessFilterPredicate, filter_net_connections
from .tc import (
    INGRESS_QDISC_PARENT_ID,
    tc_add_htb_class,
    tc_add_u32_filter,
    tc_remove_qdisc,
    tc_remove_u32_filter,
    tc_setup,
)

CONFIG_ENCODING = "UTF-8"


class _TrafficType(enum.Enum):
    Ingress = enum.auto()
    Egress = enum.auto()


def get_argument_parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("device")
    argument_parser.add_argument("config")
    argument_parser.add_argument("--delay", "-d", type=float, default=1)
    argument_parser.add_argument(
        "--logging-level",
        "-l",
        choices={"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"},
        default="INFO",
    )
    return argument_parser


def _clean_up(ingress_interface: str, egress_interface: str) -> None:
    logger.info("Cleaning up QDiscs")
    tc_remove_qdisc(ingress_interface)
    tc_remove_qdisc(egress_interface)
    tc_remove_qdisc(egress_interface, INGRESS_QDISC_PARENT_ID)


def main(arguments: argparse.Namespace) -> None:
    with open(arguments.config, "r", encoding=CONFIG_ENCODING) as file:
        config = YAML().load(file)

    # TODO: Parse download rate
    global_download_rate = config.get("download")
    global_upload_rate = config.get("upload")
    if global_download_rate:
        logger.info(
            "Setting up global download limiter with max rate {}", global_download_rate
        )
    if global_upload_rate:
        logger.info(
            "Setting up global upload limiter with max rate {}", global_upload_rate
        )

    ingress, egress = tc_setup(
        arguments.device, global_download_rate, global_upload_rate
    )
    ingress_interface, ingress_qdisc_id, ingress_root_class_id = ingress
    egress_interface, egress_qdisc_id, egress_root_class_id = egress

    atexit.register(_clean_up, ingress_interface, egress_interface)

    process_filter_predicates = []
    class_ids: Dict[_TrafficType, Dict[str, int]] = {
        _TrafficType.Ingress: {},
        _TrafficType.Egress: {},
    }
    for name, process in (config.get("processes", {}) or {}).items():
        # Prepare process filter predicates to match network connections
        conditions = [list(match.items())[0] for match in process.get("match", [])]
        if not conditions:
            logger.warning(
                "No conditions for {!r} specified, it will never be matched", name
            )

        predicate = ProcessFilterPredicate(name, conditions)
        process_filter_predicates.append(predicate)

        # Set up classes for download/upload limiting
        download_rate = process.get("download")
        upload_rate = process.get("upload")
        if download_rate:
            logger.info(
                "Setting up download limiter for {!r} with max rate {}",
                name,
                download_rate,
            )
            egress_class_id = tc_add_htb_class(
                ingress_interface,
                ingress_qdisc_id,
                ingress_root_class_id,
                download_rate,
            )
            class_ids[_TrafficType.Ingress][name] = egress_class_id
        if upload_rate:
            logger.info(
                "Setting up upload limiter for {!r} with max rate {}", name, upload_rate
            )
            ingress_class_id = tc_add_htb_class(
                egress_interface, egress_qdisc_id, egress_root_class_id, upload_rate
            )
            class_ids[_TrafficType.Egress][name] = ingress_class_id

    port_to_filter_id: Dict[_TrafficType, Dict[int, str]] = {
        _TrafficType.Ingress: {},
        _TrafficType.Egress: {},
    }

    def add_ingress_filter(port: int, class_id: int) -> None:
        filter_id = tc_add_u32_filter(
            ingress_interface,
            f"match ip dport {port} 0xffff",
            ingress_qdisc_id,
            class_id,
        )
        port_to_filter_id[_TrafficType.Ingress][port] = filter_id

    def add_egress_filter(port: int, class_id: int) -> None:
        filter_id = tc_add_u32_filter(
            egress_interface, f"match ip sport {port} 0xffff", egress_qdisc_id, class_id
        )
        port_to_filter_id[_TrafficType.Egress][port] = filter_id

    def remove_filters(port: int) -> None:
        ingress_filter_id = port_to_filter_id[_TrafficType.Ingress].get(port)
        if ingress_filter_id:
            tc_remove_u32_filter(ingress_interface, ingress_filter_id, ingress_qdisc_id)
            del port_to_filter_id[_TrafficType.Ingress][port]

        egress_filter_id = port_to_filter_id[_TrafficType.Egress].get(port)
        if egress_filter_id:
            tc_remove_u32_filter(egress_interface, egress_filter_id, egress_qdisc_id)
            del port_to_filter_id[_TrafficType.Egress][port]

    filtered_ports: DefaultDict[str, Set[int]] = collections.defaultdict(set)
    while True:
        filtered_connections = filter_net_connections(process_filter_predicates)
        for name, connections in filtered_connections.items():
            ports = set(connection.laddr.port for connection in connections)
            ingress_class_id = class_ids[_TrafficType.Ingress].get(name)
            egress_class_id = class_ids[_TrafficType.Egress].get(name)

            # Add new port filters
            new_ports = sorted(ports.difference(filtered_ports[name]))
            if new_ports:
                logger.info(
                    "Filtering traffic for {!r} on local ports {}",
                    name,
                    ", ".join(map(str, new_ports)),
                )
                for port in new_ports:
                    if ingress_class_id:
                        add_ingress_filter(port, ingress_class_id)
                    if egress_class_id:
                        add_egress_filter(port, egress_class_id)

            # Remove old port filters
            freed_ports = sorted(filtered_ports[name].difference(ports))
            if freed_ports:
                logger.info(
                    "Removing filters for {!r} on local ports {}",
                    name,
                    ", ".join(map(str, freed_ports)),
                )
                for port in freed_ports:
                    remove_filters(port)

            filtered_ports[name] = ports

        # Remove freed ports for unmatched processes (process died or predicate
        # conditions stopped matching)
        for name in set(filtered_ports).difference(filtered_connections):
            freed_ports = sorted(filtered_ports[name])
            if freed_ports:
                logger.info(
                    "Removing filters for {!r} on local ports {}",
                    name,
                    ", ".join(map(str, freed_ports)),
                )
                for port in freed_ports:
                    remove_filters(port)
            del filtered_ports[name]

        time.sleep(arguments.delay)
