"""Explicit model device selection.

The model has use_fp16/use_compile knobs but no explicit device
placement; this module adds it. resolve_device is a pure, config-driven,
env-overridable decision over the three device tiers AVSA targets:

- cpu  - the committed default; CI and CPU-only local runs are unaffected.
- mps  - Apple Silicon GPU for local development.
- cuda - the prod tier, selected via AVSA_MODEL_DEVICE=cuda.

Precedence mirrors the other AVSA_* knobs: the AVSA_MODEL_DEVICE env var
beats the [model] device config value, which beats the cpu default. An
unknown value fails fast with :class:DeviceError rather than silently guessing
a device - "in the face of ambiguity, refuse the temptation to guess."

This module deliberately does NOT import torch, so it stays importable in the
stub/CI env (mirroring how avsa_model.heads keeps numpy out of the import
path). VitEmbedder calls :func:resolve_device and moves the model/inputs
onto the resolved device in real mode only.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

VALID_DEVICES: frozenset[str] = frozenset({"cpu", "mps", "cuda"})

DEVICE_ENV_VAR = "AVSA_MODEL_DEVICE"

_DEFAULT_DEVICE = "cpu"


class DeviceError(Exception):
    """Raised when an invalid device is configured or requested.

    A typed domain error so a misconfigured device fails fast at the resolution
    boundary with a clear message, rather than as an opaque torch error deep in
    model loading.
    """


def resolve_device(config: dict[str, Any], *, env: Mapping[str, str]) -> str:
    """Resolve the device to place the model on (cpu/mps/cuda).

    Precedence (highest first):

    1. env[AVSA_MODEL_DEVICE] - the env override (prod sets this to cuda).
    2. config["model"]["device"] - the committed config default.
    3. cpu - the safe fallback when neither is set.

    Args:
        config: a parsed config/avsa.toml mapping (e.g. from tomllib).
        env: the environment mapping (pass os.environ in production; an
            explicit dict in tests so the precedence is deterministic).

    Returns:
        One of "cpu", "mps", "cuda".

    Raises:
        DeviceError: if the resolved value is not a valid device tier.
    """
    env_device = env.get(DEVICE_ENV_VAR)
    if env_device is not None:
        return _validate(env_device, source=DEVICE_ENV_VAR)

    config_device = config.get("model", {}).get("device")
    if config_device is not None:
        return _validate(str(config_device), source="[model] device")

    return _DEFAULT_DEVICE


def _validate(device: str, *, source: str) -> str:
    if device not in VALID_DEVICES:
        raise DeviceError(
            f"{source} = {device!r} is not a valid device; expected one of {sorted(VALID_DEVICES)}"
        )
    return device
