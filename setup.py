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
        files = super().find_data_files(package, src_dir)
        if package != "chaptera_server":
            return files
        return [
            path for path in files
            if "/web_ui/static/js/source-" not in path.replace("\\", "/")
            and not path.replace("\\", "/").startswith("web_ui/static/js/source-")
        ]


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
