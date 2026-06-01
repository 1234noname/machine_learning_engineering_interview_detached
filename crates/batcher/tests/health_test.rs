// Integration test: spawns the axum app on a random OS-assigned port and
// asserts GET /health returns 200 OK with the expected JSON body.
// Per testing.md §Test-first: this test is written before the route exists.

use std::collections::HashMap;

#[tokio::test]
async fn health_returns_200_with_json_body() {
    // Bind a random OS-assigned port so tests never conflict.
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("failed to bind listener");
    let addr = listener.local_addr().expect("no local addr");

    // Spawn the app on the bound listener.
    let app = avsa_batcher::router();
    tokio::spawn(async move {
        axum::serve(listener, app).await.expect("server error");
    });

    let client = reqwest::Client::new();
    let url = format!("http://{addr}/health");

    let response = client.get(&url).send().await.expect("request failed");

    assert_eq!(
        response.status().as_u16(),
        200,
        "expected HTTP 200, got {}",
        response.status()
    );

    // Assert Content-Type header contains application/json.
    let content_type = response
        .headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    assert!(
        content_type.contains("application/json"),
        "expected Content-Type: application/json, got: {content_type}"
    );

    // Assert body deserialises to {"status":"ok"}.
    let body: HashMap<String, String> = response
        .json()
        .await
        .expect("response body is not valid JSON");

    assert_eq!(
        body.get("status").map(String::as_str),
        Some("ok"),
        r#"expected {{"status":"ok"}}, got {body:?}"#
    );
}
