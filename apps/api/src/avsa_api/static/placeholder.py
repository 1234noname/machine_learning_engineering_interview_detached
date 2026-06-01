"""Placeholder image served for missing catalog objects."""

from __future__ import annotations

import base64

# 1x1 transparent PNG (RGBA)
_PLACEHOLDER_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk"
    "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

PLACEHOLDER_PNG: bytes = base64.b64decode(_PLACEHOLDER_PNG_B64)
"""Decoded placeholder PNG bytes; starts with the PNG magic header."""
