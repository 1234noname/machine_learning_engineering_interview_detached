// Entry point. Responsibilities:
//   - Initialise tracing from RUST_LOG env var (JSON format).
//   - Load BatcherConfig from config/avsa.toml (fallback to defaults).
//   - Construct VitClient and BatchQueue.
//   - Start the single background drain task via tokio::spawn.
//   - Read the bind port from BATCHER_PORT (default 8081).
//   - Wire the full router (health + embed) and serve.
//
// Zero route logic lives here - routes are in lib.rs and src/routes/.

use std::{env, sync::Arc};
use tracing_subscriber::{fmt, EnvFilter};

#[tokio::main]
async fn main() {
    // Initialise tracing with JSON format; filter driven by RUST_LOG.
    // The tracing-opentelemetry bridge is picked up automatically by any
    // subscriber that is registered before spans are emitted.
    fmt()
        .with_env_filter(EnvFilter::from_default_env())
        .json()
        .init();

    // Load config once at startup; pass to BatchQueue via constructor.
    let config = avsa_batcher::config::load_batcher_config();
    tracing::info!(
        max_batch_size = config.max_batch_size,
        max_wait_ms = config.max_wait_ms,
        num_drain_tasks = config.num_drain_tasks,
        vit_service_url = %config.vit_service_url,
        "loaded batcher config"
    );

    // Capture before config is moved into BatchQueue.
    let num_drain_tasks = config.num_drain_tasks;

    // Build the production VitClient.
    let vit_client = Arc::new(avsa_batcher::vit_client::VitClient::new(
        config.vit_service_url.clone(),
        config.vit_timeout_ms,
    ));

    // Construct the BatchQueue with config and the VitClient.
    let queue = Arc::new(avsa_batcher::batch_queue::BatchQueue::new(
        config, vit_client,
    ));

    // Spawn N concurrent drain tasks. Each independently polls the queue and
    // dispatches one batch at a time, allowing up to N batches to be in-flight
    // to the ViT service simultaneously (one per warm GPU container).
    for _ in 0..num_drain_tasks {
        let drain_queue = queue.clone();
        tokio::spawn(async move { drain_queue.run_drain_task().await });
    }

    let state = avsa_batcher::AppState {
        queue: queue.clone(),
    };

    let port: u16 = env::var("BATCHER_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(8081);
    let addr = format!("0.0.0.0:{port}");

    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .unwrap_or_else(|e| panic!("failed to bind {addr}: {e}"));

    tracing::info!(address = %addr, "avsa-batcher listening");

    axum::serve(listener, avsa_batcher::router_with_state(state))
        .await
        .expect("server error");
}
