"""Entrypoints: `chaptera-server serve` and `chaptera-server worker`."""
from __future__ import annotations

import argparse
import asyncio
import logging

from .config import Settings
from .provider import GeminiProvider
from .storage import QueueStore
from .worker import TranslationWorker


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
        store = QueueStore(settings.db_path)
        worker = TranslationWorker(store, GeminiProvider(settings.gemini_api_key, settings.gemini_model))
        try:
            asyncio.run(worker.run_forever(poll_seconds=settings.worker_poll_seconds))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
