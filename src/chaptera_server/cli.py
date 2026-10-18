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
from .member_gemini_provider import MemberGeminiProviders


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
    if settings.gemini_vault_key:
        # In personal mode, no moderator ever borrows the old host Gemini key.
        # A queued job without its own key remains queued without paid attempts.
        provider = MemberGeminiProviders(store, settings)
    else:
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



async def _run_source_worker(settings: Settings) -> None:
    """Dedicated crawler process: no Gemini key, no book-library writes."""
    from .source_job_store import SourceJobStore
    from .source_job_worker import SourceCollectionWorker
    from .source_site_http import PublicHtmlFetcher

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            pass
    jobs = SourceJobStore(
        QueueStore(settings.db_path),
        actor_quota_bytes=settings.source_actor_quota_mib * 1024 * 1024,
        global_quota_bytes=settings.source_global_quota_mib * 1024 * 1024,
    )
    crawler = SourceCollectionWorker(jobs, PublicHtmlFetcher())
    await crawler.run_forever(poll_seconds=settings.worker_poll_seconds, stop=stop)

def main() -> None:
    parser = argparse.ArgumentParser(description="Chaptera team translation backend")
    parser.add_argument("command", choices=["serve", "worker", "source-worker"])
    args = parser.parse_args()
    settings = Settings.from_env()
    logging.basicConfig(level=logging.INFO)
    if args.command == "serve":
        import uvicorn
        from .api import create_app
        uvicorn.run(create_app(settings), host=settings.bind, port=settings.port, log_level="info", proxy_headers=False)
    elif args.command == "source-worker":
        try:
            asyncio.run(_run_source_worker(settings))
        except KeyboardInterrupt:
            pass
    else:
        if not settings.gemini_api_key and not settings.gemini_vault_key:
            parser.error("Gemini worker requires a shared host key or personal vault key")
        try:
            asyncio.run(_run_worker(settings))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
