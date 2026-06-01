// REAL batcher↔model contract test - exercises the production `VitClient`
// HTTP path against a REAL running model service (`apps/model`, `POST /embed`).
//
// Why this test exists (the gap it closes):
//   `BatchQueue`'s unit tests inject `MockVitService`, so the production
//   `VitClient::embed_batch` - which serialises `{"images":[<b64>,...]}`
//   (PLURAL) and parses `{"embeddings":[[f32]],"attributes":[{...}]}` (PLURAL)
//   via `parse_embed_response` - is NEVER exercised against the real model wire
//   format in the batcher's own suite. The model service independently
//   accepts/returns that shape. A rename of `images`/`embeddings`/`attributes`
//   on EITHER side passes both unit suites green while production breaks at the
//   batcher→model boundary. This test codifies the round-trip as a repeatable
//   gate: REAL reqwest HTTP + REAL `parse_embed_response` against the REAL model
//   wire format - no `MockVitService`.
//
// Gating:
//   Marked `#[ignore]` so it never runs in a default `cargo test` (which has no
//   model server on the loopback). CI un-ignores it after starting the stub
//   model on :8090 (see the `batcher-contract` job in .github/workflows/ci.yml).
//   Locally:
//     AVSA_MODEL_STUB=1 uv --directory apps/model run \
//       uvicorn avsa_model.main:app --host 127.0.0.1 --port 8090 &
//     AVSA_VIT_TEST_URL=http://127.0.0.1:8090/embed \
//       cargo test --manifest-path crates/batcher/Cargo.toml \
//       --test vit_client_contract_test -- --ignored --nocapture
//
// The model URL is read from `AVSA_VIT_TEST_URL` (default
// `http://127.0.0.1:8090/embed`) so CI / local runs can point at any reachable
// model instance. The stub model (`AVSA_MODEL_STUB=1`) derives deterministic
// 768-d embeddings + attributes from SHA-256 of the image bytes - no GPU /
// weights / numpy - so this gate is cheap and deterministic.

use avsa_batcher::vit_client::{EmbedResult, VitClient, VitService};

/// Resolve the model `/embed` URL the contract test should hit.
fn model_url() -> String {
    std::env::var("AVSA_VIT_TEST_URL").unwrap_or_else(|_| "http://127.0.0.1:8090/embed".to_string())
}

/// Three distinct REAL 224x224 RGB JPEGs (solid red/green/blue, committed under
/// `tests/fixtures/`). The stub embedder SHA-256s the raw bytes (distinct files →
/// distinct deterministic outputs), and - crucially - these are valid images the
/// REAL ViT can decode, so this contract test passes against BOTH the stub
/// (`AVSA_MODEL_STUB=1`, CI) and a live real model. (A synthetic JPEG header that
/// only the SHA-256 stub accepts would 500 against the real ViT's PIL decode.)
fn sample_images() -> Vec<Vec<u8>> {
    vec![
        include_bytes!("fixtures/sample_red.jpg").to_vec(),
        include_bytes!("fixtures/sample_green.jpg").to_vec(),
        include_bytes!("fixtures/sample_blue.jpg").to_vec(),
    ]
}

/// The REAL contract: a production `VitClient` POSTs the plural request shape to
/// a live model `/embed`, and `parse_embed_response` parses the plural response
/// shape back into index-aligned `EmbedResult`s. Asserts on the wire format the
/// model actually returns - not a mock's hand-built shape.
#[tokio::test]
#[ignore = "requires a live model service on AVSA_VIT_TEST_URL (default :8090); run with --ignored"]
async fn vit_client_round_trips_against_real_model() {
    let url = model_url();
    let client = VitClient::new(url.clone(), 30_000);
    let images = sample_images();
    let n = images.len();

    let results: Vec<EmbedResult> = client
        .embed_batch(images)
        .await
        .unwrap_or_else(|e| panic!("real VitClient round-trip against {url} failed: {e}"));

    // One index-aligned result per image sent.
    assert_eq!(
        results.len(),
        n,
        "expected {n} results (one per image), got {}",
        results.len()
    );

    for (i, r) in results.iter().enumerate() {
        // Embedding is the model's real 768-d vector (the documented ViT dim).
        assert_eq!(
            r.embedding.len(),
            768,
            "result[{i}] embedding dim = {}, expected 768",
            r.embedding.len()
        );
        // The model L2-normalises embeddings; every component is finite.
        assert!(
            r.embedding.iter().all(|x| x.is_finite()),
            "result[{i}] embedding contains a non-finite value: {:?}",
            r.embedding
        );

        // Attributes are populated (non-empty category/colour) - proves the
        // parallel `attributes` array deserialised into the real struct, not a
        // default/empty.
        let a = &r.attributes;
        assert!(
            !a.category.is_empty(),
            "result[{i}] attributes.category is empty"
        );
        assert!(
            !a.colour.is_empty(),
            "result[{i}] attributes.colour is empty"
        );
        // Confidences are valid probabilities in [0, 1] (classifier softmax).
        assert!(
            (0.0..=1.0).contains(&a.category_confidence),
            "result[{i}] category_confidence={} out of [0,1]",
            a.category_confidence
        );
        assert!(
            (0.0..=1.0).contains(&a.colour_confidence),
            "result[{i}] colour_confidence={} out of [0,1]",
            a.colour_confidence
        );
    }
}
