// POST /embed - accepts a base64-encoded image, enqueues it into BatchQueue,
// and returns the embedding vector plus its ViT attributes.
//
// Contract (batcher → caller, response): `{"embedding": [f32; 768],
// "attributes": {category, colour, category_confidence, colour_confidence}}`.
// The `attributes` field is additive; callers that read only `embedding` are
// unaffected. The caller sends one image, so the handler returns the single
// `EmbedResult` for that image.
//
// Error mapping:
//   - 400: base64 decode failure (client sent garbage)
//   - 502: VitServiceUnavailable, MalformedVitResponse, or ChannelClosed (upstream fault)
//   - 504: Timeout (upstream too slow; timeout = max_wait_ms × 10)

use crate::{
    error::BatchError,
    metrics::{REQUESTS_TOTAL, REQUEST_LATENCY},
    vit_client::Attribute,
    AppState,
};
use axum::{
    extract::State,
    http::StatusCode,
    response::{IntoResponse, Response},
    Json,
};
use base64::{engine::general_purpose::STANDARD as BASE64_STANDARD, Engine};
use serde::{Deserialize, Serialize};
use std::time::Instant;
use tracing::instrument;

// ---------------------------------------------------------------------------
// Request / response wire types
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
pub struct EmbedRequest {
    /// Base64-encoded raw image bytes.
    image_bytes: String,
}

#[derive(Serialize)]
pub struct EmbedResponse {
    embedding: Vec<f32>,
    attributes: Attribute,
}

// ---------------------------------------------------------------------------
// Metrics helper
// ---------------------------------------------------------------------------

/// Record `avsa_batcher_requests_total{outcome}` and
/// `avsa_batcher_request_latency_seconds`, then return the response unchanged.
fn finish(t0: Instant, outcome: &str, response: Response) -> Response {
    REQUESTS_TOTAL.with_label_values(&[outcome]).inc();
    REQUEST_LATENCY.observe(t0.elapsed().as_secs_f64());
    response
}

// ---------------------------------------------------------------------------
// Handler
// ---------------------------------------------------------------------------

/// POST /embed
///
/// Decodes the base64 image, enqueues it into the shared `BatchQueue`, awaits
/// the oneshot result, and returns the embedding as JSON.
#[instrument(name = "embed_handler", skip(state, body))]
pub async fn handler(State(state): State<AppState>, Json(body): Json<EmbedRequest>) -> Response {
    let t0 = Instant::now();

    // Decode base64 in the handler - the queue stores raw bytes and must
    // remain encoding-agnostic.
    let raw_bytes = match BASE64_STANDARD.decode(&body.image_bytes) {
        Ok(bytes) => bytes,
        Err(e) => {
            tracing::warn!(error = %e, "base64 decode failed");
            let msg = format!("invalid base64: {e}");
            return finish(
                t0,
                "bad_request",
                (
                    StatusCode::BAD_REQUEST,
                    Json(serde_json::json!({"error": msg})),
                )
                    .into_response(),
            );
        }
    };

    match state.queue.enqueue(raw_bytes).await {
        Ok(result) => finish(
            t0,
            "ok",
            (
                StatusCode::OK,
                Json(EmbedResponse {
                    embedding: result.embedding,
                    attributes: result.attributes,
                }),
            )
                .into_response(),
        ),
        Err(BatchError::Base64Decode(e)) => {
            // Shouldn't reach here (decoded above), but handle defensively.
            let msg = format!("base64 decode failed: {e}");
            finish(
                t0,
                "bad_request",
                (
                    StatusCode::BAD_REQUEST,
                    Json(serde_json::json!({"error": msg})),
                )
                    .into_response(),
            )
        }
        Err(BatchError::Timeout { timeout_ms }) => {
            tracing::error!(timeout_ms, "embed request timed out");
            let msg = format!("embedding timed out after {timeout_ms}ms");
            finish(
                t0,
                "timeout",
                (
                    StatusCode::GATEWAY_TIMEOUT,
                    Json(serde_json::json!({"error": msg})),
                )
                    .into_response(),
            )
        }
        Err(BatchError::VitServiceUnavailable(e)) => {
            tracing::error!(error = %e, "ViT service unavailable");
            finish(
                t0,
                "bad_gateway",
                (
                    StatusCode::BAD_GATEWAY,
                    Json(serde_json::json!({"error": "ViT service unavailable"})),
                )
                    .into_response(),
            )
        }
        Err(BatchError::MalformedVitResponse(msg)) => {
            tracing::error!(error = %msg, "ViT service returned a malformed response");
            finish(
                t0,
                "bad_gateway",
                (
                    StatusCode::BAD_GATEWAY,
                    Json(serde_json::json!({"error": "ViT service returned a malformed response"})),
                )
                    .into_response(),
            )
        }
        Err(BatchError::ChannelClosed) => {
            tracing::error!("embed response channel closed unexpectedly");
            finish(
                t0,
                "bad_gateway",
                (
                    StatusCode::BAD_GATEWAY,
                    Json(serde_json::json!({"error": "embedding service error"})),
                )
                    .into_response(),
            )
        }
    }
}
