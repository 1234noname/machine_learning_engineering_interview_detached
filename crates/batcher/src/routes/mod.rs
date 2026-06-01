pub mod embed;
pub mod health;

/// GET /metrics - Prometheus text exposition.
pub async fn metrics() -> impl axum::response::IntoResponse {
    let body = crate::metrics::render();
    (
        [(
            axum::http::header::CONTENT_TYPE,
            "text/plain; version=0.0.4",
        )],
        body,
    )
}
