"""Load a Suite from a Python file path.

Shared by the CLI and the server. The import-linter contract lets these
top-level modules pull this in.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from evalforge.errors import ConfigurationError
from evalforge.pipeline import Suite


def load_suite(path: str | Path, *, attr: str = "suite") -> Suite:
    """Import ``path`` and return the :class:`Suite` named ``attr``.

    The file is loaded as a one-off module under a generated name so we
    don't pollute ``sys.modules`` with the user's path.
    """
    p = Path(path).resolve()
    if not p.exists():
        raise ConfigurationError(f"Suite file not found: {p}", context={"path": str(p)})

    mod_name = f"_evalforge_user_suite_{abs(hash(str(p))) & 0xFFFFFFFF:x}"
    spec = importlib.util.spec_from_file_location(mod_name, p)
    if spec is None or spec.loader is None:
        raise ConfigurationError(f"Cannot import {p}", context={"path": str(p)})
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:  # pragma: no cover — surfaces user code errors
        raise ConfigurationError(
            f"Error importing suite from {p}: {type(e).__name__}: {e}",
            context={"path": str(p)},
        ) from e

    suite = getattr(module, attr, None)
    if not isinstance(suite, Suite):
        raise ConfigurationError(
            f"Attribute {attr!r} in {p} is not a Suite",
            context={"path": str(p), "attr": attr, "got": type(suite).__name__},
        )
    return suite


__all__ = ["load_suite"]
