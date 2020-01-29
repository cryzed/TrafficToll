import sys

from loguru import logger

from .cli import get_argument_parser, main as cli_main


def main() -> None:
    parser = get_argument_parser()
    arguments = parser.parse_args()
    logger.stop(0)
    logger.add(sys.stderr, level=arguments.logging_level)

    try:
        cli_main(arguments)
    except KeyboardInterrupt:
        logger.info("Aborted")
    except Exception as error:
        logger.error("Unexpected error occurred: {}", error)
