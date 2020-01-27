import functools
import shlex
import shutil
import subprocess

from loguru import logger


# Not sure if subprocess.Popen caches the value
@functools.lru_cache(None)
def _which(binary):
    return shutil.which(binary)


# This is fine because we aren't dealing with any values in the commands that have to be
# quoted; on the off-chance that they have to, there's shlex.quote
def run(command, **kwargs):
    executable, *args = shlex.split(command)
    executable_path = _which(executable)

    logger.debug(command)
    return subprocess.run([executable_path] + args, **kwargs)
