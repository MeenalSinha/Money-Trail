"""Logging setup. Log level comes from Settings; secrets are never logged."""
import logging


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    # the client libraries can be verbose at DEBUG and may echo connection details
    logging.getLogger("redis").setLevel(logging.WARNING)
