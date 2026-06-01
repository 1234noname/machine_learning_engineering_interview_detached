//! Prometheus metrics registry for the batcher.
//!
//! Exposed at GET /metrics on the main HTTP port.
//! Metrics:
//!   avsa_batcher_queue_depth              - gauge: current number of items pending in the batch queue
//!   avsa_batcher_flush_latency_seconds    - histogram: time from first item enqueued to batch flush
//!   avsa_batcher_requests_total{outcome}  - counter: total /embed requests by outcome (ok | bad_request | timeout | bad_gateway)
//!   avsa_batcher_request_latency_seconds  - histogram: end-to-end per-request latency from handler entry to response sent

use lazy_static::lazy_static;
use prometheus::{
    exponential_buckets, register_counter_vec, register_gauge, register_histogram, CounterVec,
    Gauge, Histogram, TextEncoder,
};

lazy_static! {
    pub static ref QUEUE_DEPTH: Gauge = register_gauge!(
        "avsa_batcher_queue_depth",
        "Current number of items pending in the batch queue"
    )
    .expect("failed to register avsa_batcher_queue_depth");
    pub static ref FLUSH_LATENCY: Histogram = register_histogram!(
        "avsa_batcher_flush_latency_seconds",
        "Time from first item enqueued to batch flush",
        exponential_buckets(0.001, 2.0, 12).expect("bad buckets")
    )
    .expect("failed to register avsa_batcher_flush_latency_seconds");
    pub static ref REQUESTS_TOTAL: CounterVec = register_counter_vec!(
        "avsa_batcher_requests_total",
        "Total /embed requests by outcome (ok | bad_request | timeout | bad_gateway)",
        &["outcome"]
    )
    .expect("failed to register avsa_batcher_requests_total");
    pub static ref REQUEST_LATENCY: Histogram = register_histogram!(
        "avsa_batcher_request_latency_seconds",
        "End-to-end per-request latency from handler entry to response sent",
        exponential_buckets(0.01, 2.0, 12).expect("bad buckets")
    )
    .expect("failed to register avsa_batcher_request_latency_seconds");
}

/// Render all registered metrics as a Prometheus text exposition.
pub fn render() -> String {
    let encoder = TextEncoder::new();
    let families = prometheus::gather();
    encoder
        .encode_to_string(&families)
        .unwrap_or_else(|_| String::new())
}
