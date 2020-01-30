import sys

from loguru import logger

from .cli import get_argument_parser, main as cli_main
from .exceptions import DependencyError


def main() -> None:
    parser = get_argument_parser()
    arguments = parser.parse_args()
    logger.stop(0)
    logger.add(sys.stderr, level=arguments.logging_level)

    try:
        cli_main(arguments)
    except KeyboardInterrupt:
        logger.info("Aborted")
    except DependencyError as error:
        logger.error("Missing dependency: {}", error)
    except Exception as error:
        logger.error("Unexpected error occurred: {}", error)


if __name__ == "__main__":
    main()
