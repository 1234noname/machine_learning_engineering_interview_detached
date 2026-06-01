"""Conftest for DB-backed integration tests.

Loads the `seeded_catalog_db` and `catalog_seed_timing` fixtures from
``tests/fixtures/catalog.py`` so every test under ``tests/integration/``
can depend on them.

The fixture module is loaded by path rather than by package import
because ``tests/`` is not a package in this repo (no ``__init__.py``)
and pytest forbids ``pytest_plugins`` in non-top-level conftests. The
load is best-effort — when the fixture module isn't present (TDD
bootstrap) every dependent test fails with the usual "fixture not
found" error rather than aborting collection.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_FIXTURE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "catalog.py"

if _FIXTURE_PATH.is_file():
    _spec = importlib.util.spec_from_file_location(
        "tests_fixtures_catalog", _FIXTURE_PATH
    )
    assert _spec is not None and _spec.loader is not None
    _module = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = _module
    _spec.loader.exec_module(_module)
    # Re-export the two fixtures so pytest picks them up.
    seeded_catalog_db = _module.seeded_catalog_db
    catalog_seed_timing = _module.catalog_seed_timing
