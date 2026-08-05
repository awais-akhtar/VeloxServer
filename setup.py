from __future__ import annotations

import os
from pathlib import Path

from setuptools import setup
from setuptools.dist import Distribution


NATIVE_LIBRARY_NAMES = {
    "libveloxcore.so",
    "libveloxcore.dylib",
    "veloxcore.dll",
}


class BinaryDistribution(Distribution):
    def has_ext_modules(self) -> bool:
        return True


def should_build_binary_wheel() -> bool:
    if os.environ.get("VELOXSERVER_BINARY_WHEEL") == "1":
        return True

    native_dir = Path(__file__).parent / "src" / "veloxserver" / "native_libs"
    return any((native_dir / name).exists() for name in NATIVE_LIBRARY_NAMES)


setup_kwargs = {}
if should_build_binary_wheel():
    setup_kwargs["distclass"] = BinaryDistribution


setup(**setup_kwargs)
