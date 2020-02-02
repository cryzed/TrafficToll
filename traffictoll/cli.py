import argparse
import atexit
import collections
import enum
import time
from typing import Dict, DefaultDict, Set

from loguru import logger
from ruamel.yaml import YAML

from .exceptions import DependencyOutputError, MissingDependencyError
from .net import ProcessFilterPredicate, filter_net_connections
from .speedtest import test_speed
from .tc import (
    MAX_RATE,
    INGRESS_QDISC_PARENT_ID,
    tc_add_htb_class,
    tc_add_u32_filter,
    tc_remove_qdisc,
    tc_remove_u32_filter,
    tc_setup,
)

CONFIG_ENCODING = "UTF-8"
GLOBAL_MINIMUM_DOWNLOAD_RATE = "100kbps"
GLOBAL_MINIMUM_UPLOAD_RATE = "10kbps"
MINIMUM_DOWNLOAD_RATE = "10kbps"
MINIMUM_UPLOAD_RATE = "1kbps"


class _TrafficType(enum.Enum):
    Ingress = enum.auto()
    Egress = enum.auto()


def get_argument_parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    argument_parser.add_argument(
        "device", help="The network device to be traffic shaped"
    )
    argument_parser.add_argument("config", help="The configuration file")
    argument_parser.add_argument(
        "--delay",
        "-d",
        type=float,
        default=1,
        help="The delay in seconds between checks for changed connections in monitored "
        "processes",
    )
    argument_parser.add_argument(
        "--logging-level",
        "-l",
        choices={"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"},
        default="INFO",
        help="The logging level",
    )
    argument_parser.add_argument(
        "--speed-test",
        "-s",
        action="store_true",
        help="Automatically determine upload and download speed before start. Make sure"
        ' to run "speedtest --accept-license" beforehand if you are using the official'
        ' "Ookla Speedtest CLI".',
    )
    return argument_parser


def _clean_up(ingress_device: str, egress_device: str) -> None:
    logger.info("Cleaning up QDiscs")
    tc_remove_qdisc(ingress_device)
    tc_remove_qdisc(egress_device)
    tc_remove_qdisc(egress_device, INGRESS_QDISC_PARENT_ID)


# TODO: Check configuration and raise ConfigError
def main(arguments: argparse.Namespace) -> None:
    with open(arguments.config, "r", encoding=CONFIG_ENCODING) as file:
        config = YAML().load(file)

    config_global_download_rate = config.get("download")
    config_global_upload_rate = config.get("upload")
    if arguments.speed_test:
        logger.info("Running speed test...")

        try:
            result = test_speed()
        except MissingDependencyError as error:
            logger.error("Missing dependency: {}", error)
            result = None
        except DependencyOutputError as error:
            logger.error("Dependency output error: {}", error)
            result = None

        if result:
            logger.info(
                "Determined download speed: {}bps, upload speed: {}bps", *result
            )
            config_global_download_rate, config_global_upload_rate = result
        else:
            logger.error(
                "Failed to automatically determine download and upload speed, falling "
                "back to configuration values"
            )

    if config_global_download_rate is None:
        logger.info(
            "No global download rate specified, download traffic prioritization won't "
            "work"
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

    global_download_priority = config.get("download-priority", lowest_priority)
    global_upload_priority = config.get("upload-priority", lowest_priority)

    config_global_download_minimum_rate = config.get("download-minimum")
    global_download_minimum_rate = (
        GLOBAL_MINIMUM_DOWNLOAD_RATE
        if config_global_download_minimum_rate is None
        else config_global_download_minimum_rate
    )
    if config_global_download_rate is not None:
        logger.info(
            "Setting up global class with max download rate: {} (minimum: {}) and "
            "priority: {}",
            global_download_rate,
            global_download_minimum_rate,
            global_download_priority,
        )
    else:
        logger.info(
            "Setting up global class with unlimited download rate (minimum: {}) and "
            "priority: {}",
            global_download_minimum_rate,
            global_download_priority,
        )

    config_global_upload_minimum_rate = config.get("upload-minimum")
    global_upload_minimum_rate = (
        GLOBAL_MINIMUM_UPLOAD_RATE
        if config_global_upload_minimum_rate is None
        else config_global_upload_minimum_rate
    )
    if config_global_upload_rate is not None:
        logger.info(
            "Setting up global class with max upload rate: {} (minimum: {}) and "
            "priority: {}",
            global_upload_rate,
            global_upload_minimum_rate,
            global_upload_priority,
        )
    else:
        logger.info(
            "Setting up global class with unlimited upload rate (minimum: {}) and "
            "priority: {}",
            global_upload_minimum_rate,
            global_upload_priority,
        )

    ingress_qdisc, egress_qdisc = tc_setup(
        arguments.device,
        global_download_rate,
        global_download_minimum_rate,
        global_upload_rate,
        global_upload_minimum_rate,
        global_download_priority,
        global_upload_priority,
    )
    atexit.register(_clean_up, ingress_qdisc.device, egress_qdisc.device)

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
        config_download_minimum_rate = process.get("download-minimum")
        config_download_priority = process.get("download-priority")
        download_rate = (
            global_download_rate
            if config_download_rate is None
            else config_download_rate
        )
        download_minimum_rate = (
            MINIMUM_DOWNLOAD_RATE
            if config_download_minimum_rate is None
            else config_download_minimum_rate
        )
        download_priority = (
            lowest_priority
            if config_download_priority is None
            else config_download_priority
        )

        config_upload_rate = process.get("upload")
        config_upload_minimum_rate = process.get("upload-minimum")
        config_upload_priority = process.get("upload-priority")
        upload_rate = (
            global_upload_rate if config_upload_rate is None else config_upload_rate
        )
        upload_minimum_rate = (
            MINIMUM_UPLOAD_RATE
            if config_upload_minimum_rate is None
            else config_upload_minimum_rate
        )
        upload_priority = (
            lowest_priority
            if config_upload_priority is None
            else config_upload_priority
        )

        if config_download_rate is not None:
            logger.info(
                "Setting up class for: {!r} with max download rate: {} (minimum: {}) "
                "and priority: {}",
                name,
                download_rate,
                download_minimum_rate,
                download_priority,
            )
            ingress_class_id = tc_add_htb_class(
                ingress_qdisc, download_rate, download_minimum_rate, download_priority,
            )
            class_ids[_TrafficType.Ingress][name] = ingress_class_id
        elif config_download_priority is not None:
            logger.info(
                "Setting up class for: {!r} with unlimited download rate (minimum: {}) "
                "and priority: {}",
                name,
                download_minimum_rate,
                download_priority,
            )
            ingress_class_id = tc_add_htb_class(
                ingress_qdisc, download_rate, download_minimum_rate, download_priority,
            )
            class_ids[_TrafficType.Ingress][name] = ingress_class_id

        if config_upload_rate is not None:
            logger.info(
                "Setting up class for: {!r} with max upload rate: {} (minimum: {}) and "
                "priority: {}",
                name,
                upload_rate,
                upload_minimum_rate,
                upload_priority,
            )
            egress_class_id = tc_add_htb_class(
                egress_qdisc, upload_rate, upload_minimum_rate, upload_priority,
            )
            class_ids[_TrafficType.Egress][name] = egress_class_id
        elif config_upload_priority is not None:
            logger.info(
                "Setting up class for: {!r} with unlimited upload rate (minimum: {}) "
                "and priority: {}",
                name,
                upload_minimum_rate,
                upload_priority,
            )
            egress_class_id = tc_add_htb_class(
                egress_qdisc, upload_rate, upload_minimum_rate, upload_priority,
            )
            class_ids[_TrafficType.Egress][name] = egress_class_id

    port_to_filter_id: Dict[_TrafficType, Dict[int, str]] = {
        _TrafficType.Ingress: {},
        _TrafficType.Egress: {},
    }

    def add_ingress_filter(port: int, class_id: int) -> None:
        filter_id = tc_add_u32_filter(
            ingress_qdisc, f"match ip dport {port} 0xffff", class_id,
        )
        port_to_filter_id[_TrafficType.Ingress][port] = filter_id

    def add_egress_filter(port: int, class_id: int) -> None:
        filter_id = tc_add_u32_filter(
            egress_qdisc, f"match ip sport {port} 0xffff", class_id,
        )
        port_to_filter_id[_TrafficType.Egress][port] = filter_id

    def remove_filters(port: int) -> None:
        ingress_filter_id = port_to_filter_id[_TrafficType.Ingress].get(port)
        if ingress_filter_id:
            tc_remove_u32_filter(ingress_qdisc, ingress_filter_id)
            del port_to_filter_id[_TrafficType.Ingress][port]

        egress_filter_id = port_to_filter_id[_TrafficType.Egress].get(port)
        if egress_filter_id:
            tc_remove_u32_filter(egress_qdisc, egress_filter_id)
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
