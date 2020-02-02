import collections
import enum
import json
import subprocess
from typing import Optional

from .exceptions import DependencyOutputError
from .utils import run

SPEEDTEST_VERSION_COMMAND = "speedtest --version"
OOKLA_SPEEDTEST_COMMAND = "speedtest --format=json"
SIVEL_SPEEDTEST_COMMAND = "speedtest --json"

SpeedTestResult = collections.namedtuple("SpeedTest", ["download_rate", "upload_rate"])


class _SpeedTestProvider(enum.Enum):
    Ookla = enum.auto()
    Sivel = enum.auto()
    Unknown = enum.auto()


# https://www.speedtest.net/apps/cli
def _ookla_speedtest_cli() -> SpeedTestResult:
    process = run(
        OOKLA_SPEEDTEST_COMMAND, stdout=subprocess.PIPE, universal_newlines=True,
    )

    try:
        result = json.loads(process.stdout)
        return SpeedTestResult(
            result["download"]["bandwidth"], result["upload"]["bandwidth"]
        )
    except (json.JSONDecodeError, KeyError):
        raise DependencyOutputError(
            f"Command: {OOKLA_SPEEDTEST_COMMAND!r} returned unrecognized output: "
            f"{process.stdout!r}"
        )


# https://github.com/sivel/speedtest-cli
def _sivel_speedtest_cli() -> SpeedTestResult:
    process = run(
        SIVEL_SPEEDTEST_COMMAND, stdout=subprocess.PIPE, universal_newlines=True
    )

    try:
        result = json.loads(process.stdout)
        return SpeedTestResult(round(result["download"]), round(result["upload"]))
    except (json.JSONDecodeError, KeyError):
        raise DependencyOutputError(
            f"Command: {SIVEL_SPEEDTEST_COMMAND!r} returned unrecognized output: "
            f"{process.stdout!r}"
        )


def _get_speedtest_provider() -> _SpeedTestProvider:
    process = run(
        SPEEDTEST_VERSION_COMMAND, stdout=subprocess.PIPE, universal_newlines=True
    )
    if process.stdout.startswith("Speedtest by Ookla"):
        return _SpeedTestProvider.Ookla
    elif process.stdout.startswith("speedtest-cli"):
        return _SpeedTestProvider.Sivel
    return _SpeedTestProvider.Unknown


def test_speed() -> Optional[SpeedTestResult]:
    speedtest_version = _get_speedtest_provider()

    if speedtest_version is _SpeedTestProvider.Ookla:
        return _ookla_speedtest_cli()
    elif speedtest_version is _SpeedTestProvider.Sivel:
        return _sivel_speedtest_cli()
