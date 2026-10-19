"""Fail-closed TXT assembly from *verified original source* checkpoints.

No network access, no BookLibrary writes, no translation, and no trust in a
job's status string alone. The preview and download verify every chapter's
identity, body digest, byte length and contiguous observed manifest. The
export is spooled to a temporary file, not a 256-MiB in-memory response.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from typing import BinaryIO

from .source_job_store import SourceJobStore, SourceJobConflict
from .source_team_reader import SourceTeamReader
from .storage import QueueStore

MAX_SOURCE_TXT_BYTES = 64 * 1024 * 1024
MAX_REPORT_ISSUES = 50


class SourceExportNotReady(Exception):
    def __init__(self, report: dict):
        super().__init__("Source collection incomplete, oversized or changed")
        self.report = report


@dataclass
class SourceTXTResult:
    file: BinaryIO
    content_sha256: str
    manifest_sha256: str
    byte_length: int
    source: str
    start_ordinal: int
    end_ordinal: int


def _text_part(ordinal: int, title: str, body: str, *, first: bool) -> bytes:
    # Retain the source chapter, normalize only line endings; an arbitrary
    # source heading-like paragraph must not create an extra chapter here.
    title = re.sub(r"\s+", " ", title).strip()
    text = body.replace("\r\n", "\n").replace("\r", "\n")
    if "\x00" in title or "\x00" in text:
        raise ValueError("NUL in original text")
    separator = "" if first else "\n\n"
    return (separator + f"Глава {ordinal}. {title}\n\n" + text).encode("utf-8")


class VerifiedSourceTXT:
    def __init__(self, repo: QueueStore):
        self.repo = repo

    def _scan(self, actor: str, job_id: str, *, sink: BinaryIO | None = None,
              team_read: bool = False) -> dict:
        digest = hashlib.sha256()
        byte_length = 0
        checked = 0
        missing = 0
        failed = 0
        damaged = 0
        issues: list[dict] = []
        issue_total = 0
        all_count = 0
        with closing(self.repo._connect()) as con:
            # A single SQLite read snapshot protects against a worker updating
            # checkpoints between integrity checks and output assembly.
            con.execute("BEGIN")
            # Team-readable source bodies are a deliberate, authenticated
            # read boundary; owner-only API operations still use _owned().
            job = (SourceTeamReader._shared(con, job_id) if team_read
                   else SourceJobStore._owned(con, actor, job_id))
            selected = SourceJobStore._manifest(job)
            source = job["source"]
            start, end = selected[0]["ordinal"], selected[-1]["ordinal"]
            rows = con.execute(
                "SELECT item_index,ordinal,source_id,url,status,body,sha256,error,bytes "
                "FROM source_collect_items WHERE job_id=? ORDER BY item_index",
                (job_id,),
            )
            expected_index = 0
            seen_digests: set[str] = set()

            def note(ordinal: int | None, code: str, detail: str = "") -> None:
                nonlocal damaged, issue_total
                issue_total += 1
                if code in ("corrupted", "unexpected_checkpoint", "duplicate_body"):
                    damaged += 1
                if len(issues) < MAX_REPORT_ISSUES:
                    result = {"ordinal": ordinal, "code": code}
                    if detail:
                        result["detail"] = detail[:120]
                    issues.append(result)

            for item in rows:
                all_count += 1
                index = item["item_index"]
                if index < 0 or index >= len(selected):
                    note(item["ordinal"], "unexpected_checkpoint")
                    continue
                while expected_index < index:
                    missing += 1
                    note(selected[expected_index]["ordinal"], "missing")
                    expected_index += 1
                if index < expected_index:
                    note(item["ordinal"], "unexpected_checkpoint")
                    continue
                ch = selected[index]
                expected_index = index + 1
                if (item["ordinal"] != ch["ordinal"] or
                    item["source_id"] != ch["source_id"] or
                    item["url"] != ch["url"]):
                    note(ch["ordinal"], "corrupted", "Stored chapter identity differs from catalog")
                    continue
                if item["status"] == "failed":
                    failed += 1
                    note(ch["ordinal"], "failed", str(item["error"] or "Source unavailable"))
                    continue
                body, recorded_hash, recorded_bytes = item["body"], item["sha256"], item["bytes"]
                if (item["status"] != "ok" or not isinstance(body, str) or
                    not isinstance(recorded_hash, str) or
                    not isinstance(recorded_bytes, int)):
                    note(ch["ordinal"], "corrupted", "Invalid source checkpoint")
                    continue
                body_bytes = body.encode("utf-8")
                actual_hash = hashlib.sha256(body_bytes).hexdigest()
                if (actual_hash != recorded_hash or
                    len(body_bytes) != recorded_bytes):
                    note(ch["ordinal"], "corrupted", "Source content checksum mismatch")
                    continue
                if actual_hash in seen_digests:
                    note(ch["ordinal"], "duplicate_body", "Two chapter bodies are identical")
                    continue
                seen_digests.add(actual_hash)
                try:
                    part = _text_part(ch["ordinal"], ch["title"], body, first=index == 0)
                except ValueError:
                    note(ch["ordinal"], "corrupted", "Control characters in source text")
                    continue
                if byte_length + len(part) + 1 > MAX_SOURCE_TXT_BYTES:
                    note(ch["ordinal"], "too_large", "Selected TXT exceeds 64 MiB limit")
                    continue
                byte_length += len(part)
                digest.update(part)
                if sink is not None:
                    sink.write(part)
                checked += 1
            while expected_index < len(selected):
                missing += 1
                note(selected[expected_index]["ordinal"], "missing")
                expected_index += 1

            # A complete status is necessary but never sufficient. Detect
            # corrupted/missing checkpoints even if status is forged.
            ready = (
                job["status"] == "complete" and checked == len(selected) and
                all_count == len(selected) and not (missing or failed or damaged or issues)
            )
            if job["status"] != "complete":
                note(None, "job_not_complete", f"Job state: {job['status']}")
            if ready:
                digest.update(b"\n")
                if sink is not None:
                    sink.write(b"\n")
                byte_length += 1
            result = {
                "ready": ready,
                "scope": "selected_catalog_range_only",
                "source": source,
                "catalog_sha256": job["catalog_sha256"],
                "manifest_sha256": job["manifest_sha256"],
                "start_ordinal": start,
                "end_ordinal": end,
                "selected_count": len(selected),
                "verified_count": checked,
                "missing_count": missing,
                "failed_count": failed,
                "damaged_count": damaged,
                "issue_count": issue_total,
                "issues": issues,
                "issues_truncated": issue_total > len(issues),
                "content_sha256": digest.hexdigest() if ready else None,
                "content_bytes": byte_length if ready else None,
            }
            return result

    def inspect(self, actor: str, job_id: str, *, team_read: bool = False) -> dict:
        """Return coverage and manifest receipt, never original prose."""
        return self._scan(actor, job_id, team_read=team_read)

    def render(
        self, actor: str, job_id: str,
        expected_manifest_sha256: str, expected_content_sha256: str,
        *, team_read: bool = False,
    ) -> tuple[dict, SourceTXTResult]:
        """Build a fully checked file before exposing the first HTTP byte.

        If the source changed since preview, discard the temporary file and
        reject rather than offering a silent partial manuscript.
        """
        handle = tempfile.TemporaryFile(mode="w+b")
        try:
            report = self._scan(actor, job_id, sink=handle, team_read=team_read)
            if (not report["ready"] or
                report["manifest_sha256"] != expected_manifest_sha256 or
                report["content_sha256"] != expected_content_sha256):
                raise SourceExportNotReady(report)
            handle.seek(0)
            return report, SourceTXTResult(
                handle, report["content_sha256"], report["manifest_sha256"],
                report["content_bytes"], report["source"],
                report["start_ordinal"], report["end_ordinal"],
            )
        except BaseException:
            handle.close()
            raise
