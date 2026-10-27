"""Strict, offline reader for full_cycle_app.chapter_exchange v1 source ZIPs.

Chapter boundaries, titles and original/working bytes come from manifest.json,
never from title heuristics. No extraction to disk, HTTP, or translation jobs.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile

ARCHIVE_FORMAT = "full_cycle_app.chapter_exchange"
ARCHIVE_VERSION = 1
MAX_EXCHANGE_UPLOAD_BYTES = 24 * 1024 * 1024
MAX_EXCHANGE_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_EXCHANGE_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_EXCHANGE_MEMBER_BYTES = 10 * 1024 * 1024
MAX_EXCHANGE_CHAPTERS = 2000
_HEX_SHA = re.compile(r"[0-9a-f]{64}\Z")
_MEMBER_NAME = re.compile(r"(?:chapters|original)/[0-9]{6,10}\.txt\Z")


class ExchangeImportError(ValueError):
    """The supplied archive cannot be trusted as a complete source exchange."""


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict:
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ExchangeImportError("Duplicate JSON key in source manifest")
        obj[key] = value
    return obj


def _string(value: object, key: str, limit: int, *, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise ExchangeImportError(f"Invalid source archive {key}")
    if required and not value.strip():
        raise ExchangeImportError(f"Missing source archive {key}")
    return value


def _sha(value: object, field: str) -> str:
    if not isinstance(value, str) or _HEX_SHA.fullmatch(value) is None:
        raise ExchangeImportError(f"Invalid {field} in source archive")
    return value


def _read_member(archive: zipfile.ZipFile, member: str, max_bytes: int) -> bytes:
    try:
        info = archive.getinfo(member)
        if info.file_size > max_bytes:
            raise ExchangeImportError(f"Archive member exceeds safety limit: {member}")
        with archive.open(info, "r") as stream:
            result = stream.read(max_bytes + 1)
    except (KeyError, RuntimeError, OSError, EOFError, zipfile.BadZipFile) as exc:
        raise ExchangeImportError(f"Missing or damaged archive member: {member}") from exc
    if len(result) != info.file_size or len(result) > max_bytes:
        raise ExchangeImportError(f"Archive member size mismatch: {member}")
    return result


def _validate_archive(archive: zipfile.ZipFile) -> set[str]:
    infos = archive.infolist()
    if len(infos) > 2 * MAX_EXCHANGE_CHAPTERS + 4:
        raise ExchangeImportError("Too many entries in source archive")
    names: set[str] = set()
    total = 0
    for info in infos:
        name = info.filename
        if name in names:
            raise ExchangeImportError("Duplicate archive member name")
        names.add(name)
        if (not name or name.startswith("/") or "\\" in name or "\x00" in name
                or any(part in ("", ".", "..") for part in name.rstrip("/").split("/"))):
            raise ExchangeImportError("Unsafe source archive member path")
        mode = stat.S_IFMT(info.external_attr >> 16)
        if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ExchangeImportError("Source archive contains a non-regular file")
        if info.flag_bits & 1:
            raise ExchangeImportError("Encrypted source archives are not supported")
        if info.is_dir():
            if name not in ("chapters/", "original/"):
                raise ExchangeImportError("Unexpected source archive directory")
            continue
        if name not in ("manifest.json", "README.txt") and not _MEMBER_NAME.fullmatch(name):
            raise ExchangeImportError("Unexpected file in source archive")
        total += info.file_size
        member_limit = (MAX_EXCHANGE_MANIFEST_BYTES if name == "manifest.json"
                        else 1024 * 1024 if name == "README.txt"
                        else MAX_EXCHANGE_MEMBER_BYTES)
        if (info.file_size > member_limit
                or total > MAX_EXCHANGE_EXPANDED_BYTES
                or info.file_size > 250 * max(1024, info.compress_size)):
            raise ExchangeImportError("Source archive exceeds decompression limits")
    if "manifest.json" not in names:
        raise ExchangeImportError("Source archive has no manifest.json")
    return names


def load_full_cycle_source(data: bytes, fallback_title: str) -> dict:
    """Read only a versioned *source* export, validating every referenced byte.

    No partial import on damaged chapter, duplicate identity, edited manifest,
    wrong archive kind, or missing original. The manifest is the only TOC.
    """
    if not data or len(data) > MAX_EXCHANGE_UPLOAD_BYTES:
        raise ExchangeImportError("Full Cycle App ZIP exceeds the 24 MiB upload limit")
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, EOFError, OSError) as exc:
        raise ExchangeImportError("Invalid Full Cycle App ZIP archive") from exc
    with archive:
        names = _validate_archive(archive)
        try:
            manifest = json.loads(
                _read_member(archive, "manifest.json", MAX_EXCHANGE_MANIFEST_BYTES).decode("utf-8"),
                object_pairs_hook=_object_without_duplicates,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ExchangeImportError("Invalid UTF-8 source manifest.json") from exc
        if (not isinstance(manifest, dict)
                or manifest.get("format") != ARCHIVE_FORMAT
                or type(manifest.get("version")) is not int
                or manifest["version"] != ARCHIVE_VERSION
                or manifest.get("kind") != "source"):
            raise ExchangeImportError("Expected Full Cycle App source archive v1")
        chapters = manifest.get("chapters")
        if (not isinstance(chapters, list)
                or not 1 <= len(chapters) <= MAX_EXCHANGE_CHAPTERS
                or type(manifest.get("chapter_count")) is not int
                or manifest["chapter_count"] != len(chapters)):
            raise ExchangeImportError("Source archive chapter count does not match manifest")
        project = manifest.get("project")
        source = manifest.get("source")
        if not isinstance(project, dict) or not isinstance(source, dict):
            raise ExchangeImportError("Source archive project/source metadata is missing")
        title = _string(project.get("title", ""), "project title", 500).strip() or fallback_title
        source_url = _string(source.get("source_url", ""), "source URL", 2048)
        source_domain = _string(source.get("domain", ""), "source domain", 255)
        external_book_id = _string(source.get("external_book_id", ""), "external book ID", 255)
        parsed = []
        used_indices = set()
        used_members = {"manifest.json", "README.txt"}
        for sequence, row in enumerate(chapters, start=1):
            if not isinstance(row, dict):
                raise ExchangeImportError(f"Invalid source chapter {sequence}")
            index = row.get("chapter_index")
            if type(index) is not int or not 1 <= index <= 10_000_000:
                raise ExchangeImportError(f"Invalid source chapter index at row {sequence}")
            if index in used_indices:
                raise ExchangeImportError(f"Duplicate source chapter index {index}")
            used_indices.add(index)
            raw_title = _string(row.get("chapter_title", ""), "chapter title", 500)
            title_label = raw_title.strip() or f"Без названия (позиция {index})"
            chapter_url = _string(row.get("chapter_url", ""), "chapter URL", 2048)
            expected_working = f"chapters/{index:06d}.txt"
            expected_original = f"original/{index:06d}.txt"
            if row.get("working_file") != expected_working or row.get("original_file") != expected_original:
                raise ExchangeImportError(f"Archive chapter {index} has mismatched file references")
            for member in (expected_working, expected_original):
                if member not in names or member in used_members:
                    raise ExchangeImportError(f"Archive chapter {index} has a missing or shared member")
                used_members.add(member)
            expected_working_hash = _sha(row.get("working_sha256"), "working_sha256")
            expected_original_hash = _sha(row.get("original_sha256"), "original_sha256")
            working_data = _read_member(archive, expected_working, MAX_EXCHANGE_MEMBER_BYTES)
            original_data = _read_member(archive, expected_original, MAX_EXCHANGE_MEMBER_BYTES)
            if (hashlib.sha256(working_data).hexdigest() != expected_working_hash
                    or hashlib.sha256(original_data).hexdigest() != expected_original_hash):
                raise ExchangeImportError(f"Archive chapter {index} failed SHA-256 validation")
            try:
                working = working_data.decode("utf-8")
                original = original_data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ExchangeImportError(f"Archive chapter {index} is not UTF-8") from exc
            if not working.strip() or "\x00" in working or "\x00" in original:
                raise ExchangeImportError(f"Archive chapter {index} has empty or invalid text")
            if len(working) > 2_000_000 or len(original) > 2_000_000:
                raise ExchangeImportError(f"Archive chapter {index} exceeds the text limit")
            parsed.append({
                "chapter_index": index,
                "chapter_title": title_label,
                "source_url": chapter_url,
                "working_text": working,
                "original_text": original,
                "working_sha256": expected_working_hash,
                "original_sha256": expected_original_hash,
            })
        # No unaccounted TXT files, even if they were not referenced by manifest.
        if names - used_members - {"chapters/", "original/"}:
            raise ExchangeImportError("Archive includes unreferenced chapter files")
        sorted_indices = sorted(used_indices)
        gaps = [
            [left + 1, right - 1]
            for left, right in zip(sorted_indices, sorted_indices[1:])
            if right > left + 1
        ]
        return {
            "format": ARCHIVE_FORMAT,
            "version": ARCHIVE_VERSION,
            "title": title,
            "source_url": source_url,
            "source_domain": source_domain,
            "external_book_id": external_book_id,
            "missing_positions": gaps,
            "chapters": parsed,
        }
