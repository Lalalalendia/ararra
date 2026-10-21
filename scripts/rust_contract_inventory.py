#!/usr/bin/env python3
"""M0 executable Python HTTP inventory vs candidate Rust routes.

This is a coverage map, NOT behavioral equivalence or a production switch.
Never opens the production database or contacts a provider/source site.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import tempfile
from pathlib import Path

from fastapi.routing import APIRoute
from starlette.routing import Mount

from chaptera_server.api import create_app
from chaptera_server.config import Settings
from chaptera_server.http.dependencies import require_admin, require_member
from chaptera_server.storage import QueueStore

ROOT = Path(__file__).resolve().parents[1]
RUST_API = ROOT / "rust/crates/erida-api/src/lib.rs"
ARCHITECTURE = ROOT / "tests/test_architecture.py"

# Session handlers enforce their own cookie/session login rules, unlike v1
# member/admin routes that use standard recursive FastAPI dependencies.
MANUAL_AUTH = {
    ("POST", "/v1/session"): "public_login_with_origin_and_throttle",
    ("GET", "/v1/session"): "browser_session_in_handler",
    ("POST", "/v1/session/logout"): "browser_session_in_handler",
}
RUST_ROUTE = re.compile(
    r'\.route\(\s*"(?P<path>/[^"]*)"\s*,\s*'
    r'(?:(?:axum::routing::)?(?P<method>get|post|put|patch|delete))\s*\(',
    re.MULTILINE,
)


def _param(field: object) -> dict:
    annotation = getattr(getattr(field, "field_info", None), "annotation", None)
    if annotation is None:
        annotation = getattr(field, "type_", None)
    kind = getattr(annotation, "__name__", None) or str(annotation)
    return {
        "name": str(getattr(field, "name", "")),
        "type": kind,
        "required": bool(getattr(field, "required", False)),
    }


def _dependency_calls(dependant: object) -> set[object]:
    found = set()
    pending = list(getattr(dependant, "dependencies", ()))
    while pending:
        dep = pending.pop()
        call = getattr(dep, "call", None)
        if call is not None:
            found.add(call)
        pending.extend(getattr(dep, "dependencies", ()))
    return found


def _authorization(method: str, path: str, dependant: object) -> str:
    if (method, path) in MANUAL_AUTH:
        return MANUAL_AUTH[(method, path)]
    deps = _dependency_calls(dependant)
    if require_admin in deps:
        return "administrator"
    if require_member in deps:
        return "member"
    if path == "/healthz":
        return "public_health"
    if path.startswith("/app"):
        return "ui_shell_handler"
    return "UNREVIEWED"


def _request_fields(dependant: object) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for key in ("path_params", "query_params", "header_params", "cookie_params", "body_params"):
        result[key] = sorted(
            (_param(p) for p in getattr(dependant, key, ())),
            key=lambda p: p["name"],
        )
    return result


def _python_routes() -> tuple[list[dict], list[str]]:
    with tempfile.TemporaryDirectory(prefix="erida-rust-contract-") as temp:
        store = QueueStore(Path(temp) / "contract.sqlite3")
        settings = Settings(
            db_path=store.db_path,
            team_tokens={"contract-admin": "synthetic-local-key"},
            admin_member_id="contract-admin",
        )
        app = create_app(settings, store)
        entries: list[dict] = []
        mounts: list[str] = []
        for route in app.routes:
            if isinstance(route, Mount):
                mounts.append(route.path)
            if not isinstance(route, APIRoute):
                continue
            for method in sorted(route.methods or ()):
                path = route.path
                auth = _authorization(method, path, route.dependant)
                entries.append({
                    "method": method,
                    "path": path,
                    "handler": f"{route.endpoint.__module__}.{route.endpoint.__name__}",
                    "auth": auth,
                    "cookie_csrf": (
                        "verified_by_dependency_for_cookie_writes"
                        if auth in {"member", "administrator"}
                        and method not in {"GET", "HEAD", "OPTIONS"}
                        else "manual_handler_review_required"
                        if auth == "browser_session_in_handler"
                        else "not_applicable"
                    ),
                    "success_status_declared": route.status_code or 200,
                    "request_fields": _request_fields(route.dependant),
                    # Declaration inspection cannot establish dynamic error
                    # status, response headers, side effects or byte parity.
                    "runtime_errors": "needs_differential_tests",
                    "side_effects": "needs_review",
                    "response_parity": "unproven",
                })
        return sorted(entries, key=lambda e: (e["path"], e["method"])), sorted(mounts)


def _architecture_routes(path: Path = ARCHITECTURE) -> set[tuple[str, str]]:
    module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for stmt in module.body:
        if isinstance(stmt, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "ROUTE_CONTRACT"
            for target in stmt.targets
        ):
            result = ast.literal_eval(stmt.value)
            if not isinstance(result, set) or not all(
                isinstance(item, tuple) and len(item) == 2
                and all(isinstance(value, str) for value in item)
                for item in result
            ):
                raise ValueError("ROUTE_CONTRACT must be a literal set of method/path pairs")
            return result
    raise ValueError("Static Python architecture ROUTE_CONTRACT not found")


def _rust_routes(path: Path = RUST_API) -> list[dict]:
    source = path.read_text(encoding="utf-8")
    marker = "if state.test_writes_enabled {"
    position = source.find(marker)
    if position < 0:
        raise ValueError("Rust disposable writer fence marker has disappeared")
    found = []
    for match in RUST_ROUTE.finditer(source):
        method = match.group("method").upper()
        mode = "disposable_test_writes" if match.start() > position else "read_only_candidate"
        found.append({
            "method": method,
            "path": match.group("path"),
            "mode": mode,
            "behavior_parity": "not_proven_by_route_inventory",
        })
    return sorted(found, key=lambda e: (e["path"], e["method"]))


def build_inventory() -> dict:
    python, mounts = _python_routes()
    rust = _rust_routes()
    python_api = {
        (r["method"], r["path"])
        for r in python
        if r["path"] == "/healthz" or r["path"].startswith("/v1/")
    }
    rust_set = {(r["method"], r["path"]) for r in rust}
    live_reads = {(r["method"], r["path"]) for r in rust
                  if r["mode"] == "read_only_candidate"}
    test_writes = {(r["method"], r["path"]) for r in rust
                   if r["mode"] == "disposable_test_writes"}
    return {
        "schema": 1,
        "scope": "declarations_only__not_behavioral_parity",
        "python_routes": python,
        "static_mounts": mounts,
        "rust_routes": rust,
        "summary": {
            "python_api_methods": len(python_api),
            "python_ui_routes": len(python) - len(python_api),
            "rust_candidate_methods": len(rust_set),
            "rust_read_only_methods": len(live_reads),
            "rust_disposable_write_methods": len(test_writes),
            "python_missing_from_rust": [
                {"method": method, "path": path}
                for method, path in sorted(python_api - rust_set)
            ],
            "rust_not_in_python": [
                {"method": method, "path": path}
                for method, path in sorted(rust_set - python_api)
            ],
            "full_feature_equivalence": False,
            "production_rust_writer_enabled": False,
        },
    }


def validate_inventory(report: dict) -> list[str]:
    problems: list[str] = []
    python = report["python_routes"]
    p_pairs = [(r["method"], r["path"]) for r in python]
    rust = report["rust_routes"]
    r_pairs = [(r["method"], r["path"]) for r in rust]
    expected = _architecture_routes()
    api = {pair for pair in p_pairs if pair[1] == "/healthz"
           or pair[1].startswith("/v1/")}
    if len(set(p_pairs)) != len(p_pairs):
        problems.append("Duplicate Python method/path handler")
    if api != expected:
        problems.append(
            "Python inventory differs from architecture route ratchet: "
            f"added={sorted(api - expected)!r} missing={sorted(expected - api)!r}"
        )
    if len(set(r_pairs)) != len(r_pairs):
        problems.append("Duplicate Rust method/path route")
    for row in rust:
        pair = (row["method"], row["path"])
        if pair not in api:
            problems.append(f"Rust route has no Python reference: {pair}")
        if row["method"] != "GET" and row["mode"] != "disposable_test_writes":
            problems.append(f"Rust unsafe write outside disposable fence: {pair}")
    for row in python:
        if row["auth"] == "UNREVIEWED":
            problems.append(f"Unreviewed Python authorization: {(row['method'], row['path'])}")
    if "/app-static" not in report["static_mounts"]:
        problems.append("Static UI mount absent from inventory")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="Reject contract drift and unsafe Rust route exposure")
    parser.add_argument("--json", type=Path,
                        help="Write metadata-only machine inventory JSON")
    args = parser.parse_args()
    report = build_inventory()
    problems = validate_inventory(report)
    if args.json is not None:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print("rust_m0_python_api_methods=" + str(report["summary"]["python_api_methods"]))
    print("rust_m0_rust_read_only_methods=" + str(report["summary"]["rust_read_only_methods"]))
    print("rust_m0_rust_disposable_write_methods=" + str(
        report["summary"]["rust_disposable_write_methods"]
    ))
    print("rust_m0_python_missing_from_rust=" + str(
        len(report["summary"]["python_missing_from_rust"])
    ))
    print("rust_m0_behavioral_parity_proven=NO")
    print("rust_m0_production_sqlite_touched=NO")
    for error in problems:
        print("rust_m0_gate_error=" + error)
    return 2 if args.check and problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
