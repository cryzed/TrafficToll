import functools
import shlex
import shutil
import subprocess
from typing import Optional

from loguru import logger


# Not sure if subprocess.Popen caches the value
@functools.lru_cache(None)
def _which(binary: str) -> Optional[str]:
    return shutil.which(binary)


# This is fine because we aren't dealing with any values in the commands that have to be
# quoted; on the off-chance that they have to, there's shlex.quote
def run(command: str, **kwargs) -> subprocess.CompletedProcess:
    executable, *args = shlex.split(command)
    executable_path = _which(executable)
    if not executable_path:
        raise RuntimeError(f"Executable for command: {command!r} not found")

    logger.debug(command)
    return subprocess.run([executable_path] + args, **kwargs)
