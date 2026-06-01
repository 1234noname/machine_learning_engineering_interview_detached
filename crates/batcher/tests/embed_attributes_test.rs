// Integration + contract tests for batcher attributes passthrough.
//
// Per testing.md §Test-first: these tests pin the TARGET API. Kept SEPARATE
// from `embed_test.rs` so the existing embedding tests keep compiling + passing
// against the current trait signature.
//
// Target API (encoded here, built by the impl):
//   - `vit_client::Attribute { category, colour, category_confidence, colour_confidence }`
//   - `vit_client::EmbedResult { embedding: Vec<f32>, attributes: Attribute }`
//   - `VitService::embed_batch(..) -> Result<Vec<EmbedResult>, BatchError>` (index-aligned)
//   - `BatchQueue::enqueue(image) -> Result<EmbedResult, BatchError>`
//   - route `EmbedResponse { embedding: Vec<f32>, attributes: Attribute }`

mod common;
use common::{attribute_for_image, test_config};

use async_trait::async_trait;
use avsa_batcher::{
    batch_queue::BatchQueue,
    vit_client::{Attribute, EmbedResult, VitService},
    BatchError,
};
use std::sync::Arc;

// A non-unit embedding derived from the image so we can verify (a) the embedding
// is L2-normalised by the batcher and (b) the embedding routes to the right
// caller. [k, 0, 0, 0] normalises to [1, 0, 0, 0] for any k != 0 - that loses
// the per-image signal, so instead use [k, 1, 0, 0] which normalises distinctly
// per k while still being non-unit pre-normalisation (norm = sqrt(k^2 + 1)).
fn embedding_for_image(image: &[u8]) -> Vec<f32> {
    let key = image.first().copied().unwrap_or(0) as f32;
    vec![key, 1.0, 0.0, 0.0]
}

// ---------------------------------------------------------------------------
// MockVitService - returns a DISTINCT EmbedResult per image, keyed off the
// image bytes, against the TARGET trait signature.
// ---------------------------------------------------------------------------

struct AttributeMock;

#[async_trait]
impl VitService for AttributeMock {
    async fn embed_batch(&self, images: Vec<Vec<u8>>) -> Result<Vec<EmbedResult>, BatchError> {
        let results = images
            .iter()
            .map(|img| EmbedResult {
                embedding: embedding_for_image(img),
                attributes: attribute_for_image(img),
            })
            .collect();
        Ok(results)
    }
}

// ---------------------------------------------------------------------------
// Test 1 (the crux): N concurrent callers each receive their OWN image's
// attributes AND embedding. Mirrors embed_test.rs::concurrent_callers_*.
//
// Each caller submits a distinct image (first byte = caller index + 1). The
// mock derives category/colour from the image bytes, so the only way caller-i
// can hold the attribute "category-{i+1}" is if the fan-out routed caller-i's
// OWN result back to it. A mapping bug would mismatch.
// ---------------------------------------------------------------------------

#[tokio::test]
async fn concurrent_callers_receive_own_attributes() {
    let mock = Arc::new(AttributeMock);
    let n = 5usize;
    // Batch size == n so all callers flush together in a single batch - the
    // worst case for a fan-out mapping bug.
    let cfg = test_config(n, 500);
    let queue = Arc::new(BatchQueue::new(cfg, mock));

    let drain = queue.clone();
    tokio::spawn(async move { drain.run_drain_task().await });

    // Spawn N concurrent callers, each with a distinct image. Keep the image
    // alongside the join handle so we can check each caller's result against
    // ITS OWN image.
    let mut handles = Vec::with_capacity(n);
    for i in 0..n {
        let q = queue.clone();
        // First byte = i + 1 (distinct, non-zero per caller).
        let image = vec![(i as u8) + 1; 8];
        let expected = image.clone();
        handles.push((
            expected,
            tokio::spawn(async move { q.enqueue(image).await }),
        ));
    }

    for (image, handle) in handles {
        let result: EmbedResult = handle.await.expect("join error").expect("embed error");

        let expected_attrs = attribute_for_image(&image);
        assert_eq!(
            result.attributes, expected_attrs,
            "caller with image[0]={} received the wrong attributes (mis-mapping): got {:?}, expected {:?}",
            image[0], result.attributes, expected_attrs
        );

        // The embedding must also map to this caller's own image. The mock
        // returns [k, 1, 0, 0] (k = image[0]); after L2-normalisation the ratio
        // embedding[0] / embedding[1] == k must be preserved, which uniquely
        // identifies the source image.
        let key = image[0] as f32;
        let norm: f32 = result.embedding.iter().map(|x| x * x).sum::<f32>().sqrt();
        assert!(
            (norm - 1.0).abs() < 1e-5,
            "embedding for image[0]={} not unit-norm: norm={norm}",
            image[0]
        );
        let ratio = result.embedding[0] / result.embedding[1];
        assert!(
            (ratio - key).abs() < 1e-4,
            "embedding for image[0]={} routed to wrong caller: ratio={ratio}, expected {key}",
            image[0]
        );
    }
}

// ---------------------------------------------------------------------------
// Test 2: A single batch of K images preserves each image's attributes through
// the batching fan-out; the embedding is L2-normalised but the attributes are
// NOT normalised/mutated.
// ---------------------------------------------------------------------------

#[tokio::test]
async fn attributes_preserved_through_batching() {
    let mock = Arc::new(AttributeMock);
    let k = 4usize;
    let cfg = test_config(k, 500);
    let queue = Arc::new(BatchQueue::new(cfg, mock));

    let drain = queue.clone();
    tokio::spawn(async move { drain.run_drain_task().await });

    let mut handles = Vec::with_capacity(k);
    for i in 0..k {
        let q = queue.clone();
        let image = vec![(i as u8) + 10; 8];
        let expected = image.clone();
        handles.push((
            expected,
            tokio::spawn(async move { q.enqueue(image).await }),
        ));
    }

    for (image, handle) in handles {
        let result: EmbedResult = handle.await.expect("join error").expect("embed error");
        let key = image[0];

        // Attributes are the mock's per-image attribute, passed through verbatim
        // (NOT normalised or otherwise mutated by the batcher).
        let expected_attrs = attribute_for_image(&image);
        assert_eq!(
            result.attributes, expected_attrs,
            "attributes mutated/mis-mapped for image[0]={key}"
        );
        // Confidence values are passed through unchanged - the batcher must not
        // apply L2-normalisation (or any transform) to attribute fields.
        assert_eq!(
            result.attributes.category_confidence, key as f32,
            "category_confidence was mutated for image[0]={key}"
        );
        assert_eq!(
            result.attributes.colour_confidence,
            (key as f32) + 0.5,
            "colour_confidence was mutated for image[0]={key}"
        );

        // The embedding IS L2-normalised by the batcher (unit norm).
        let norm: f32 = result.embedding.iter().map(|x| x * x).sum::<f32>().sqrt();
        assert!(
            (norm - 1.0).abs() < 1e-5,
            "embedding not L2-normalised for image[0]={key}: norm={norm}"
        );
    }
}

// ---------------------------------------------------------------------------
// Test 3 (wire contract, pure unit): the model-shaped /embed response
// `{"embeddings": [[...]], "attributes": [{...}]}` parses into the target
// types, and a length mismatch (embeddings.len() != attributes.len()) is
// REJECTED as an error.
//
// This pins the deserialise + validation surface the impl must expose. The
// impl provides a function that parses the model response body into a
// `Vec<EmbedResult>` (index-aligned), erroring on length mismatch. The exact
// name is pinned here as `vit_client::parse_embed_response`.
// ---------------------------------------------------------------------------

#[test]
fn embed_response_deserializes_attributes() {
    use avsa_batcher::vit_client::parse_embed_response;

    // Well-formed model response: 2 embeddings, 2 parallel attributes.
    let body = r#"{
        "embeddings": [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]],
        "attributes": [
            {"category": "dress", "colour": "red", "category_confidence": 0.91, "colour_confidence": 0.80},
            {"category": "shoe", "colour": "blue", "category_confidence": 0.55, "colour_confidence": 0.42}
        ]
    }"#;

    let parsed: Vec<EmbedResult> =
        parse_embed_response(body).expect("well-formed model response must parse");

    assert_eq!(parsed.len(), 2, "expected 2 index-aligned results");

    assert_eq!(parsed[0].embedding, vec![1.0_f32, 0.0, 0.0, 0.0]);
    assert_eq!(
        parsed[0].attributes,
        Attribute {
            category: "dress".to_string(),
            colour: "red".to_string(),
            category_confidence: 0.91,
            colour_confidence: 0.80,
        }
    );

    assert_eq!(parsed[1].embedding, vec![0.0_f32, 1.0, 0.0, 0.0]);
    assert_eq!(parsed[1].attributes.category, "shoe");
    assert_eq!(parsed[1].attributes.colour, "blue");
}

// ---------------------------------------------------------------------------
// Test 4: the ViT service returns FEWER EmbedResults than images SENT (a
// wrong-count response). The fan-out must detect results.len() != items.len()
// and fan an explicit error to EVERY caller - none may be silently dropped
// (which would surface as an opaque ChannelClosed/502 only for the truncated
// tail) or left hanging.
//
// `parse_embed_response` only guarantees embeddings.len() == attributes.len();
// it does NOT guarantee one result per image sent. This test pins the guard
// at the batch fan-out boundary.
// ---------------------------------------------------------------------------

/// Returns one FEWER result than images sent (drops the last image's slot).
struct WrongCountMock;

#[async_trait]
impl VitService for WrongCountMock {
    async fn embed_batch(&self, images: Vec<Vec<u8>>) -> Result<Vec<EmbedResult>, BatchError> {
        // Return strictly fewer results than images sent: a contract violation.
        let short = images.len().saturating_sub(1);
        let results = images
            .iter()
            .take(short)
            .map(|img| EmbedResult {
                embedding: embedding_for_image(img),
                attributes: attribute_for_image(img),
            })
            .collect();
        Ok(results)
    }
}

#[tokio::test]
async fn wrong_result_count_fans_error_to_all_callers() {
    let mock = Arc::new(WrongCountMock);
    let n = 5usize;
    // Batch size == n so all N callers flush together in a single batch, then
    // the mock returns n-1 results - every caller must receive an error.
    let cfg = test_config(n, 200);
    let queue = Arc::new(BatchQueue::new(cfg, mock));

    let drain = queue.clone();
    tokio::spawn(async move { drain.run_drain_task().await });

    let mut handles = Vec::with_capacity(n);
    for i in 0..n {
        let q = queue.clone();
        let image = vec![(i as u8) + 1; 8];
        handles.push(tokio::spawn(async move { q.enqueue(image).await }));
    }

    // EVERY caller must observe an Err (502-class) - none silently dropped/hung.
    // A dropped sender surfaces as ChannelClosed (or Timeout) either way, so we
    // assert all N got an Err result rather than pinning a single variant.
    for (i, handle) in handles.into_iter().enumerate() {
        let result = handle.await.expect("join error");
        assert!(
            result.is_err(),
            "caller {i} did NOT receive an error on a wrong-count response (silently dropped/hung): got {:?}",
            result.ok()
        );
    }
}

#[test]
fn embed_response_length_mismatch_is_rejected() {
    use avsa_batcher::vit_client::parse_embed_response;

    // 2 embeddings but only 1 attribute - the per-image mapping is broken, so
    // the impl MUST reject this rather than silently truncate/zip.
    let body = r#"{
        "embeddings": [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]],
        "attributes": [
            {"category": "dress", "colour": "red", "category_confidence": 0.91, "colour_confidence": 0.80}
        ]
    }"#;

    let result = parse_embed_response(body);
    assert!(
        result.is_err(),
        "length mismatch (embeddings != attributes) must be rejected, got Ok({:?})",
        result.ok()
    );
}
