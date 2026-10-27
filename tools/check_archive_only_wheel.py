"""Build both Erida wheel variants and accept the archive-only installed wheel.

Run in public Ararra CI (Python 3.11/3.12), not on the VPS. The fixture is
synthetic and comes from the unmodified Full Cycle App archive exporter.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "full_cycle_actual_exporter_20261017.zip.b64"


def command(argv, *, cwd: Path, env: dict[str, str] | None = None) -> None:
    subprocess.run(argv, cwd=cwd, env=env, check=True, timeout=240)


def build_variant(root: Path, name: str, archive_only: bool) -> Path:
    source = root / name / "project"
    output = root / name / "dist"
    source.mkdir(parents=True)
    output.mkdir(parents=True)
    for file in ("pyproject.toml", "setup.py"):
        shutil.copy2(ROOT / file, source / file)
    shutil.copytree(
        ROOT / "src", source / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    env = dict(os.environ)
    env["CHAPTERA_ARCHIVE_ONLY_BUILD"] = "1" if archive_only else "0"
    command(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-cache-dir",
         "--wheel-dir", str(output), str(source)],
        cwd=root, env=env,
    )
    wheels = list(output.glob("*.whl"))
    assert len(wheels) == 1, wheels
    return wheels[0]


def inspect_wheels(full: Path, slim: Path) -> None:
    with zipfile.ZipFile(full) as z:
        original = set(z.namelist())
    with zipfile.ZipFile(slim) as z:
        lean = set(z.namelist())
    keep = (
        "chaptera_server/full_cycle_archive.py",
        "chaptera_server/book_import.py",
        "chaptera_server/import_sessions.py",
        "chaptera_server/api.py",
        "chaptera_server/web_ui/templates/shell.html",
        "chaptera_server/web_ui/static/js/readera.mjs",
    )
    for name in keep:
        assert name in original and name in lean, name
    assert "chaptera_server/source_runtime.py" in original
    assert "chaptera_server/source_runtime.py" not in lean
    assert any(name.startswith("chaptera_server/source_") and name.endswith(".py")
               for name in original)
    assert not any(name.startswith("chaptera_server/source_") and name.endswith(".py")
                   for name in lean)
    assert not any(name.startswith("chaptera_server/http/routers/source_")
                   and name.endswith(".py") for name in lean)
    assert any(name.startswith("chaptera_server/web_ui/static/js/source-")
               and name.endswith(".mjs") for name in original)
    assert not any(name.startswith("chaptera_server/web_ui/static/js/source-")
                   and name.endswith(".mjs") for name in lean)
    assert slim.stat().st_size < full.stat().st_size
    print(f"full_wheel_bytes={full.stat().st_size}")
    print(f"archive_only_wheel_bytes={slim.stat().st_size}")
    print(f"archive_only_wheel_saved_bytes={full.stat().st_size-slim.stat().st_size}")


def check_installed(root: Path, wheel: Path) -> None:
    venv = root / "installed"
    command([sys.executable, "-m", "venv", "--system-site-packages", str(venv)], cwd=root)
    python = venv / "bin" / "python"
    command([str(python), "-m", "pip", "install", "--no-deps", "--no-index",
             str(wheel)], cwd=root)
    fixture = root / "real-exporter-zip.b64"
    shutil.copy2(FIXTURE, fixture)
    probe = root / "probe.py"
    probe.write_text(textwrap.dedent('''
        from pathlib import Path
        import asyncio
        import base64
        import importlib.util
        import sys
        import httpx
        import chaptera_server
        from chaptera_server.api import create_app
        from chaptera_server.config import Settings
        from chaptera_server.storage import QueueStore

        root=Path(sys.argv[1])
        assert str(chaptera_server.__file__).startswith(str(Path(sys.prefix)))
        assert importlib.util.find_spec("chaptera_server.source_runtime") is None
        assert importlib.util.find_spec("chaptera_server.full_cycle_archive") is not None
        settings=Settings(db_path=root / "lean-installed.sqlite3",
                          team_tokens={"operator":"operator-probe"},
                          archive_only=True)
        app=create_app(settings, QueueStore(settings.db_path))
        assert not hasattr(app.state, "public_source_fetcher")
        assert not hasattr(app.state,"source_job_store")
        raw=base64.b64decode((root / "real-exporter-zip.b64").read_text())
        async def run():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test.invalid"
            ) as c:
                response=await c.post(
                    "/v1/imports/preview",
                    files={"file":("fullcycle.zip",raw,"application/zip")},
                    headers={"X-API-Key":"operator-probe"})
                assert response.status_code==200,response.text
                assert response.json()["chapter_count"]==3
                page=await c.get("/app/import")
                assert page.status_code==200
                assert 'id="source-import"' not in page.text
        asyncio.run(run())
        print("archive_only_installed_wheel_zip_preview=PASS")
        print("archive_only_installed_wheel_crawler_absent=PASS")
    '''), encoding="utf-8")
    env = dict(os.environ)
    env["CHAPTERA_ARCHIVE_ONLY"] = "1"
    env.pop("PYTHONPATH", None)
    command([str(python), str(probe), str(root)], cwd=root, env=env)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="erida-archive-only-build-") as directory:
        root = Path(directory)
        full = build_variant(root, "full", archive_only=False)
        slim = build_variant(root, "archive-only", archive_only=True)
        inspect_wheels(full, slim)
        check_installed(root, slim)
    print("archive_only_release_variant=PASS")


if __name__ == "__main__":
    main()
