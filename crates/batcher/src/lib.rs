// Library entry point. Exports the axum Router so integration tests can
// spawn the app without going through main(). Keeps main.rs free of route logic.
//
// Two router functions:
//   - `router()`:            GET /health only (backward-compat for health_test.rs).
//   - `router_with_state()`: GET /health + POST /embed with injected AppState.

pub mod batch_queue;
pub mod config;
pub mod error;
pub mod metrics;
pub mod routes;
pub mod vit_client;

use axum::{
    routing::{get, post},
    Router,
};
use batch_queue::BatchQueue;
use std::sync::Arc;

/// Shared application state injected via axum `State`.
///
/// Cheap to clone: the field is an `Arc`.
#[derive(Clone)]
pub struct AppState {
    pub queue: Arc<BatchQueue>,
}

/// Build a router with GET /health only.
///
/// Preserved for backward-compatibility with `tests/health_test.rs`, which
/// spawns the app without constructing an `AppState`.
pub fn router() -> Router {
    Router::new()
        .route("/health", get(routes::health::handler))
        .route("/metrics", get(routes::metrics))
}

/// Build the full router with GET /health, GET /metrics, and POST /embed.
///
/// Requires an `AppState` with a live `BatchQueue`.
pub fn router_with_state(state: AppState) -> Router {
    Router::new()
        .route("/health", get(routes::health::handler))
        .route("/metrics", get(routes::metrics))
        .route("/embed", post(routes::embed::handler))
        .with_state(state)
}

// Re-export the public error type so tests can match on it.
pub use error::BatchError;
