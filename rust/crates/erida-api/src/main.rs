//! Loopback-only experimental Rust process. No production symlink switches.
use erida_api::{app, AppState};
use std::net::{IpAddr, SocketAddr};

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let bind = std::env::var("ERIDA_RUST_ADDR")
        .unwrap_or_else(|_| "127.0.0.1:18377".to_string());
    let addr: SocketAddr = bind.parse()?;
    if !matches!(addr.ip(), IpAddr::V4(ip) if ip.is_loopback())
        && !matches!(addr.ip(), IpAddr::V6(ip) if ip.is_loopback())
    {
        return Err("Experimental Rust API must bind to loopback".into());
    }
    let state = AppState::from_env()
        .map_err(|_| "Experimental Rust API cannot safely initialize")?;
    let listener = tokio::net::TcpListener::bind(addr).await?;
    axum::serve(listener, app(state))
        .with_graceful_shutdown(async {
            let _ = tokio::signal::ctrl_c().await;
        })
        .await?;
    Ok(())
}
