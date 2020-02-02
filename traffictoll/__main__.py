import sys

from loguru import logger

from .cli import get_argument_parser, main as cli_main
from .exceptions import ConfigError, MissingDependencyError


def main() -> None:
    parser = get_argument_parser()
    arguments = parser.parse_args()
    logger.stop(0)
    logger.add(sys.stderr, level=arguments.logging_level)

    # noinspection PyBroadException
    try:
        cli_main(arguments)
    except KeyboardInterrupt:
        logger.info("Aborted")
    except ConfigError as error:
        logger.error("Invalid configuration: {}", error)
    except MissingDependencyError as error:
        logger.error("Missing dependency: {}", error)
    except Exception:
        logger.exception("Unexpected error occurred:")


if __name__ == "__main__":
    main()
