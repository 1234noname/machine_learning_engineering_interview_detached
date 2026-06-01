// VitClient - HTTP client for the upstream ViT model service.
//
// The `VitService` trait is the mockable interface used by `BatchQueue`.
// `VitClient` is the production implementation. Tests inject a `MockVitService`
// that never touches the network.
//
// Object-safety requirement: `VitService` must be boxable as `Box<dyn VitService>`.
// The `async_trait` macro achieves this on stable Rust by desugaring async fn
// into a boxed future.

use crate::error::BatchError;
use async_trait::async_trait;
use base64::{engine::general_purpose::STANDARD as BASE64_STANDARD, Engine};
use serde::{Deserialize, Serialize};
use std::time::Duration;
use tracing::instrument;

// ---------------------------------------------------------------------------
// Result + attribute types
// ---------------------------------------------------------------------------

/// Per-image attribute prediction from the dual-head ViT.
///
/// `category`/`colour` are the argmax labels; the confidences are the softmax
/// probability at the winning index (`[0, 1]` from the model). These are
/// classifier outputs, NOT an embedding - the batcher passes them through
/// verbatim and never L2-normalises or otherwise mutates them.
#[derive(Serialize, Deserialize, Clone, Debug, PartialEq)]
pub struct Attribute {
    pub category: String,
    pub colour: String,
    pub category_confidence: f32,
    pub colour_confidence: f32,
}

/// One image's worth of model output: its embedding paired with its attributes.
///
/// The batcher fans these out index-aligned to callers (`result[i]` ↔
/// `images[i]`). The `embedding` is L2-normalised by the batcher before it
/// reaches a caller; `attributes` pass through unchanged.
#[derive(Clone, Debug, PartialEq)]
pub struct EmbedResult {
    pub embedding: Vec<f32>,
    pub attributes: Attribute,
}

// ---------------------------------------------------------------------------
// VitService trait (object-safe via async_trait)
// ---------------------------------------------------------------------------

/// Mockable interface for the upstream ViT model service.
///
/// `BatchQueue` holds an `Arc<dyn VitService>` so tests can swap in a mock.
#[async_trait]
pub trait VitService: Send + Sync {
    /// Forward a batch of raw image bytes to the ViT service and return one
    /// [`EmbedResult`] (embedding + attributes) per image.
    ///
    /// The returned `Vec<EmbedResult>` must be the same length as `images` and
    /// in the same order - the drain task fans out results by index.
    async fn embed_batch(&self, images: Vec<Vec<u8>>) -> Result<Vec<EmbedResult>, BatchError>;
}

// ---------------------------------------------------------------------------
// Wire types for the ViT HTTP API
//
// Contract (batcher → model `POST /embed`, response): the model service returns
// `{"embeddings": [[f32; 768], ...], "attributes": [{category, colour,
// category_confidence, colour_confidence}, ...]}` - two parallel, index-aligned
// arrays. The batcher deserialises BOTH and pairs them by index into
// `EmbedResult`. A length mismatch breaks the per-image mapping and is rejected
// (`BatchError::MalformedVitResponse`), never silently zipped.
// ---------------------------------------------------------------------------

#[derive(Serialize)]
struct EmbedRequest {
    /// Base64-encoded image bytes, one per batch item.
    images: Vec<String>,
}

#[derive(Deserialize)]
struct EmbedResponse {
    /// One L2-normalised embedding vector per image.
    embeddings: Vec<Vec<f32>>,
    /// One attribute prediction per image, parallel to `embeddings`.
    attributes: Vec<Attribute>,
}

/// Parse a model `/embed` response body into index-aligned [`EmbedResult`]s.
///
/// The two wire arrays (`embeddings`, `attributes`) MUST have equal length -
/// they are parallel per-image outputs. If they differ the per-image mapping is
/// broken, so we reject with [`BatchError::MalformedVitResponse`] rather than
/// silently zip/truncate (which would hand a caller another image's attributes).
pub fn parse_embed_response(body: &str) -> Result<Vec<EmbedResult>, BatchError> {
    let parsed: EmbedResponse = serde_json::from_str(body)
        .map_err(|e| BatchError::MalformedVitResponse(format!("invalid JSON: {e}")))?;

    if parsed.embeddings.len() != parsed.attributes.len() {
        return Err(BatchError::MalformedVitResponse(format!(
            "embeddings.len()={} != attributes.len()={}; per-image mapping is broken",
            parsed.embeddings.len(),
            parsed.attributes.len()
        )));
    }

    Ok(parsed
        .embeddings
        .into_iter()
        .zip(parsed.attributes)
        .map(|(embedding, attributes)| EmbedResult {
            embedding,
            attributes,
        })
        .collect())
}

// ---------------------------------------------------------------------------
// VitClient - production implementation
// ---------------------------------------------------------------------------

/// Production HTTP client that calls the ViT model service.
pub struct VitClient {
    client: reqwest::Client,
    url: String,
}

impl VitClient {
    /// Create a new `VitClient` pointing at `url` with an explicit HTTP timeout.
    ///
    /// `timeout_ms` must cover cold-start latency on remote GPU backends (Modal,
    /// GKE node pool) in addition to inference time - typically 30 s.
    pub fn new(url: String, timeout_ms: u64) -> Self {
        VitClient {
            client: reqwest::Client::builder()
                .timeout(Duration::from_millis(timeout_ms))
                .build()
                .expect("reqwest client build failed"),
            url,
        }
    }
}

#[async_trait]
impl VitService for VitClient {
    /// POST `{"images": [<base64>, ...]}` to the ViT service and parse the
    /// `{"embeddings": [[f32; 768], ...], "attributes": [{...}, ...]}` response
    /// into index-aligned [`EmbedResult`]s via [`parse_embed_response`].
    ///
    /// The OTel span `vit.embed.duration_ms` wraps this call via `#[instrument]`.
    #[instrument(name = "vit.embed.duration_ms", skip(self, images), fields(batch_size = images.len()))]
    async fn embed_batch(&self, images: Vec<Vec<u8>>) -> Result<Vec<EmbedResult>, BatchError> {
        tracing::debug!(batch_size = images.len(), "sending batch to ViT service");
        let encoded: Vec<String> = images.iter().map(|b| BASE64_STANDARD.encode(b)).collect();

        let body = EmbedRequest { images: encoded };

        let response = self
            .client
            .post(&self.url)
            .json(&body)
            .send()
            .await
            .map_err(BatchError::VitServiceUnavailable)?;

        let raw = response
            .error_for_status()
            .map_err(BatchError::VitServiceUnavailable)?
            .text()
            .await
            .map_err(BatchError::VitServiceUnavailable)?;

        parse_embed_response(&raw)
    }
}
