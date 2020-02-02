import json
import subprocess
from typing import Tuple, Optional

from traffictoll.utils import run


# https://www.speedtest.net/apps/cli
def _ookla_speedtest_cli() -> Tuple[int, int]:
    process = run(
        "speedtest --format=json", stdout=subprocess.PIPE, universal_newlines=True,
    )
    result = json.loads(process.stdout)
    return result["download"]["bandwidth"], result["upload"]["bandwidth"]


# https://github.com/sivel/speedtest-cli
def _sivel_speedtest_cli() -> Tuple[int, int]:
    process = run("speedtest --json", stdout=subprocess.PIPE, universal_newlines=True)
    result = json.loads(process.stdout)
    return round(result["download"]), round(result["upload"])


def test_speed() -> Tuple[int, int]:
    process = run(
        "speedtest --version", stdout=subprocess.PIPE, universal_newlines=True
    )

    first_line = process.stdout.splitlines()[0]
    if first_line.startswith("Speedtest by Ookla"):
        return _ookla_speedtest_cli()
    elif first_line.startswith("speedtest-cli"):
        return _sivel_speedtest_cli()
