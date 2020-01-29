import collections
import itertools
import re
from typing import List, DefaultDict, Iterable

import psutil
from loguru import logger

ProcessFilterPredicate = collections.namedtuple(
    "ProcessFilterPredicate", ["name", "conditions"]
)


def _match_process(process: psutil.Process, predicate: str) -> bool:
    name, regex = predicate
    value = getattr(process, name)()
    if isinstance(value, int):
        value = str(value)
    elif isinstance(value, (list, tuple)):
        value = " ".join(value)

    return bool(re.match(regex, value))


def filter_net_connections(
    predicates: Iterable[ProcessFilterPredicate],
) -> DefaultDict[str, List[psutil._common.pconn]]:
    filtered: DefaultDict[str, List[psutil._common.pconn]] = collections.defaultdict(
        list
    )
    connections = psutil.net_connections()
    for connection, predicate in itertools.product(connections, predicates):
        # Stop no specified conditions from matching every process
        if not (predicate.conditions and connection.pid):
            continue

        try:
            process = psutil.Process(connection.pid)
            if all(
                _match_process(process, condition) for condition in predicate.conditions
            ):
                filtered[predicate.name].append(connection)
        except psutil.NoSuchProcess:
            logger.warning(
                "Process with PID {} died while filtering network connections",
                connection.pid,
            )
            continue

    return filtered
