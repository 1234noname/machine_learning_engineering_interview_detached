// Integration tests for POST /embed, BatchQueue, and VitService.
// Per testing.md §Test-first: tests are written before production code.
// Uses a MockVitService injected into BatchQueue so no real ViT model is needed.

mod common;
use common::{attribute_for_image, test_config};

use async_trait::async_trait;
use avsa_batcher::{
    batch_queue::BatchQueue,
    router_with_state,
    vit_client::{EmbedResult, VitService},
    AppState, BatchError,
};
use base64::{engine::general_purpose::STANDARD as BASE64_STANDARD, Engine};
use std::sync::{
    atomic::{AtomicUsize, Ordering},
    Arc,
};
use tracing_test::traced_test;

// ---------------------------------------------------------------------------
// MockVitService - returns distinct embeddings per item index position.
// The mock records how many times embed_batch was called.
// ---------------------------------------------------------------------------

struct MockVitService {
    call_count: Arc<AtomicUsize>,
}

impl MockVitService {
    fn new() -> (Arc<Self>, Arc<AtomicUsize>) {
        let count = Arc::new(AtomicUsize::new(0));
        let svc = Arc::new(MockVitService {
            call_count: count.clone(),
        });
        (svc, count)
    }
}

#[async_trait]
impl VitService for MockVitService {
    async fn embed_batch(&self, images: Vec<Vec<u8>>) -> Result<Vec<EmbedResult>, BatchError> {
        self.call_count.fetch_add(1, Ordering::SeqCst);
        // Return a distinct embedding for each image ([index as f32; 4]) paired
        // with a plausible per-image attribute.
        let results = images
            .iter()
            .enumerate()
            .map(|(i, img)| EmbedResult {
                embedding: vec![i as f32; 4],
                attributes: attribute_for_image(img),
            })
            .collect();
        Ok(results)
    }
}

// ---------------------------------------------------------------------------
// Test 1: Flush-at-size - queue flushes exactly when max_batch_size items enqueued.
//
// Strategy: set max_batch_size=3, max_wait_ms=2000 (long so timeout doesn't fire).
// Enqueue 3 items concurrently; each should receive an embedding.
// The mock records call_count; assert it was called exactly once.
// ---------------------------------------------------------------------------

#[tokio::test]
async fn flush_at_size() {
    let (mock, call_count) = MockVitService::new();
    let cfg = test_config(3, 2000);
    let queue = Arc::new(BatchQueue::new(cfg, mock));

    // Start the background drain task.
    let drain_queue = queue.clone();
    tokio::spawn(async move { drain_queue.run_drain_task().await });

    // Enqueue 3 items in parallel.
    let q1 = queue.clone();
    let q2 = queue.clone();
    let q3 = queue.clone();
    let (r1, r2, r3) = tokio::join!(
        tokio::spawn(async move { q1.enqueue(vec![1u8; 8]).await }),
        tokio::spawn(async move { q2.enqueue(vec![2u8; 8]).await }),
        tokio::spawn(async move { q3.enqueue(vec![3u8; 8]).await }),
    );
    let r1 = r1.expect("join error").expect("embed error 1");
    let r2 = r2.expect("join error").expect("embed error 2");
    let r3 = r3.expect("join error").expect("embed error 3");

    // Embeddings are assigned positionally within the batch.
    // All three must be non-empty f32 vecs.
    assert_eq!(r1.embedding.len(), 4, "r1 len");
    assert_eq!(r2.embedding.len(), 4, "r2 len");
    assert_eq!(r3.embedding.len(), 4, "r3 len");

    // The mock must have been called exactly once (one batch of 3).
    assert_eq!(
        call_count.load(Ordering::SeqCst),
        1,
        "embed_batch call count"
    );
}

// ---------------------------------------------------------------------------
// Test 2: Flush-at-timeout - queue flushes after max_wait_ms even if fewer than
// max_batch_size items are queued.
//
// Strategy: set max_batch_size=100 (won't be reached), max_wait_ms=50.
// Enqueue 2 items; wait for results (should arrive within ~100 ms).
// ---------------------------------------------------------------------------

#[tokio::test]
async fn flush_at_timeout() {
    let (mock, call_count) = MockVitService::new();
    let cfg = test_config(100, 50);
    let queue = Arc::new(BatchQueue::new(cfg, mock));

    let drain_queue = queue.clone();
    tokio::spawn(async move { drain_queue.run_drain_task().await });

    let q1 = queue.clone();
    let q2 = queue.clone();

    let (r1, r2) = tokio::join!(
        tokio::spawn(async move { q1.enqueue(vec![1u8; 8]).await }),
        tokio::spawn(async move { q2.enqueue(vec![2u8; 8]).await }),
    );
    let r1 = r1.expect("join error").expect("embed error 1");
    let r2 = r2.expect("join error").expect("embed error 2");

    assert_eq!(r1.embedding.len(), 4, "r1 len");
    assert_eq!(r2.embedding.len(), 4, "r2 len");
    assert_eq!(
        call_count.load(Ordering::SeqCst),
        1,
        "embed_batch call count"
    );
}

// ---------------------------------------------------------------------------
// Test 7: Batcher L2-normalises embeddings before returning them to callers.
//
// The batcher owns the normalisation invariant so that callers never receive
// a non-unit embedding regardless of what the ViT service returns. Without
// this, pgvector's <=> cosine-distance operator silently produces wrong results.
//
// Strategy: use a mock that returns [3.0, 4.0, 0.0, 0.0] (norm = 5.0) for
// every image. After normalisation, the expected result is [0.6, 0.8, 0.0, 0.0].
// ---------------------------------------------------------------------------

struct KnownNormMock;

#[async_trait]
impl VitService for KnownNormMock {
    async fn embed_batch(&self, images: Vec<Vec<u8>>) -> Result<Vec<EmbedResult>, BatchError> {
        // [3.0, 4.0, 0.0, 0.0] has L2 norm = 5.0.
        // After normalisation: [0.6, 0.8, 0.0, 0.0] with norm = 1.0.
        Ok(images
            .iter()
            .map(|img| EmbedResult {
                embedding: vec![3.0_f32, 4.0, 0.0, 0.0],
                attributes: attribute_for_image(img),
            })
            .collect())
    }
}

#[tokio::test]
async fn batcher_l2_normalises_embeddings_before_returning() {
    let mock = Arc::new(KnownNormMock);
    let cfg = test_config(1, 500);
    let queue = Arc::new(BatchQueue::new(cfg, mock));

    let drain = queue.clone();
    tokio::spawn(async move { drain.run_drain_task().await });

    let embedding = queue
        .enqueue(vec![1u8; 8])
        .await
        .expect("embed failed")
        .embedding;

    // Verify the batcher normalised [3, 4, 0, 0] → [0.6, 0.8, 0, 0].
    assert_eq!(embedding.len(), 4);

    let norm: f32 = embedding.iter().map(|x| x * x).sum::<f32>().sqrt();
    assert!(
        (norm - 1.0).abs() < 1e-5,
        "embedding must be a unit vector after batcher normalisation; norm={norm}"
    );

    assert!(
        (embedding[0] - 0.6).abs() < 1e-5,
        "embedding[0] expected 0.6, got {}",
        embedding[0]
    );
    assert!(
        (embedding[1] - 0.8).abs() < 1e-5,
        "embedding[1] expected 0.8, got {}",
        embedding[1]
    );
    assert!(embedding[2].abs() < 1e-5, "embedding[2] expected 0.0");
    assert!(embedding[3].abs() < 1e-5, "embedding[3] expected 0.0");
}

// ---------------------------------------------------------------------------
// Test 4: HTTP POST /embed end-to-end - base64-encoded image bytes, returns embedding.
// ---------------------------------------------------------------------------

#[tokio::test]
async fn http_post_embed_returns_embedding() {
    let (mock, _) = MockVitService::new();
    let cfg = test_config(1, 500);
    let queue = Arc::new(BatchQueue::new(cfg, mock));

    let drain_queue = queue.clone();
    tokio::spawn(async move { drain_queue.run_drain_task().await });

    let state = AppState {
        queue: queue.clone(),
    };

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("failed to bind");
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, router_with_state(state))
            .await
            .expect("server error");
    });

    let client = reqwest::Client::new();
    let image_b64 = BASE64_STANDARD.encode(vec![42u8; 16]);
    let body = serde_json::json!({ "image_bytes": image_b64 });

    let resp = client
        .post(format!("http://{addr}/embed"))
        .json(&body)
        .send()
        .await
        .expect("request failed");

    assert_eq!(resp.status().as_u16(), 200, "expected 200");

    let json: serde_json::Value = resp.json().await.expect("invalid json");
    let embedding = json["embedding"].as_array().expect("embedding not array");
    assert_eq!(embedding.len(), 4, "embedding length");
}

// ---------------------------------------------------------------------------
// Test 5: Invalid base64 returns HTTP 400.
// ---------------------------------------------------------------------------

#[tokio::test]
async fn http_post_embed_bad_base64_returns_400() {
    let (mock, _) = MockVitService::new();
    let cfg = test_config(1, 500);
    let queue = Arc::new(BatchQueue::new(cfg, mock));

    let drain_queue = queue.clone();
    tokio::spawn(async move { drain_queue.run_drain_task().await });

    let state = AppState { queue };

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("failed to bind");
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, router_with_state(state))
            .await
            .expect("server error");
    });

    let client = reqwest::Client::new();
    let body = serde_json::json!({ "image_bytes": "!!!not-valid-base64!!!" });

    let resp = client
        .post(format!("http://{addr}/embed"))
        .json(&body)
        .send()
        .await
        .expect("request failed");

    assert_eq!(resp.status().as_u16(), 400, "expected 400 for bad base64");
}

// ---------------------------------------------------------------------------
// Test 8: HTTP POST /embed returns a non-200 status when the ViT service is
// unavailable.
//
// BatchQueue uses a VecDeque with no capacity limit, so there is no queue-full
// path to exercise. Instead this test validates the upstream-failure path:
// the batcher must propagate errors from the ViT service back to the HTTP caller
// as a 5xx response (502 Bad Gateway or 503 Service Unavailable).
//
// Strategy: inject a FailingVitService that always returns an error; assert the
// /embed response status is not 200.
// ---------------------------------------------------------------------------

struct FailingVitService;

#[async_trait]
impl VitService for FailingVitService {
    async fn embed_batch(&self, _images: Vec<Vec<u8>>) -> Result<Vec<EmbedResult>, BatchError> {
        Err(BatchError::ChannelClosed)
    }
}

#[tokio::test]
async fn batcher_returns_error_when_vit_service_unavailable() {
    let cfg = test_config(1, 50);
    let queue = Arc::new(BatchQueue::new(cfg, Arc::new(FailingVitService)));

    let drain_queue = queue.clone();
    tokio::spawn(async move { drain_queue.run_drain_task().await });

    let state = AppState {
        queue: queue.clone(),
    };

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("failed to bind");
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, router_with_state(state))
            .await
            .expect("server error");
    });

    let client = reqwest::Client::new();
    let image_b64 = BASE64_STANDARD.encode(vec![1u8; 8]);
    let body = serde_json::json!({ "image_bytes": image_b64 });

    let resp = client
        .post(format!("http://{addr}/embed"))
        .json(&body)
        .send()
        .await
        .expect("request failed");

    // The batcher must not return 200 when the ViT service fails.
    // Expect 502 Bad Gateway (ChannelClosed maps to 502 in the embed handler).
    assert_ne!(
        resp.status().as_u16(),
        200,
        "expected non-200 when ViT service is unavailable; got {}",
        resp.status()
    );
    assert_eq!(
        resp.status().as_u16(),
        502,
        "expected 502 Bad Gateway when ViT service fails; got {}",
        resp.status()
    );
}

// ---------------------------------------------------------------------------
// Test 6: OTel span names are emitted during a full enqueue → flush → ViT cycle.
//
// Uses #[traced_test] to capture tracing output in memory. Each span emits a
// debug event so logs_contain can find the span name in the formatted log line
// (tracing-subscriber fmt includes span context: "span_name{fields}: message").
// The mock implements #[instrument(name = "vit.embed.duration_ms")] to mirror
// the production VitClient's instrumentation.
// ---------------------------------------------------------------------------

#[tokio::test]
#[traced_test]
async fn otel_spans_are_emitted() {
    struct InstrumentedMock;

    #[async_trait]
    impl VitService for InstrumentedMock {
        #[tracing::instrument(name = "vit.embed.duration_ms", skip_all)]
        async fn embed_batch(&self, images: Vec<Vec<u8>>) -> Result<Vec<EmbedResult>, BatchError> {
            tracing::debug!(batch_size = images.len(), "sending batch to ViT service");
            Ok(images
                .iter()
                .map(|img| EmbedResult {
                    embedding: vec![0.0f32; 4],
                    attributes: attribute_for_image(img),
                })
                .collect())
        }
    }

    let cfg = test_config(1, 500);
    let queue = Arc::new(BatchQueue::new(cfg, Arc::new(InstrumentedMock)));

    let drain = queue.clone();
    tokio::spawn(async move { drain.run_drain_task().await });

    queue.enqueue(vec![1u8; 8]).await.expect("embed failed");

    assert!(logs_contain("batcher.embed.queued"));
    assert!(logs_contain("batcher.batch.flush"));
    assert!(logs_contain("vit.embed.duration_ms"));
}
