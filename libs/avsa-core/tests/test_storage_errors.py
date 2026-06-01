"""Failing tests for housekeeping fold-in F11 — NotFound moved to storage/errors.py.

Authored at step 2A-i (pre-implementation) for the Phase 2b housekeeping fold-in
F11 from . The new canonical location `avsa_core.storage.errors.NotFound`
does not yet exist; we import inside a try/except so collection succeeds and each
test fails with a meaningful assertion failure (pytest.fail) — per
docs/agents/standards/testing.md § "Test-first protocol".

Behaviour under test:
- `from avsa_core.storage.errors import NotFound` (NEW canonical path)
- `from avsa_core.storage import NotFound` (BACK-COMPAT re-export)
- Both imports resolve to the SAME class object — there must be only one definition.
"""

from __future__ import annotations

import pytest

try:
    from avsa_core.storage.errors import NotFound as _ErrorsNotFound  # noqa: F401

    _ERRORS_MODULE_AVAILABLE = True
except ImportError:
    _ERRORS_MODULE_AVAILABLE = False


def _require_errors_module() -> None:
    if not _ERRORS_MODULE_AVAILABLE:
        pytest.fail(
            "avsa_core.storage.errors.NotFound not implemented yet — expected during "
            "2A-i pre-implementation. Implement per issues/072-061-housekeeping.md § F11 "
            "(extract NotFound from storage/__init__.py into storage/errors.py to break "
            "the fragile bottom-of-file deferred-import cycle)."
        )


def test_NotFound_importable_from_avsa_core_storage_errors() -> None:
    """NotFound must be importable from the new canonical path storage.errors."""
    _require_errors_module()
    from avsa_core.storage.errors import NotFound

    assert isinstance(NotFound, type), (
        f"avsa_core.storage.errors.NotFound must be a class; got {type(NotFound).__name__}"
    )
    assert issubclass(NotFound, Exception), (
        "avsa_core.storage.errors.NotFound must subclass Exception (domain error type)."
    )


def test_NotFound_importable_from_avsa_core_storage() -> None:
    """NotFound must remain importable from avsa_core.storage (back-compat re-export)."""
    _require_errors_module()
    from avsa_core.storage import NotFound

    assert isinstance(NotFound, type), (
        f"avsa_core.storage.NotFound must remain a class (back-compat); "
        f"got {type(NotFound).__name__}"
    )


def test_NotFound_from_both_paths_is_same_class() -> None:
    """Both import paths must resolve to the SAME class — no second copy.

    This is the load-bearing assertion for F11: if two distinct NotFound classes
    exist, `except NotFound` at one call site silently misses exceptions raised
    by the other. The cycle break must re-export, not redefine.
    """
    _require_errors_module()
    from avsa_core.storage import NotFound as N1  # noqa: N814 — alias compared with `is`
    from avsa_core.storage.errors import NotFound as N2  # noqa: N814 — alias compared with `is`

    assert N1 is N2, (
        "avsa_core.storage.NotFound and avsa_core.storage.errors.NotFound must be the "
        "SAME class object (re-export, not redefinition). "
        f"Got two distinct classes: N1 id={id(N1)}, N2 id={id(N2)}, "
        f"N1 module={N1.__module__!r}, N2 module={N2.__module__!r}."
    )
