"""UTC diagnostics for the daemon and satellite's existing stderr sink."""
import logging
import time


class UTCFormatter(logging.Formatter):
    converter = time.gmtime


def configure_logging(level: int | str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(UTCFormatter(
        "%(asctime)s.%(msecs)03dZ %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    ))
    # basicConfig remains a no-op when an embedding caller owns the handlers.
    logging.basicConfig(level=level, handlers=[handler])
