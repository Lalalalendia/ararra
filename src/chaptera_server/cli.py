"""Entrypoints: `chaptera-server serve` and `chaptera-server worker`."""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from .config import Settings
from .provider import GeminiProvider
from .quota import DailyQuotaLedger, QuotaControlledProvider
from .scheduled_provider import ScheduledGeminiProvider
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
    # Desktop model config reserves 65,536 output tokens and sends 98% of
    # that ceiling to Gemini. Keep the same conservative TPM envelope.
    output_tokens = (max(1, int(settings.gemini_reserved_output_tokens * 0.98))
                     if settings.gemini_reserved_output_tokens else None)
    provider = GeminiProvider(settings.gemini_api_key, settings.gemini_model,
                              max_output_tokens=output_tokens,
                              stream_atoms=settings.gemini_stream_atoms)
    if settings.gemini_requests_per_day:
        ledger = DailyQuotaLedger(
            store, settings.gemini_quota_project,
            settings.gemini_model, settings.gemini_requests_per_day,
        )
        provider = QuotaControlledProvider(provider, ledger)
    # Desktop ordering: RPM/RPS/TPM -> network gate -> SQLite RPD -> Gemini.
    # One worker/key by default, with one shared scheduler across its chapters.
    provider = ScheduledGeminiProvider(
        provider, rpm_limit=settings.gemini_rpm,
        tpm_limit=settings.gemini_tpm,
        reserved_output_tokens=settings.gemini_reserved_output_tokens,
    )
    worker = TranslationWorker(store, provider)
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
