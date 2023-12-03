"""Build optional cython modules."""

import logging
import os
from distutils.command.build_ext import build_ext
from typing import Any

_LOGGER = logging.getLogger(__name__)


class BuildExt(build_ext):
    """Build extension."""

    def build_extensions(self) -> None:
        """Build extensions."""
        try:
            super().build_extensions()
        except Exception as ex:  # nosec
            _LOGGER.debug("Failed to build extensions: %s", ex, exc_info=True)
            pass


def build(setup_kwargs: Any) -> None:
    """Build optional cython modules."""
    if os.environ.get("SKIP_CYTHON", False):
        return
    try:
        from Cython.Build import cythonize

        setup_kwargs.update(
            {
                "ext_modules": cythonize(
                    ["src/habluetooth/base_scanner.py"],
                    compiler_directives={"language_level": "3"},  # Python 3
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
        pass
