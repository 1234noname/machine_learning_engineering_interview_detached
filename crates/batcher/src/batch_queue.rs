// BatchQueue - the deep module that hides all concurrency complexity.
//
// Callers hand over raw image bytes and await an embedding vector. They never
// see a Mutex, a VecDeque, or a oneshot channel. The queue absorbs those
// details so every call site is clean (Ousterhout §4: "pull complexity downward").
//
// Architecture:
//   - `enqueue()` pushes a `(raw_bytes, oneshot::Sender<...>)` tuple onto a
//     shared `Mutex<VecDeque<BatchItem>>` and returns the receiver end of the
//     oneshot so the caller can await the result.
//   - A single background `tokio::spawn` (started in `main.rs`) polls the queue
//     every `max_wait_ms` and flushes whenever the queue reaches `max_batch_size`
//     OR the wait deadline expires - whichever comes first.
//   - The drain task calls `VitService::embed_batch()`, fans results back to
//     callers via their oneshot senders, and loops.
//
// Safety:
//   - `std::sync::Mutex` is used (not `tokio::sync::Mutex`) because the critical
//     section is sub-microsecond push/drain - no `.await` is held inside the lock.
//   - Results are sent via `oneshot::Sender::send()`, which does not block.

use crate::{
    config::BatcherConfig,
    error::BatchError,
    metrics,
    vit_client::{EmbedResult, VitService},
};
use std::{
    collections::VecDeque,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};
use tokio::sync::oneshot;
use tracing::{info_span, instrument, Instrument};

/// L2-normalise an embedding so that pgvector's `<=>` cosine-distance operator
/// gives correct results. The batcher enforces this invariant as a defensive
/// measure regardless of what the upstream ViT service returns.
///
/// Zero vectors are passed through unchanged with a warning - a zero-norm
/// embedding indicates a ViT model bug that callers should investigate.
fn l2_normalize(v: Vec<f32>) -> Vec<f32> {
    let norm: f32 = v.iter().map(|x| x * x).sum::<f32>().sqrt();
    if norm < f32::EPSILON {
        tracing::warn!(
            dim = v.len(),
            "ViT returned zero-norm embedding; passing through unchanged"
        );
        return v;
    }
    if (norm - 1.0).abs() > 1e-4 {
        tracing::debug!(norm, "normalising ViT embedding to unit vector");
    }
    v.into_iter().map(|x| x / norm).collect()
}

/// A single item waiting in the batch queue.
struct BatchItem {
    /// Raw image bytes (encoding-agnostic; base64 decode happens in the handler).
    image: Vec<u8>,
    /// Channel to send the embedding + attributes result back to the awaiting
    /// handler.
    tx: oneshot::Sender<Result<EmbedResult, BatchError>>,
}

/// Shared state for the drain task - tracks when the first item arrived so
/// the timeout is measured from the first enqueue, not from epoch.
struct QueueState {
    items: VecDeque<BatchItem>,
    /// Instant of the first item in the current (unflushed) batch.
    first_item_at: Option<Instant>,
}

impl QueueState {
    fn new() -> Self {
        QueueState {
            items: VecDeque::new(),
            first_item_at: None,
        }
    }
}

/// The deep module. Holds a `Arc<dyn VitService>` so `VitClient` or a mock
/// can be injected at construction time.
pub struct BatchQueue {
    state: Arc<Mutex<QueueState>>,
    vit: Arc<dyn VitService>,
    config: BatcherConfig,
}

impl BatchQueue {
    /// Construct a `BatchQueue` with the given config and ViT service implementation.
    pub fn new(config: BatcherConfig, vit: Arc<dyn VitService>) -> Self {
        BatchQueue {
            state: Arc::new(Mutex::new(QueueState::new())),
            vit,
            config,
        }
    }

    /// Return the current number of items waiting in the queue.
    ///
    /// Used for OTel span attributes on `batcher.embed.queued`.
    pub fn depth(&self) -> usize {
        self.state.lock().expect("queue mutex poisoned").items.len()
    }

    /// Enqueue raw image bytes and await the embedding result.
    ///
    /// The caller blocks on the oneshot receiver until the drain task flushes
    /// this item's batch and sends back the result. The OTel span
    /// `batcher.embed.queued` wraps this entire operation (enqueue → result).
    ///
    /// # Errors
    ///
    /// - `BatchError::VitServiceUnavailable` if the upstream call fails.
    /// - `BatchError::Timeout` if the oneshot times out (> `max_wait_ms × 10`).
    /// - `BatchError::ChannelClosed` if the drain task drops the sender (internal error).
    #[instrument(name = "batcher.embed.queued", skip(self, image), fields(queue_depth))]
    pub async fn enqueue(&self, image: Vec<u8>) -> Result<EmbedResult, BatchError> {
        let (tx, rx) = oneshot::channel();

        // Record depth before push so the span attribute reflects the queue
        // depth at the moment this item arrived.
        let depth = {
            let mut guard = self.state.lock().expect("queue mutex poisoned");
            if guard.first_item_at.is_none() {
                guard.first_item_at = Some(Instant::now());
            }
            guard.items.push_back(BatchItem { image, tx });
            guard.items.len()
        };

        // Update Prometheus gauge to reflect the new queue depth.
        metrics::QUEUE_DEPTH.inc();

        tracing::Span::current().record("queue_depth", depth);
        tracing::debug!(queue_depth = depth, "embed request enqueued");

        // Caller waits up to vit_timeout_ms for the drain task to complete
        // the model HTTP call. This is independent of max_wait_ms (batch fill
        // wait) and must cover cold-start latency on remote GPU backends.
        let timeout_ms = self.config.vit_timeout_ms;
        let timeout_duration = Duration::from_millis(timeout_ms);

        tokio::time::timeout(timeout_duration, rx)
            .await
            .map_err(|_| BatchError::Timeout { timeout_ms })?
            .map_err(|_| BatchError::ChannelClosed)?
    }

    /// The long-lived background drain task. Call once from `main.rs` inside
    /// a `tokio::spawn`. This method loops forever - it never returns.
    ///
    /// Flush conditions (whichever comes first):
    ///   (a) queue length reaches `max_batch_size`
    ///   (b) `max_wait_ms` has elapsed since the first item was enqueued
    pub async fn run_drain_task(self: Arc<Self>) {
        loop {
            // Poll interval: sleep for max_wait_ms, then check.
            tokio::time::sleep(Duration::from_millis(self.config.max_wait_ms)).await;
            self.maybe_flush().await;
        }
    }

    /// Check flush conditions and drain the queue if either is met.
    ///
    /// Returns immediately without flushing if the queue is empty or neither
    /// condition is satisfied.
    async fn maybe_flush(&self) {
        // Drain items under the lock (sub-microsecond critical section).
        let (items, flush_start): (Vec<BatchItem>, Option<Instant>) = {
            let mut guard = self.state.lock().expect("queue mutex poisoned");

            if guard.items.is_empty() {
                return;
            }

            let should_flush = {
                let size_reached = guard.items.len() >= self.config.max_batch_size;
                let time_elapsed = guard
                    .first_item_at
                    .map(|t| t.elapsed() >= Duration::from_millis(self.config.max_wait_ms))
                    .unwrap_or(false);
                size_reached || time_elapsed
            };

            if !should_flush {
                return;
            }

            // Capture timing info before draining.
            let start = guard.first_item_at.take();

            // Take at most max_batch_size items so concurrent drain tasks can
            // pick up the remainder in parallel rather than one task swallowing
            // the entire queue. Sending >max_batch_size to the model would also
            // hit unwarmed CUDA graph shapes and stall inference.
            let count = guard.items.len().min(self.config.max_batch_size);
            let drained: Vec<BatchItem> = guard.items.drain(..count).collect();

            // If items remain after a partial drain, restart their wait timer
            // so the next drain task picks them up within max_wait_ms.
            if !guard.items.is_empty() {
                guard.first_item_at = Some(Instant::now());
            }

            (drained, start)
        };

        // Update Prometheus metrics now that we've committed to flushing.
        let batch_len = items.len();
        metrics::QUEUE_DEPTH.sub(batch_len as f64);
        if let Some(start) = flush_start {
            metrics::FLUSH_LATENCY.observe(start.elapsed().as_secs_f64());
        }

        // Outside the lock: call the ViT service.
        self.flush_batch(items).await;
    }

    /// Call the ViT service with a batch and fan results back to callers.
    ///
    /// The OTel span `batcher.batch.flush` wraps this operation.
    async fn flush_batch(&self, items: Vec<BatchItem>) {
        let batch_size = items.len();
        let span = info_span!("batcher.batch.flush", batch_size);

        async move {
            tracing::debug!(batch_size, "flushing batch to ViT service");
            let images: Vec<Vec<u8>> = items.iter().map(|i| i.image.clone()).collect();

            match self.vit.embed_batch(images).await {
                Ok(results) => {
                    // Enforce the upstream contract at the fan-out boundary: the
                    // model MUST return exactly one result per image SENT.
                    // `parse_embed_response` only guarantees embeddings.len() ==
                    // attributes.len(), NOT results.len() == items.len(). A
                    // wrong-count response would otherwise silently truncate via
                    // the positional zip (trailing callers get an opaque
                    // ChannelClosed/502). Mirror the parse-time length guard here
                    // and fan an explicit contract violation to EVERY caller.
                    if results.len() != items.len() {
                        tracing::error!(
                            sent = items.len(),
                            got = results.len(),
                            "ViT returned wrong result count"
                        );
                        // MalformedVitResponse wraps a String (Clone-able), so we
                        // construct the same message per caller. This makes the
                        // handler's MalformedVitResponse → 502 arm reachable on the
                        // batched path with its specific log.
                        let msg = format!(
                            "ViT returned {} results for {} images",
                            results.len(),
                            items.len()
                        );
                        for item in items.into_iter() {
                            let _ = item
                                .tx
                                .send(Err(BatchError::MalformedVitResponse(msg.clone())));
                        }
                        return;
                    }

                    // Equal-length (normal) case: fan results back to each caller
                    // by index. L2-normalise the embedding ONLY - the batcher owns
                    // this invariant so callers never receive a non-unit embedding.
                    // Attributes are classifier outputs and pass through verbatim.
                    for (item, result) in items.into_iter().zip(results.into_iter()) {
                        let result = EmbedResult {
                            embedding: l2_normalize(result.embedding),
                            attributes: result.attributes,
                        };
                        // Ignore send errors: the caller may have timed out.
                        let _ = item.tx.send(Ok(result));
                    }
                }
                Err(e) => {
                    // Log the original error once. Because `BatchError` isn't
                    // `Clone` (e.g. VitServiceUnavailable wraps a non-Clone
                    // reqwest::Error), we can't fan the original variant to every
                    // caller. Instead, every caller receives a `ChannelClosed`
                    // sentinel, which the handler maps to HTTP 502 - the same
                    // 502-class outcome callers would observe for the underlying
                    // upstream fault. (The wrong-count case in the Ok arm above is
                    // the one exception: it fans MalformedVitResponse, since that
                    // variant wraps a Clone-able String.)
                    tracing::error!(error = %e, "ViT embed_batch failed");
                    for item in items.into_iter() {
                        let _ = item.tx.send(Err(BatchError::ChannelClosed));
                    }
                }
            }
        }
        .instrument(span)
        .await;
    }
}
