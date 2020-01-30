import functools
import shlex
import shutil
import subprocess
from typing import Optional

from loguru import logger

from .exceptions import DependencyError


# Cache the full executable path just in case subprocess.Popen doesn't
@functools.lru_cache(None)
def _which(binary: str) -> Optional[str]:
    return shutil.which(binary)


def run(command: str, **popen_kwargs) -> subprocess.CompletedProcess:
    executable, *arguments = shlex.split(command)
    path = _which(executable)
    if not path:
        raise DependencyError(f"Executable for command: {command!r} not found")

    logger.debug(command)
    return subprocess.run([path] + arguments, **popen_kwargs)
