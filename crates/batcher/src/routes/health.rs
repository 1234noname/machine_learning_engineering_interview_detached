// GET /health - liveness probe. Returns {"status":"ok"} with HTTP 200.
// Callers should not need to know anything about the server's internals
// to interpret a healthy response.

use axum::Json;
use serde_json::{json, Value};

pub async fn handler() -> Json<Value> {
    Json(json!({"status": "ok"}))
}
