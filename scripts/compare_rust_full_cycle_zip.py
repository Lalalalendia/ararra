#!/usr/bin/env python3
"""M2f-1: differential Full Cycle exporter ZIP validation, entirely offline.

Real desktop exporter fixture, as well as adversarial synthetic archives,
are checked against the Python manifest/ZIP oracle. The native Rust reader
must reproduce full verified source+working text and provenance exactly,
or reject the ENTIRE archive. No SQLite, HTTP requests, Drive, or Gemini.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import stat
import subprocess
import sys
import tempfile
import warnings
import zipfile
from pathlib import Path

from chaptera_server.full_cycle_archive import (
    ExchangeImportError, load_full_cycle_source,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/full_cycle_actual_exporter_20261017.zip.b64"
FIXTURE_SHA = "b23df10ca7abd4e155cfdf38e7203a55e58554e7034ae9787394dbb2de7d146a"
FALLBACK = "Fallback novel"


def contents(data: bytes) -> list[tuple[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return [(entry.filename, archive.read(entry)) for entry in archive.infolist()]


def encode_manifest(manifest: dict) -> bytes:
    return json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def manifest_of(entries: list[tuple[str, bytes]]) -> dict:
    return json.loads(dict(entries)["manifest.json"])


def replace(entries: list[tuple[str, bytes]], name: str, payload: bytes):
    assert sum(key == name for key, _ in entries) == 1, name
    return [(key, payload if key == name else data) for key, data in entries]


def repack(entries, method=zipfile.ZIP_DEFLATED, overrides=None) -> bytes:
    overrides = overrides or {}
    stream = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(stream, "w", compression=method, allowZip64=True) as out:
            for filename, payload in entries:
                special = overrides.get(filename)
                if special is not None:
                    special.compress_type = method
                    out.writestr(special, payload)
                else:
                    out.writestr(filename, payload)
    return stream.getvalue()


def mutate_manifest(entries, update) -> list[tuple[str, bytes]]:
    obj = copy.deepcopy(manifest_of(entries))
    update(obj)
    return replace(entries, "manifest.json", encode_manifest(obj))


def edit_member(entries, index: int, version: str, payload: bytes):
    manifest = manifest_of(entries)
    chapter = manifest["chapters"][index]
    name = chapter[f"{version}_file"]
    chapter[f"{version}_sha256"] = hashlib.sha256(payload).hexdigest()
    return replace(replace(entries, name, payload), "manifest.json", encode_manifest(manifest))


def valid_cases(raw: bytes) -> list[tuple[str, bytes]]:
    entries = contents(raw)
    valid = [
        ("unmodified_actual_desktop_export", raw),
        ("stored_zip_same_content", repack(entries, zipfile.ZIP_STORED)),
        ("repacked_deflate", repack(entries)),
    ]
    edits = [
        ("reverse_manifest_positions", lambda m: m["chapters"].reverse()),
        ("duplicate_source_link_is_warning", lambda m: m["chapters"][1].update(
            chapter_url=m["chapters"][0]["chapter_url"])),
        ("blank_chapter_title", lambda m: m["chapters"][0].update(chapter_title="")),
        ("blank_project_title_fallback", lambda m: m["project"].update(title="")),
        ("missing_source_url_allowed", lambda m: m["source"].pop("source_url", None)),
        ("extra_metadata_is_not_a_chapter", lambda m: m.update(
            producer={"version": "desktop", "note": "原文 / проверка"})),
    ]
    for label, change in edits:
        valid.append((label, repack(mutate_manifest(entries, change))))
    return valid


def invalid_cases(raw: bytes) -> list[tuple[str, bytes]]:
    entries = contents(raw)
    bad = [("not_zip", b"not a zip archive")]
    mutations = [
        ("wrong_format", lambda m: m.update(format="other")),
        ("wrong_version", lambda m: m.update(version=2)),
        ("boolean_version", lambda m: m.update(version=True)),
        ("translation_archive_is_not_source", lambda m: m.update(kind="translation")),
        ("wrong_chapter_count", lambda m: m.update(chapter_count=2000)),
        ("duplicate_chapter_index", lambda m: m["chapters"][1].update(
            chapter_index=m["chapters"][0]["chapter_index"])),
        ("invalid_chapter_zero", lambda m: m["chapters"][0].update(chapter_index=0)),
        ("invalid_index_too_large", lambda m: m["chapters"][0].update(chapter_index=10000001)),
        ("mismatched_working_filename", lambda m: m["chapters"][0].update(
            working_file=m["chapters"][1]["working_file"])),
        ("bad_working_sha", lambda m: m["chapters"][0].update(
            working_sha256="0" * 64)),
        ("bad_original_sha", lambda m: m["chapters"][0].update(
            original_sha256="f" * 64)),
        ("uppercase_sha_not_canonical", lambda m: m["chapters"][0].update(
            working_sha256=m["chapters"][0]["working_sha256"].upper())),
        ("non_http_source_url", lambda m: m["source"].update(
            source_url="file:///etc/passwd")),
        ("credentials_in_chapter_url", lambda m: m["chapters"][0].update(
            chapter_url="https://alice:secret@book.example/7")),
        ("wrong_url_port", lambda m: m["source"].update(
            source_url="https://book.example:99999/")),
        ("newline_in_domain", lambda m: m["source"].update(
            domain="book\n.example")),
        ("empty_chapter_list", lambda m: m.update(chapters=[], chapter_count=0)),
        ("missing_project", lambda m: m.pop("project")),
        ("working_file_empty_metadata", lambda m: m["chapters"][0].pop("working_file")),
    ]
    for label, update in mutations:
        bad.append((label, repack(mutate_manifest(entries, update))))

    bad.extend([
        ("missing_original", repack([
            (name, data) for name, data in entries
            if name != manifest_of(entries)["chapters"][1]["original_file"]
        ])),
        ("unreferenced_chapter_member", repack(entries + [
            ("chapters/999999.txt", b"Unowned chapter")
        ])),
        ("duplicate_member_name", repack(entries + [
            ("manifest.json", encode_manifest(manifest_of(entries)))
        ])),
        ("path_traversal_member", repack(entries + [
            ("../secret.txt", b"payload")
        ])),
        ("unsupported_bzip2", repack(entries, zipfile.ZIP_BZIP2)),
        ("empty_working_text", repack(edit_member(entries, 1, "working", b""))),
        ("nul_working_text", repack(edit_member(entries, 1, "working", b"hello\x00world"))),
        ("nul_original_text", repack(edit_member(entries, 1, "original", b"hello\x00world"))),
        ("non_utf8_working_with_valid_sha", repack(edit_member(entries, 1, "working", b"\xff"))),
        ("non_utf8_original_with_valid_sha", repack(edit_member(entries, 1, "original", b"\xfe"))),
    ])
    # Both runtimes must detect duplicate manifest JSON keys even inside a row.
    manifest = encode_manifest(manifest_of(entries))
    assert b'"chapter_index":7' in manifest
    bad.append(("duplicate_json_field_nested", repack(replace(
        entries, "manifest.json",
        manifest.replace(b'"chapter_index":7', b'"chapter_index":7,"chapter_index":9', 1),
    ))))
    bad.append(("duplicate_json_field_root", repack(replace(
        entries, "manifest.json", b'{"format":"other",' + manifest[1:],
    ))))
    # A symlink is not a chapter file, even with a valid expected filename.
    original_name = manifest_of(entries)["chapters"][0]["original_file"]
    link = zipfile.ZipInfo(original_name)
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    bad.append(("symlink_in_original", repack(entries, overrides={original_name: link})))
    # Metadata-only ZIP bombs are rejected before any individual chapter read.
    manifest_object = manifest_of(entries)
    manifest_object["padding"] = "A" * (2 * 1024 * 1024)
    bad.append(("manifest_over_size", repack(replace(
        entries, "manifest.json", encode_manifest(manifest_object),
    ))))
    return bad


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: compare_rust_full_cycle_zip.py /path/to/full-cycle-zip-probe")
    binary = str(Path(sys.argv[1]).resolve(strict=True))
    raw = base64.b64decode(FIXTURE.read_text(encoding="ascii"))
    assert hashlib.sha256(raw).hexdigest() == FIXTURE_SHA

    positive, negative = valid_cases(raw), invalid_cases(raw)
    with tempfile.TemporaryDirectory(prefix="erida-rust-fullcycle-zip-") as directory:
        root = Path(directory)
        for i, (label, data) in enumerate(positive):
            file = root / f"ok-{i}.zip"
            file.write_bytes(data)
            expected = load_full_cycle_source(data, FALLBACK)
            result = subprocess.run(
                [binary, str(file), FALLBACK], text=True, encoding="utf-8",
                capture_output=True, check=True, timeout=15,
            )
            native = json.loads(result.stdout)
            assert native == {"status": "ok", "source": expected}, (
                label, native, expected, result.stderr,
            )
            # Original and working files must remain distinct without title
            # parsing; chapter 9 was manually edited by the desktop operator.
            if i == 0:
                assert [c["chapter_index"] for c in native["source"]["chapters"]] == [7, 9, 10]
                assert native["source"]["missing_positions"] == [[8, 8]]
                assert (native["source"]["chapters"][1]["working_text"]
                        != native["source"]["chapters"][1]["original_text"])

        for i, (label, data) in enumerate(negative):
            file = root / f"bad-{i}.zip"
            file.write_bytes(data)
            try:
                load_full_cycle_source(data, FALLBACK)
            except ExchangeImportError:
                pass
            else:
                raise AssertionError(f"Python accepted invalid archive: {label}")
            result = subprocess.run(
                [binary, str(file), FALLBACK], text=True, encoding="utf-8",
                capture_output=True, check=True, timeout=15,
            )
            assert json.loads(result.stdout) == {"status": "rejected"}, (
                label, result.stdout, result.stderr,
            )
    print(f"rust_full_cycle_python_verified_zip_parity={len(positive)}_PASS")
    print(f"rust_full_cycle_corrupt_manifest_security_rejections={len(negative)}_PASS")
    print("rust_full_cycle_source_boundaries=MANIFEST_ONLY")
    print("rust_full_cycle_sqlite_writes=ZERO")
    print("rust_full_cycle_source_network_calls=ZERO")
    print("rust_full_cycle_gemini_and_drive_calls=ZERO")


if __name__ == "__main__":
    main()
