// Shared helpers for the batcher integration tests.
//
// Lives under tests/common/ (as mod.rs, NOT tests/common.rs) so cargo treats it
// as a module that the test crates `mod common;`-import, rather than compiling
// it as its own test binary.

use avsa_batcher::{config::BatcherConfig, vit_client::Attribute};

/// Build a `BatcherConfig` with small values so tests run fast.
///
/// `num_drain_tasks` is not exercised by the integration tests (they spawn drain
/// tasks manually via `run_drain_task()`); only `main.rs` reads it to decide how
/// many to spawn. It is set to 1 here purely to satisfy the struct.
pub fn test_config(max_batch_size: usize, max_wait_ms: u64) -> BatcherConfig {
    BatcherConfig {
        max_batch_size,
        max_wait_ms,
        vit_service_url: "http://localhost:9999/embed".to_string(),
        vit_timeout_ms: 30_000,
        num_drain_tasks: 1,
    }
}

/// Build a plausible per-image `Attribute` keyed off the image's first byte, so
/// distinct images yield distinct attributes. Used to verify the batch fan-out
/// routes each caller's own result back to it (a mis-mapping yields the wrong
/// category/colour for the caller's image).
pub fn attribute_for_image(image: &[u8]) -> Attribute {
    let key = image.first().copied().unwrap_or(0);
    Attribute {
        category: format!("category-{key}"),
        colour: format!("colour-{key}"),
        category_confidence: key as f32,
        colour_confidence: (key as f32) + 0.5,
    }
}
