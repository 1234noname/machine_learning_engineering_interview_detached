"""Domain error types for the storage abstraction.

Extracted from storage/__init__.py so both the package
__init__ and concrete backends import from a single leaf module rather than
the package, eliminating a bottom-of-file deferred-import cycle. There is
exactly one NotFound definition; avsa_core.storage re-exports it for
back-compat.
"""

from __future__ import annotations


class NotFound(Exception):  # noqa: N818 — domain error name fixed by  spec.
    """Raised by a StorageBackend when the requested object key is absent."""
