import collections
import itertools
import re
from typing import DefaultDict, Iterable, Set

import psutil
from loguru import logger

# noinspection PyProtectedMember
from psutil._common import pconn

ProcessFilterPredicate = collections.namedtuple(
    "ProcessFilterPredicate", ["name", "conditions", "recursive"]
)


def _match_process(process: psutil.Process, predicate: ProcessFilterPredicate) -> bool:
    for condition in predicate.conditions:
        name, regex = condition
        value = getattr(process, name)()
        if isinstance(value, int):
            value = str(value)
        elif isinstance(value, (list, tuple)):
            value = " ".join(value)

        if not re.match(regex, value):
            return False

    return True


def filter_net_connections(
    predicates: Iterable[ProcessFilterPredicate],
) -> DefaultDict[str, Set[pconn]]:
    filtered: DefaultDict[str, Set[pconn]] = collections.defaultdict(set)
    for process, predicate in itertools.product(psutil.process_iter(), predicates):
        try:
            if not _match_process(process, predicate):
                continue

            connections = filtered[predicate.name]
            connections.update(process.connections())
        except psutil.NoSuchProcess:
            logger.debug(
                "Process with PID {} died while filtering network connections",
                process.pid,
            )
            continue

        if predicate.recursive:
            for child in process.children(recursive=True):
                try:
                    connections.update(child.connections())
                except psutil.NoSuchProcess:
                    pass

    return filtered
