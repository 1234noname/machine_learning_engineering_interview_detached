// Typed error enum for the batcher crate.
//
// All error variants are named and carry context. Callers pattern-match
// on the variant - they never inspect a string. `thiserror` derives Display
// and std::error::Error automatically.

use thiserror::Error;

#[derive(Debug, Error)]
pub enum BatchError {
    /// The upstream ViT model service returned an error or was unreachable.
    #[error("ViT service unavailable: {0}")]
    VitServiceUnavailable(#[source] reqwest::Error),

    /// The upstream ViT model service returned a response that violated the
    /// contract - e.g. `embeddings` and `attributes` had different lengths, so
    /// the per-image mapping is broken. This is an upstream fault (502-class):
    /// we refuse to silently zip/truncate a mismatched response.
    #[error("malformed ViT response: {0}")]
    MalformedVitResponse(String),

    /// The batch embedding timed out waiting for the upstream response.
    #[error("embedding request timed out after {timeout_ms}ms")]
    Timeout { timeout_ms: u64 },

    /// The base64 payload in the request body could not be decoded.
    #[error("base64 decode failed: {0}")]
    Base64Decode(#[from] base64::DecodeError),

    /// A caller's oneshot receiver was dropped before a result was sent.
    /// This indicates an internal logic error in the drain task.
    #[error("internal: response channel closed before result was sent")]
    ChannelClosed,
}
