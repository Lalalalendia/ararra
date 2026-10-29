"""Optional lean source-archive-only wheel (full developer builds unchanged).

Build with CHAPTERA_ARCHIVE_ONLY_BUILD=1 to exclude site-crawler Python
modules and browser scripts. Always run the installed wheel with
CHAPTERA_ARCHIVE_ONLY=1; the default full build remains backwards compatible.

The Full Cycle App ZIP parser is *not* a site crawler. It stays in the wheel.
"""
from __future__ import annotations

import os

from setuptools import setup
from setuptools.command.build_py import build_py

choice = os.environ.get("CHAPTERA_ARCHIVE_ONLY_BUILD", "0")
if choice not in {"0", "1"}:
    raise ValueError("CHAPTERA_ARCHIVE_ONLY_BUILD must be 0 or 1")


class ArchiveOnlyBuildPy(build_py):
    def find_package_modules(self, package, package_dir):
        modules = super().find_package_modules(package, package_dir)
        if package in {"chaptera_server", "chaptera_server.http.routers"}:
            return [
                record for record in modules
                if not record[1].startswith("source_")
            ]
        return modules

    def find_data_files(self, package, src_dir):
        # Setuptools may classify static folders as implicit namespace packages.
        # Match their source directory, not just the chaptera_server package.
        from pathlib import Path
        files = super().find_data_files(package, src_dir)
        directory = Path(src_dir).as_posix().replace("\\\\", "/")
        if not directory.endswith("web_ui/static/js"):
            return [
                name for name in files
                if "web_ui/static/js/source-" not in name.replace("\\\\", "/")
            ]
        return [name for name in files if not Path(name).name.startswith("source-")]

    def build_package_data(self):
        # Final protection if package_data arrives via a namespace package
        # that bypasses find_data_files. Remove only crawler JS from build_lib;
        # import-model, archive-review, editor and all other JS stay untouched.
        super().build_package_data()
        from pathlib import Path
        root = Path(self.build_lib) / "chaptera_server/web_ui/static/js"
        if root.is_dir():
            for file in root.glob("source-*.mjs"):
                file.unlink()
            for file in root.glob("source-*.js"):
                file.unlink()


if choice == "1":
    setup(
        cmdclass={"build_py": ArchiveOnlyBuildPy},
        exclude_package_data={
            "chaptera_server": ["web_ui/static/js/source-*.mjs",
                                "web_ui/static/js/source-*.js"],
        },
    )
else:
    setup()
