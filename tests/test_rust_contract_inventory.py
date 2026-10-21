"""M0 executable route inventory must fail closed before Rust API cutover."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/rust_contract_inventory.py"
SPEC = importlib.util.spec_from_file_location("rust_contract_inventory", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
inventory = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inventory)


def _route(report: dict, method: str, path: str) -> dict:
    return next(row for row in report["python_routes"]
                if row["method"] == method and row["path"] == path)


def test_m0_manifest_matches_the_existing_architecture_ratchet():
    report = inventory.build_inventory()
    assert inventory.validate_inventory(report) == []
    assert report["summary"]["python_api_methods"] >= 70
    assert report["summary"]["rust_read_only_methods"] >= 9
    assert report["summary"]["rust_disposable_write_methods"] >= 6
    assert report["summary"]["python_missing_from_rust"]
    assert not report["summary"]["rust_not_in_python"]
    assert not report["summary"]["full_feature_equivalence"]
    assert not report["summary"]["production_rust_writer_enabled"]
    assert "/app-static" in report["static_mounts"]


def test_m0_auth_and_cookie_write_semantics_are_explicit():
    data = inventory.build_inventory()
    assert _route(data, "GET", "/healthz")["auth"] == "public_health"
    assert _route(data, "GET", "/v1/me")["auth"] == "member"
    assert _route(data, "GET", "/v1/admin/members")["auth"] == "administrator"
    assert _route(data, "POST", "/v1/admin/members")["auth"] == "administrator"
    assert _route(data, "POST", "/v1/books")["cookie_csrf"] == (
        "verified_by_dependency_for_cookie_writes"
    )
    assert _route(data, "POST", "/v1/session")["auth"] == (
        "public_login_with_origin_and_throttle"
    )
    assert _route(data, "POST", "/v1/session/logout")["auth"] == (
        "browser_session_in_handler"
    )
    assert _route(data, "POST", "/v1/session/logout")["cookie_csrf"] == (
        "manual_handler_review_required"
    )


def test_m0_rust_mutations_cannot_be_mistaken_for_live_methods():
    report = inventory.build_inventory()
    for row in report["rust_routes"]:
        if row["method"] != "GET":
            assert row["mode"] == "disposable_test_writes"
        assert row["behavior_parity"] == "not_proven_by_route_inventory"
    assert all(
        row["runtime_errors"] == "needs_differential_tests"
        and row["response_parity"] == "unproven"
        for row in report["python_routes"]
    )


def test_m0_drift_rejects_unreviewed_path_and_unsafe_rust_write():
    report = inventory.build_inventory()
    modified = json.loads(json.dumps(report))
    modified["rust_routes"].append({
        "method": "POST", "path": "/v1/jobs",
        "mode": "read_only_candidate", "behavior_parity": "unproven",
    })
    assert any("unsafe write" in e for e in inventory.validate_inventory(modified))
    modified = json.loads(json.dumps(report))
    modified["python_routes"].append({
        **_route(report, "GET", "/v1/me"),
        "path": "/v1/unreviewed", "auth": "UNREVIEWED",
    })
    problems = inventory.validate_inventory(modified)
    assert any("differs" in p for p in problems)
    assert any("Unreviewed" in p for p in problems)


def test_m0_inventory_contains_no_token_or_chapter_text():
    report = inventory.build_inventory()
    serialized = json.dumps(report, ensure_ascii=False)
    assert "synthetic-local-key" not in serialized
    assert "contract-admin" not in serialized
    assert "chapter_text" not in serialized
