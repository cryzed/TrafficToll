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
from .tc import MAX_RATE

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

    # TODO: Parse download rate and raise ConfigError appropriately
    config_global_download_rate = config.get("download")
    config_global_upload_rate = config.get("upload")

    # TODO: Add option to determine max download/upload rate?
    if config_global_download_rate is None:
        logger.info(
            "No global download rate specified, download traffic prioritization won't work"
        )
        global_download_rate = MAX_RATE
    else:
        global_download_rate = config_global_download_rate

    if config_global_upload_rate is None:
        logger.info(
            "No global upload rate specified, upload traffic prioritization won't work"
        )
        global_upload_rate = MAX_RATE
    else:
        global_upload_rate = config_global_upload_rate

    # Determine the priority we want the global default classes to have: this is n+1
    # where n is the lowest defined (=highest integer) priority for any processes in the
    # configuration file. Processes that do not explicitly specify a priority will use
    # this default priority and therefore have the same priority as the global default
    # classes
    lowest_priority = -1
    for name, process in (config.get("processes", {}) or {}).items():
        lowest_priority = max(process.get("upload-priority", -1), lowest_priority)
        lowest_priority = max(process.get("download-priority", -1), lowest_priority)

    lowest_priority += 1

    if config_global_download_rate is not None:
        logger.info(
            "Setting up global class with max download rate: {} and priority: {}",
            global_download_rate,
            lowest_priority,
        )
    else:
        logger.info(
            "Setting up global class with unlimited download rate and priority: {}",
            lowest_priority,
        )
    if config_global_upload_rate is not None:
        logger.info(
            "Setting up global class with max upload rate: {} and priority: {}",
            global_upload_rate,
            lowest_priority,
        )
    else:
        logger.info(
            "Setting up global class with unlimited upload rate and priority: {}",
            lowest_priority,
        )

    ingress, egress = tc_setup(
        arguments.device, global_download_rate, global_upload_rate, lowest_priority,
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
                "No conditions for: {!r} specified, it will never be matched", name
            )
            continue

        predicate = ProcessFilterPredicate(
            name, conditions, process.get("recursive", False)
        )
        process_filter_predicates.append(predicate)

        # Set up classes for download/upload limiting
        config_download_rate = process.get("download")
        config_download_priority = process.get("download-priority")
        download_rate = (
            global_download_rate
            if config_download_rate is None
            else config_download_rate
        )
        download_priority = (
            lowest_priority
            if config_download_priority is None
            else config_download_priority
        )

        config_upload_rate = process.get("upload")
        config_upload_priority = process.get("upload-priority")
        upload_rate = (
            global_upload_rate if config_upload_rate is None else config_upload_rate
        )
        upload_priority = (
            lowest_priority
            if config_upload_priority is None
            else config_upload_priority
        )

        if config_download_rate is not None:
            logger.info(
                "Setting up class for: {!r} with max download rate: {} and priority: {}",
                name,
                download_rate,
                download_priority,
            )
            egress_class_id = tc_add_htb_class(
                ingress_interface,
                ingress_qdisc_id,
                ingress_root_class_id,
                download_rate,
                download_priority,
            )
            class_ids[_TrafficType.Ingress][name] = egress_class_id
        elif config_download_priority is not None:
            logger.info(
                "Setting up class for: {!r} with unlimited download rate and priority: {}",
                name,
                download_priority,
            )
            egress_class_id = tc_add_htb_class(
                ingress_interface,
                ingress_qdisc_id,
                ingress_root_class_id,
                download_rate,
                download_priority,
            )
            class_ids[_TrafficType.Ingress][name] = egress_class_id

        if config_upload_rate is not None:
            logger.info(
                "Setting up class for: {!r} with max upload rate: {} and priority: {}",
                name,
                upload_rate,
                upload_priority,
            )
            ingress_class_id = tc_add_htb_class(
                egress_interface,
                egress_qdisc_id,
                egress_root_class_id,
                upload_rate,
                upload_priority,
            )
            class_ids[_TrafficType.Egress][name] = ingress_class_id
        elif config_upload_priority is not None:
            logger.info(
                "Setting up class for: {!r} with unlimited upload rate and priority: {}",
                name,
                upload_priority,
            )
            ingress_class_id = tc_add_htb_class(
                egress_interface,
                egress_qdisc_id,
                egress_root_class_id,
                upload_rate,
                upload_priority,
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
                    "Shaping traffic for {!r} on local ports {}",
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
