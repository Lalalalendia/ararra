//! M1j: opt-in, fail-closed client identity from a trusted local reverse proxy.
//!
//! Never consume X-Forwarded-For directly: it may contain a user-provided
//! prefix. The deployment must configure Nginx to OVERWRITE the dedicated
//! X-ReadEra-Client-IP header with $remote_addr on every proxied request.
//! This module does not enable production Rust writes or claim live Nginx proof.
use super::ApiError;
use axum::http::HeaderMap;
use std::net::{IpAddr, SocketAddr};

const CLIENT_HEADER: &str = "x-readera-client-ip";

/// Return a stable, privacy-preserving *input* to the existing SHA256 throttle.
/// The raw IP must never be persisted or logged.
///
/// Default mode: TCP peer is the only authority, and all incoming proxy claims
/// are ignored. In explicit loopback-proxy mode, accept EXACTLY ONE dedicated
/// header, from an immediate loopback peer; reject missing, malformed, duplicate
/// or ambiguous headers before charging the login attempt counter.
pub fn login_client_ip(
    peer: SocketAddr,
    headers: &HeaderMap,
    trust_loopback_proxy: bool,
) -> Result<String, ApiError> {
    if !trust_loopback_proxy {
        return Ok(peer.ip().to_string());
    }
    if !peer.ip().is_loopback() {
        return Err(ApiError::TrustedClientIpUnavailable);
    }
    let mut values = headers.get_all(CLIENT_HEADER).iter();
    let value = values.next().ok_or(ApiError::TrustedClientIpUnavailable)?;
    if values.next().is_some() {
        return Err(ApiError::TrustedClientIpUnavailable);
    }
    let raw = value.to_str().map_err(|_| ApiError::TrustedClientIpUnavailable)?;
    if raw.is_empty() || raw.trim() != raw || raw.contains(',')
        || raw.contains('%') || raw.len() > 45
    {
        return Err(ApiError::TrustedClientIpUnavailable);
    }
    let parsed: IpAddr = raw.parse().map_err(|_| ApiError::TrustedClientIpUnavailable)?;
    if parsed.is_unspecified() || parsed.is_multicast() {
        return Err(ApiError::TrustedClientIpUnavailable);
    }
    Ok(parsed.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::HeaderValue;

    fn peer() -> SocketAddr {
        "127.0.0.1:4321".parse().unwrap()
    }
    fn headers(ip: &str) -> HeaderMap {
        let mut h = HeaderMap::new();
        h.insert(CLIENT_HEADER, HeaderValue::from_str(ip).unwrap());
        h
    }

    #[test]
    fn default_ignores_spoofed_proxy_headers() {
        let mut h = headers("198.51.100.99");
        h.insert("x-forwarded-for", HeaderValue::from_static("203.0.113.100"));
        assert_eq!(login_client_ip(peer(), &h, false).unwrap(), "127.0.0.1");
        assert_eq!(login_client_ip(peer(), &HeaderMap::new(), false).unwrap(), "127.0.0.1");
    }

    #[test]
    fn explicit_proxy_mode_requires_local_tcp_peer_and_one_valid_header() {
        let mut h = headers("198.51.100.11");
        h.insert("x-forwarded-for", HeaderValue::from_static("203.0.113.200, 198.51.100.7"));
        assert_eq!(login_client_ip(peer(), &h, true).unwrap(), "198.51.100.11");
        let external_peer: SocketAddr = "198.51.100.99:4444".parse().unwrap();
        assert!(login_client_ip(external_peer, &h, true).is_err());
        assert!(login_client_ip(peer(), &HeaderMap::new(), true).is_err());
        h.append(CLIENT_HEADER, HeaderValue::from_static("198.51.100.12"));
        assert!(login_client_ip(peer(), &h, true).is_err());
    }

    #[test]
    fn invalid_ip_forms_are_fail_closed() {
        for bad in [
            "not-an-ip", "198.51.100.1:456", "198.51.100.1,198.51.100.2",
            "0.0.0.0", "ff02::1", "::", "[2001:db8::1]",
            "fe80::1%eth0", " 198.51.100.1", "198.51.100.1 ",
        ] {
            assert!(login_client_ip(peer(), &headers(bad), true).is_err(), "{bad}");
        }
    }

    #[test]
    fn valid_ipv4_and_ipv6_stay_independent() {
        assert_eq!(login_client_ip(peer(), &headers("192.0.2.15"), true).unwrap(),
                   "192.0.2.15");
        assert_eq!(login_client_ip(peer(), &headers("2001:db8::17"), true).unwrap(),
                   "2001:db8::17");
        let v6peer: SocketAddr = "[::1]:4444".parse().unwrap();
        assert_eq!(login_client_ip(v6peer, &headers("203.0.113.15"), true).unwrap(),
                   "203.0.113.15");
    }
}
