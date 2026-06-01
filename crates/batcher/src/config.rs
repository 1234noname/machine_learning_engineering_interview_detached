// Config loader for the batcher crate.
//
// Reads `config/avsa.toml` relative to the binary's working directory and
// deserialises the `[batcher]` section into `BatcherConfig`. Falls back to
// `BatcherConfig::default()` when the file is absent (so tests that do not
// place a config file on disk still compile and run).
//
// The file is read once at startup and the result is passed to `BatchQueue`
// via constructor argument. No global state, no lazy_static.

use serde::Deserialize;
use std::fs;

/// Configuration for the batcher service, sourced from `config/avsa.toml`.
#[derive(Debug, Clone, Deserialize)]
pub struct BatcherConfig {
    /// Flush the batch when this many items are queued.
    pub max_batch_size: usize,

    /// Flush the batch after this many milliseconds even if not full.
    pub max_wait_ms: u64,

    /// HTTP URL of the upstream ViT model service `/embed` endpoint.
    pub vit_service_url: String,

    /// Total timeout in milliseconds for a single model HTTP call.
    ///
    /// Set generously for remote GPU backends (Modal, GKE) that may incur a
    /// cold-start penalty (~10–15 s). The default covers a Modal A10G cold
    /// start plus inference headroom. This is independent of `max_wait_ms`
    /// (the batch-fill wait), which is intentionally short (~50 ms).
    pub vit_timeout_ms: u64,

    /// Number of concurrent background drain tasks to spawn.
    ///
    /// Each task independently polls the queue and dispatches one batch at a
    /// time to the ViT service, allowing N batches to be in-flight
    /// simultaneously. Set to match the number of warm GPU containers
    /// (`min_containers` on the Modal AvsaModel class) so every container
    /// stays busy. Defaults to 1 (safe for single-container deployments).
    pub num_drain_tasks: usize,
}

impl Default for BatcherConfig {
    fn default() -> Self {
        BatcherConfig {
            max_batch_size: 8,
            max_wait_ms: 50,
            vit_service_url: "http://localhost:8090/embed".to_string(),
            vit_timeout_ms: 30_000,
            num_drain_tasks: 1,
        }
    }
}

/// Read `config/avsa.toml` and return the `[batcher]` section.
///
/// Returns `BatcherConfig::default()` if the file does not exist so that
/// integration tests that run without a repo-root config file still work.
///
/// # Errors
///
/// Panics if the file exists but cannot be parsed - a mis-configured deploy
/// should fail loudly at startup rather than silently using defaults.
pub fn load_batcher_config() -> BatcherConfig {
    let path = "config/avsa.toml";

    let raw = match fs::read_to_string(path) {
        Ok(content) => content,
        Err(_) => {
            tracing::warn!(
                config_path = path,
                "config file not found - using BatcherConfig defaults"
            );
            return BatcherConfig::default();
        }
    };

    parse_batcher_config(&raw)
}

/// Parse the `[batcher]` section out of a raw `config/avsa.toml` string.
///
/// Returns `BatcherConfig::default()` when the `[batcher]` table is absent so a
/// minimal config still boots. Panics if the TOML is malformed or the
/// `[batcher]` table is present but invalid - e.g. a field of the wrong type,
/// or (because no field carries a serde default) a newly-added required field
/// missing. A mis-configured deploy must fail loudly at startup, not silently
/// fall back to defaults.
///
/// Split out from [`load_batcher_config`] so the parse + section-extraction
/// logic is unit-testable without touching the filesystem.
fn parse_batcher_config(raw: &str) -> BatcherConfig {
    let doc: toml::Value =
        toml::from_str(raw).unwrap_or_else(|e| panic!("failed to parse config/avsa.toml: {e}"));

    doc.get("batcher")
        .cloned()
        .map(|v| {
            v.try_into::<BatcherConfig>()
                .unwrap_or_else(|e| panic!("invalid [batcher] section in config/avsa.toml: {e}"))
        })
        .unwrap_or_else(|| {
            tracing::warn!("[batcher] section absent - using BatcherConfig defaults");
            BatcherConfig::default()
        })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_a_full_batcher_section_including_num_drain_tasks() {
        // If a field is added to BatcherConfig without a serde default, this
        // round-trip panics until the new field is represented in the TOML.
        let raw = r#"
            [batcher]
            max_batch_size = 24
            max_wait_ms = 50
            vit_service_url = "http://model:8090/embed"
            vit_timeout_ms = 30000
            num_drain_tasks = 6
        "#;

        let cfg = parse_batcher_config(raw);

        assert_eq!(cfg.max_batch_size, 24);
        assert_eq!(cfg.max_wait_ms, 50);
        assert_eq!(cfg.vit_service_url, "http://model:8090/embed");
        assert_eq!(cfg.vit_timeout_ms, 30_000);
        assert_eq!(cfg.num_drain_tasks, 6);
    }

    #[test]
    fn absent_batcher_section_falls_back_to_defaults() {
        // Valid TOML, but no [batcher] table → defaults (the service still boots).
        let cfg = parse_batcher_config("[other]\nkey = 1\n");
        let default = BatcherConfig::default();
        assert_eq!(cfg.max_batch_size, default.max_batch_size);
        assert_eq!(cfg.num_drain_tasks, default.num_drain_tasks);
        assert_eq!(cfg.vit_service_url, default.vit_service_url);
    }

    #[test]
    #[should_panic(expected = "invalid [batcher] section")]
    fn invalid_batcher_section_panics_loudly() {
        // Wrong type for max_batch_size → fail at startup, not silent-default.
        let _ = parse_batcher_config("[batcher]\nmax_batch_size = \"nope\"\n");
    }
}
