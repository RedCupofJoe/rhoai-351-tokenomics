"""Tokenomics exporter entrypoint."""

from __future__ import annotations

import logging
import signal
import threading

from tokenomics import __version__

from .config import Config
from .checkpoint import Checkpoint
from .metrics import Metrics
from .pollers import start_pollers
from .record import Recorder
from .server import make_server

LOG = logging.getLogger("maas_tokenomics")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = Config.from_env()
    if not config.ingest_token:
        raise SystemExit("INGEST_TOKEN is required. Refusing to expose an open usage ingest.")
    checkpoint = Checkpoint(config.checkpoint_path)
    metrics = Metrics()
    metrics.build_info.labels(version=__version__).set(1)
    recorder = Recorder(checkpoint, metrics)
    stop = threading.Event()
    start_pollers(config, recorder, stop)
    server = make_server(config, recorder, metrics.registry)

    def _shutdown(signum: int, _frame: object) -> None:
        LOG.info("Shutting down on signal %s", signum)
        stop.set()
        server.shutdown()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    LOG.info("Listening on %s:%s", config.listen_host, config.listen_port)
    server.serve_forever()
    checkpoint.close()


if __name__ == "__main__":
    main()
