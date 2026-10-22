//! Disposable-only native UI acceptance executable. Does not change erida-api.
#[path = "../preview_ui/mod.rs"]
mod preview_ui;
use erida_api::{app, AppState};
use std::net::SocketAddr;

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    for flag in ["ERIDA_RUST_UI_PREVIEW", "ERIDA_RUST_TEST_WRITES"] {
        if !std::env::var(flag).is_ok_and(|value| value == "1") {
            return Err("UI preview requires explicit disposable test mode".into());
        }
    }
    let address: SocketAddr = std::env::var("ERIDA_RUST_ADDR")
        .unwrap_or_else(|_| "127.0.0.1:18492".into()).parse()?;
    if !address.ip().is_loopback() {
        return Err("UI preview must bind to loopback".into());
    }
    // Validates the canonical SQLite path against the explicit temporary root.
    let state = AppState::from_env().map_err(|_| "Unsafe UI preview initialization")?;
    let listener = tokio::net::TcpListener::bind(address).await?;
    axum::serve(listener, app(state).merge(preview_ui::router())
        .into_make_service_with_connect_info::<SocketAddr>())
        .with_graceful_shutdown(async { let _ = tokio::signal::ctrl_c().await; })
        .await?;
    Ok(())
}
