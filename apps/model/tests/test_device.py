"""The model has use_fp16/use_compile knobs but no explicit device
placement. This module pins the contract of the config-driven, env-overridable
device resolver in avsa_model.device:

- [model] device in config/avsa.toml is the configured default
  (cpu locally so CI/stub stay CPU; cuda in prod via the env override).
- AVSA_MODEL_DEVICE env var overrides the config value (env beats config,
  the same precedence as the other AVSA_* knobs).
- Only cpu/mps/cuda are accepted - an unknown value fails fast at the
  boundary rather than silently guessing a device.
"""

from __future__ import annotations

import pytest

from avsa_model.device import (
    VALID_DEVICES,
    DeviceError,
    resolve_device,
)

# ---------------------------------------------------------------------------
# Generated-config wiring - the 'local' dev profile selects a valid device (mps)
# ---------------------------------------------------------------------------


def test_local_profile_device_is_mps(local_config: dict[str, object]) -> None:
    """The generated 'local' dev profile selects the Apple-GPU device (mps).

    local_config is generated in-memory from the committed base +
    avsa.local.toml (config-gen's merge), NOT read from the dev's gitignored
    config/avsa.toml - so this is deterministic regardless of which profile was
    last generated locally. avsa.local.toml sets device=mps (Apple GPU for local
    dev); it must be a valid tier the resolver accepts.
    """
    model_cfg = local_config.get("model", {})
    assert isinstance(model_cfg, dict)
    device = model_cfg.get("device")
    assert device in VALID_DEVICES, (
        f"[model] device must be a valid tier {sorted(VALID_DEVICES)}; got {device!r}"
    )
    assert device == "mps", (
        f"the generated local profile must select device=mps (Apple GPU); got {device!r}"
    )


# ---------------------------------------------------------------------------
# resolve_device - config value used when env is absent
# ---------------------------------------------------------------------------


def test_resolve_device_uses_config_when_env_absent() -> None:
    config = {"model": {"device": "mps"}}
    assert resolve_device(config, env={}) == "mps"


def test_resolve_device_defaults_to_cpu_when_unset() -> None:
    """Neither config nor env set → cpu (the safe default, not a guess)."""
    assert resolve_device({"model": {}}, env={}) == "cpu"
    assert resolve_device({}, env={}) == "cpu"


# ---------------------------------------------------------------------------
# resolve_device - AVSA_MODEL_DEVICE env overrides config
# ---------------------------------------------------------------------------


def test_env_overrides_config() -> None:
    """AVSA_MODEL_DEVICE beats the [model] device config value."""
    config = {"model": {"device": "cpu"}}
    resolved = resolve_device(config, env={"AVSA_MODEL_DEVICE": "cuda"})
    assert resolved == "cuda", (
        "AVSA_MODEL_DEVICE=cuda must override [model] device=cpu (env beats config)"
    )


# ---------------------------------------------------------------------------
# resolve_device - unknown values fail fast (no silent guessing)
# ---------------------------------------------------------------------------


def test_unknown_config_device_raises() -> None:
    with pytest.raises(DeviceError, match="gpu"):
        resolve_device({"model": {"device": "gpu"}}, env={})


def test_unknown_env_device_raises() -> None:
    with pytest.raises(DeviceError, match="tpu"):
        resolve_device({"model": {"device": "cpu"}}, env={"AVSA_MODEL_DEVICE": "tpu"})
