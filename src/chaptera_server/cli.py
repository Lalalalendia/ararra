"""Entrypoints: `chaptera-server serve` and `chaptera-server worker`."""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from .config import Settings
from .provider import GeminiProvider
from .storage import QueueStore
from .worker import TranslationWorker


async def _run_worker(settings: Settings) -> None:
    """Finish the active job on SIGTERM and stop claiming additional jobs."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            pass
    store = QueueStore(settings.db_path)
    worker = TranslationWorker(store, GeminiProvider(settings.gemini_api_key, settings.gemini_model))
    await worker.run_forever(poll_seconds=settings.worker_poll_seconds, stop=stop)


def main() -> None:
    parser = argparse.ArgumentParser(description="Chaptera team translation backend")
    parser.add_argument("command", choices=["serve", "worker"])
    args = parser.parse_args()
    settings = Settings.from_env()
    logging.basicConfig(level=logging.INFO)
    if args.command == "serve":
        import uvicorn
        from .api import create_app
        uvicorn.run(create_app(settings), host=settings.bind, port=settings.port, log_level="info")
    else:
        if not settings.gemini_api_key:
            parser.error("CHAPTERA_GEMINI_API_KEY is required for worker")
        try:
            asyncio.run(_run_worker(settings))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
