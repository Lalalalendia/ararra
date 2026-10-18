"""Entrypoints: `chaptera-server serve` and `chaptera-server worker`."""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import json
import os

from .config import Settings, _read_systemd_credential
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
    parser.add_argument("command", choices=["serve", "worker", "source-worker", "export-drive", "drive-authorize", "drive-smoke", "drive-outbox"])
    parser.add_argument("--job-id", default="", help="Completed book translation job UUID")
    parser.add_argument("--dry-run", action="store_true", help="Inspect a job; no OAuth or Drive writes")
    parser.add_argument("--token-file", default="", help="Absolute path for a NEW private OAuth token file")
    parser.add_argument("--client-json", default="", help="Local Desktop OAuth client JSON downloaded from Google Cloud")
    parser.add_argument("--no-browser", action="store_true", help="Print consent link for manual opening on this computer")
    parser.add_argument("--live-smoke", action="store_true", help="Explicitly create and re-edit a synthetic Drive chapter pair")
    parser.add_argument("--enable", action="store_true", help="Opt in to QUEUE ONLY future completed book jobs")
    parser.add_argument("--disable", action="store_true", help="Pause the automatic Drive queue")
    parser.add_argument("--run-once", action="store_true", help="Export at most one queued completed job")
    parser.add_argument("--allow-drive-write", action="store_true", help="Explicit permission for outbox remote mutations")
    parser.add_argument("--retry-held", action="store_true", help="Explicitly requeue one held job after review")
    args = parser.parse_args()
    settings = Settings.from_env()
    logging.basicConfig(level=logging.INFO)
    if args.command == "serve":
        import uvicorn
        from .api import create_app
        # Only the local Nginx hop is trusted. Its X-Forwarded-For is
        # OVERWRITTEN with $remote_addr; user-supplied values never survive.
        private_proxy = settings.bind == "127.0.0.1"
        uvicorn.run(
            create_app(settings), host=settings.bind, port=settings.port,
            log_level="info", proxy_headers=private_proxy,
            forwarded_allow_ips="127.0.0.1",
        )
    elif args.command == "source-worker":
        try:
            asyncio.run(_run_source_worker(settings))
        except KeyboardInterrupt:
            pass
    elif args.command == "drive-authorize":
        from pathlib import Path
        from .drive_oauth_setup import DriveAuthorizationError, authorize_owner
        from .drive_export import DriveExportError
        if not args.token_file:
            parser.error("--token-file is required for drive-authorize")
        source_folder = os.environ.get("CHAPTERA_DRIVE_SOURCE_FOLDER_ID", "").strip()
        translation_folder = os.environ.get("CHAPTERA_DRIVE_TRANSLATION_FOLDER_ID", "").strip()
        client_id = _read_systemd_credential(
            "google_drive_client_id","CHAPTERA_DRIVE_CLIENT_ID","",
        ).strip()
        client_secret = _read_systemd_credential(
            "google_drive_client_secret","CHAPTERA_DRIVE_CLIENT_SECRET","",
        ).strip()
        try:
            if args.client_json:
                from .drive_oauth_setup import load_desktop_oauth_client
                client_id,client_secret=load_desktop_oauth_client(Path(args.client_json))
            result=authorize_owner(
                client_id=client_id,client_secret=client_secret,
                source_folder_id=source_folder,
                translation_folder_id=translation_folder,
                token_file=Path(args.token_file),
                open_browser=not args.no_browser,
            )
        except (DriveAuthorizationError,DriveExportError) as exc:
            parser.error(str(exc))
        print(json.dumps(result,ensure_ascii=False))
    elif args.command == "drive-smoke":
        from pathlib import Path
        import httpx
        from .drive_oauth_setup import (
            DriveAuthorizationError, load_desktop_oauth_client,
            read_private_refresh_token,
        )
        from .drive_acceptance import run_drive_acceptance
        from .drive_export import GoogleDriveClient, DriveExportError
        if bool(args.client_json) != bool(args.token_file):
            parser.error("For desktop smoke provide BOTH --client-json and --token-file")
        source_folder = os.environ.get("CHAPTERA_DRIVE_SOURCE_FOLDER_ID", "").strip()
        translation_folder = os.environ.get("CHAPTERA_DRIVE_TRANSLATION_FOLDER_ID", "").strip()
        if not source_folder or not translation_folder:
            parser.error("Both preexisting Google Drive folder IDs are required")
        try:
            if args.client_json:
                # Trusted owner's local computer: never echo imported secrets.
                client_id, client_secret = load_desktop_oauth_client(Path(args.client_json))
                refresh_token = read_private_refresh_token(Path(args.token_file))
            else:
                # VPS/systemd: reuse the SAME credentials as export-drive.
                # No need to place the owner JSON/token in CLI arguments.
                client_id = _read_systemd_credential(
                    "google_drive_client_id","CHAPTERA_DRIVE_CLIENT_ID","",
                ).strip()
                client_secret = _read_systemd_credential(
                    "google_drive_client_secret","CHAPTERA_DRIVE_CLIENT_SECRET","",
                ).strip()
                refresh_token = _read_systemd_credential(
                    "google_drive_refresh_token","CHAPTERA_DRIVE_REFRESH_TOKEN","",
                ).strip()
                if not all((client_id, client_secret, refresh_token)):
                    raise DriveAuthorizationError(
                        "Owner OAuth credentials are missing from protected server settings"
                    )
            with httpx.Client(
                timeout=30.0,trust_env=False,follow_redirects=False,
            ) as http:
                drive = GoogleDriveClient(
                    client_id=client_id,client_secret=client_secret,
                    refresh_token=refresh_token,http=http,
                )
                receipt = run_drive_acceptance(
                    drive,
                    source_folder_id=source_folder,
                    translation_folder_id=translation_folder,
                    live=args.live_smoke,
                )
            print(json.dumps(receipt,ensure_ascii=False))
        except (DriveAuthorizationError,DriveExportError) as exc:
            parser.error(str(exc))
    elif args.command == "export-drive":
        from .drive_export import (
            DriveExportError, GoogleDriveClient, read_completed_job, export_completed_job,
            _digest,
        )
        if not args.job_id:
            parser.error("--job-id is required for export-drive")
        store = QueueStore(settings.db_path)
        try:
            job = read_completed_job(store, args.job_id)
            if args.dry_run:
                print(json.dumps({
                    "dry_run":True,"job_id":job.job_id,"book_id":job.book_id,
                    "chapter_index":job.chapter_index,
                    "source_sha256":_digest(job.source),
                    "translation_sha256":_digest(job.translation),
                    "network_used":False,
                },ensure_ascii=False))
                return
            source_folder = os.environ.get("CHAPTERA_DRIVE_SOURCE_FOLDER_ID", "").strip()
            translation_folder = os.environ.get("CHAPTERA_DRIVE_TRANSLATION_FOLDER_ID", "").strip()
            client_id = _read_systemd_credential(
                "google_drive_client_id","CHAPTERA_DRIVE_CLIENT_ID","",
            ).strip()
            client_secret = _read_systemd_credential(
                "google_drive_client_secret","CHAPTERA_DRIVE_CLIENT_SECRET","",
            ).strip()
            refresh_token = _read_systemd_credential(
                "google_drive_refresh_token","CHAPTERA_DRIVE_REFRESH_TOKEN","",
            ).strip()
            if not source_folder or not translation_folder:
                raise DriveExportError("Both existing Drive folder IDs must be configured")
            with __import__("httpx").Client(
                timeout=30.0,trust_env=False,follow_redirects=False,
            ) as http:
                drive = GoogleDriveClient(
                    client_id=client_id,client_secret=client_secret,
                    refresh_token=refresh_token,http=http,
                )
                receipt = export_completed_job(
                    store,drive,args.job_id,source_folder_id=source_folder,
                    translation_folder_id=translation_folder,
                )
            print(json.dumps(receipt,ensure_ascii=False))
        except DriveExportError as exc:
            parser.error(str(exc))
    elif args.command == "drive-outbox":
        from .drive_outbox import DriveOutbox
        from .drive_export import (
            DriveExportError, GoogleDriveClient, export_completed_job,
        )
        import httpx
        actions=(args.enable,args.disable,args.run_once,args.retry_held)
        if sum(actions)>1:
            parser.error("Choose only one outbox operation")
        outbox=DriveOutbox(QueueStore(settings.db_path))
        if args.enable:
            receipt=outbox.set_enabled(True)
        elif args.disable:
            receipt=outbox.set_enabled(False)
        elif args.retry_held:
            if not args.job_id:
                parser.error("--retry-held requires --job-id and prior remote review")
            receipt={"stage":"requeued" if outbox.retry_held(args.job_id)
                     else "not_held","job_id":args.job_id}
        elif args.run_once:
            if not args.allow_drive_write:
                parser.error("--run-once requires --allow-drive-write")
            def export_one(job_id: str) -> dict:
                source_folder=os.environ.get("CHAPTERA_DRIVE_SOURCE_FOLDER_ID","").strip()
                translation_folder=os.environ.get("CHAPTERA_DRIVE_TRANSLATION_FOLDER_ID","").strip()
                if not source_folder or not translation_folder:
                    raise DriveExportError("Both Drive folder IDs must be configured")
                client_id=_read_systemd_credential(
                    "google_drive_client_id","CHAPTERA_DRIVE_CLIENT_ID","",
                ).strip()
                client_secret=_read_systemd_credential(
                    "google_drive_client_secret","CHAPTERA_DRIVE_CLIENT_SECRET","",
                ).strip()
                refresh_token=_read_systemd_credential(
                    "google_drive_refresh_token","CHAPTERA_DRIVE_REFRESH_TOKEN","",
                ).strip()
                with httpx.Client(timeout=30.0,trust_env=False,
                                  follow_redirects=False) as http:
                    drive=GoogleDriveClient(
                        client_id=client_id,client_secret=client_secret,
                        refresh_token=refresh_token,http=http,
                    )
                    return export_completed_job(
                        outbox.store,drive,job_id,source_folder_id=source_folder,
                        translation_folder_id=translation_folder,
                    )
            receipt=outbox.run_once(export_one)
        else:
            receipt={"stage":"status",**outbox.status()}
        print(json.dumps(receipt,ensure_ascii=False))
    else:
        if not settings.gemini_api_key and not settings.gemini_vault_key:
            parser.error("Gemini worker requires a shared host key or personal vault key")
        try:
            asyncio.run(_run_worker(settings))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
