"""Read the exact full_cycle_app.chapter_exchange v1 SOURCE ZIP contract.

The archive is a transport for independently parsed chapters. Never try to
recognise chapter titles inside its TXT bodies, fetch source URLs, or treat
the ordinal as an author's chapter number. This module only validates bytes.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit

FORMAT = "full_cycle_app.chapter_exchange"
VERSION = 1
SHA = re.compile(r"[0-9a-f]{64}\Z")


def _valid_url(url: str) -> bool:
    if not url:
        return True
    if len(url) > 2048 or any(ord(ch) < 32 for ch in url):
        return False
    try:
        parsed = urlsplit(url)
        return (parsed.scheme in {"https", "http"} and
                bool(parsed.hostname) and parsed.username is None and
                parsed.password is None)
    except ValueError:
        return False


def read_exchange(data: bytes, checked_zip, archive_member, error_type,
                  *, chapter_limit: int, character_limit: int):
    """Return (book title, validated chapter entries, warnings, source metadata).

    checked_zip and archive_member are the importer's existing bounded ZIP
    primitives: no extraction, symlinks or unbounded uncompressed payloads.
    """
    with checked_zip(data, max_entries=chapter_limit * 2 + 2) as zf:
        infos = zf.infolist()
        names = [info.filename for info in infos if not info.is_dir()]
        if len(names) != len(set(names)):
            raise error_type("Duplicate ZIP member names are not allowed")
        for info in infos:
            kind = (info.external_attr >> 16) & 0o170000
            if kind == 0o120000:
                raise error_type("ZIP symlinks are forbidden")
            if "\\" in info.filename:
                raise error_type("Backslash ZIP paths are forbidden")
        if names.count("manifest.json") != 1:
            raise error_type("Full Cycle source archive must contain manifest.json")
        raw = archive_member(zf, "manifest.json")
        if len(raw) > 2_000_000:
            raise error_type("Full Cycle manifest is too large")
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise error_type("Invalid UTF-8 manifest.json") from exc
        if (not isinstance(manifest, dict) or manifest.get("format") != FORMAT or
                type(manifest.get("version")) is not int or
                manifest["version"] != VERSION or manifest.get("kind") != "source"):
            raise error_type("Expected full_cycle_app.chapter_exchange v1 source archive")
        chapters = manifest.get("chapters")
        count = manifest.get("chapter_count")
        if (not isinstance(chapters, list) or not chapters or
                len(chapters) > chapter_limit or type(count) is not int or
                count != len(chapters)):
            raise error_type("Full Cycle chapter count is invalid or exceeds the limit")
        project = manifest.get("project")
        source = manifest.get("source")
        if not isinstance(project, dict) or not isinstance(source, dict):
            raise error_type("Full Cycle project/source metadata is missing")
        title = project.get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > 500:
            raise error_type("Full Cycle book title is missing or invalid")
        book_url = source.get("source_url", "")
        if not isinstance(book_url, str) or not _valid_url(book_url):
            raise error_type("Invalid source book URL")
        used_indices: set[int] = set()
        expected_files = {"manifest.json"}
        if "README.txt" in names:
            expected_files.add("README.txt")
        output: list[dict] = []
        for sequence, entry in enumerate(chapters, 1):
            if not isinstance(entry, dict):
                raise error_type(f"Full Cycle entry {sequence} is invalid")
            index = entry.get("chapter_index")
            if type(index) is not int or not 1 <= index <= 10_000_000:
                raise error_type(f"Full Cycle entry {sequence} has an invalid position")
            if index in used_indices:
                raise error_type(f"Duplicate Full Cycle position {index}")
            used_indices.add(index)
            chapter_title = entry.get("chapter_title")
            if not isinstance(chapter_title, str) or len(chapter_title) > 500:
                raise error_type(f"Full Cycle entry {sequence} has an invalid title")
            chapter_url = entry.get("chapter_url", "")
            if not isinstance(chapter_url, str) or not _valid_url(chapter_url):
                raise error_type(f"Full Cycle entry {sequence} has an invalid URL")
            working_file = entry.get("working_file")
            original_file = entry.get("original_file")
            if (working_file != f"chapters/{index:06d}.txt" or
                    original_file != f"original/{index:06d}.txt"):
                raise error_type(f"Full Cycle entry {sequence} has invalid file references")
            expected_files.update((working_file, original_file))
            hashes: list[str] = []
            texts: list[str] = []
            for member, field in ((working_file, "working_sha256"),
                                  (original_file, "original_sha256")):
                declared = entry.get(field)
                if not isinstance(declared, str) or not SHA.fullmatch(declared):
                    raise error_type(f"Invalid {field} for chapter {index}")
                raw_text = archive_member(zf, member)
                if hashlib.sha256(raw_text).hexdigest() != declared:
                    raise error_type(f"SHA-256 mismatch for chapter {index}: {member}")
                try:
                    text = raw_text.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise error_type(f"Chapter {index} is not UTF-8") from exc
                if len(text) > character_limit:
                    raise error_type(f"Chapter {index} exceeds the character limit")
                if not text.strip():
                    raise error_type(f"Chapter {index} has empty {field}")
                texts.append(text)
                hashes.append(declared)
            output.append({
                "position": index,
                "title": chapter_title or f"Без названия (позиция {index})",
                "url": chapter_url,
                "working_file": working_file,
                "original_file": original_file,
                "working_text": texts[0],
                "original_text": texts[1],
                "working_sha256": hashes[0],
                "original_sha256": hashes[1],
                "had_edited_copy": entry.get("had_edited_copy") is True,
                "had_normalized_copy": entry.get("had_normalized_copy") is True,
            })
        actual = set(names)
        if actual != expected_files:
            raise error_type(
                "Full Cycle ZIP has unexpected or unreferenced files: " +
                ", ".join(sorted(actual ^ expected_files)[:4])
            )
        # Never infer full TOC completeness from the number of exported files.
        sorted_positions = sorted(used_indices)
        missing_ranges = [
            [left + 1, right - 1]
            for left, right in zip(sorted_positions, sorted_positions[1:])
            if right > left + 1
        ]
        warnings = ["Full Cycle archive contains only exported chapters; source TOC completeness is not verified"]
        if missing_ranges:
            warnings.append("Gaps in exported chapter positions; check omitted or failed downloads")
        return title, output, warnings, {
            "format": FORMAT,
            "version": VERSION,
            "source_url": book_url,
            "source_domain": source.get("domain") if isinstance(source.get("domain"), str) else "",
            "external_book_id": source.get("external_book_id") if isinstance(source.get("external_book_id"), str) else "",
            "chapter_positions": sorted_positions,
            "missing_position_ranges": missing_ranges[:100],
            "source_coverage_verified": False,
        }
