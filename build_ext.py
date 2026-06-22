"""Build optional cython modules."""

import logging
import os
from distutils.command.build_ext import build_ext
from typing import Any

try:
    from setuptools import Extension
except ImportError:
    from distutils.core import Extension


_LOGGER = logging.getLogger(__name__)

TO_CYTHONIZE = [
    "src/habluetooth/advertisement_tracker.py",
    "src/habluetooth/auto_scheduler.py",
    "src/habluetooth/base_scanner.py",
    "src/habluetooth/manager.py",
    "src/habluetooth/models.py",
    "src/habluetooth/scanner_bleak.py",
    "src/habluetooth/channels/bluez.py",
]

EXTENSIONS = [
    Extension(
        ext.removeprefix("src/").removesuffix(".py").replace("/", "."),
        [ext],
        language="c",
        extra_compile_args=["-O3", "-g0"],
    )
    for ext in TO_CYTHONIZE
]


class BuildExt(build_ext):
    """Build extension."""

    def build_extensions(self) -> None:
        """Build extensions."""
        if self.parallel is None:  # type: ignore[has-type, unused-ignore]
            self.parallel = os.cpu_count() or 1
        try:
            super().build_extensions()
        except Exception as ex:  # nosec  # noqa: BLE001
            # Cython is optional; any compile failure (missing C compiler,
            # platform mismatch, etc.) should fall back to the pure-Python
            # install rather than break the build.
            _LOGGER.debug("Failed to build extensions: %s", ex, exc_info=True)


def build(setup_kwargs: Any) -> None:
    """Build optional cython modules."""
    if os.environ.get("SKIP_CYTHON"):
        return
    try:
        # Cython is optional; defer the import so the SKIP_CYTHON
        # branch above never has to find it on sys.path.
        from Cython.Build import cythonize  # noqa: PLC0415

        setup_kwargs.update(
            {
                "ext_modules": cythonize(
                    EXTENSIONS,
                    compiler_directives={"language_level": "3"},  # Python 3
                    annotate=bool(os.environ.get("CYTHON_ANNOTATE")),
                ),
                "cmdclass": {"build_ext": BuildExt},
            }
        )
        setup_kwargs["exclude_package_data"] = {
            pkg: ["*.c"] for pkg in setup_kwargs["packages"]
        }
    except Exception:
        if os.environ.get("REQUIRE_CYTHON"):
            raise
