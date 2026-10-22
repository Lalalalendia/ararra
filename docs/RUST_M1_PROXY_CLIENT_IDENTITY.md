# RUST-M1j — trustworthy client IP for browser login behind a local proxy

## Evidence and threat

Rust M1g initially obtains the login peer from Axum ConnectInfo<Tcp SocketAddr>. On a public application with a loopback Nginx -> Rust hop, the immediate peer is localhost. That collapses all external users into **one** browser_login_rate IP bucket and can cause cross-user denial of login after a small number of failures.

The Python command's existing explicit proxy boundary uses Uvicorn proxy_headers=True and forwarded_allow_ips=127.0.0.1 only for a loopback bind; non-loopback binds disable proxy headers. M1j follows the same fail-closed principle but uses a dedicated header instead of blindly parsing X-Forwarded-For.

Nginx authoritative references:
- [proxy_set_header](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_set_header) can replace an upstream request header with a server-derived variable.
- [ngx_http_realip_module](https://nginx.org/en/docs/http/ngx_http_realip_module.html) requires explicit trusted source addresses to replace Nginx's own client address. Incorrect set_real_ip_from / real_ip_header configuration can make remote addresses attacker-controlled.

## Contract

Default (no new environment variable): Rust login continues ignoring ALL forwarded headers and throttles by immediate TCP peer as before. No production behavior changes.

Explicit experiment, on the already loopback-only process:

    ERIDA_RUST_TRUST_LOOPBACK_PROXY_CLIENT_IP=1

When enabled:
1. The immediate kernel TCP peer MUST be loopback. Non-loopback peers are refused before SQLite access.
2. The request must contain **exactly one** X-ReadEra-Client-IP header with a valid single IPv4 or IPv6 literal, without port, scope, comma-separated chain, leading/trailing whitespace, zero/multicast address or non-IP names.
3. Invalid, missing or duplicate claims yield HTTP 400 with a non-sensitive error; no credential is checked and no login attempt counter is updated.
4. X-Forwarded-For and X-Real-IP sent by a client are never parsed by Rust.
5. The canonical parsed IP string is used as input to the unchanged SHA256 domain-separated SQLite throttle, compatible with the Python client IP value. The database stores digests only, never plaintext IP.

These properties require a **deployment-controlled** proxy header, not trust in arbitrary remote headers. The Rust server already refuses binding outside loopback. A same-host process with direct loopback access could otherwise spoof the header; production enablement therefore depends on restricting local access and separately reviewing the Nginx service, process boundaries and privileges.

## Example Nginx change (not applied)

Only in the exact upstream location forwarding login to the loopback Rust process, after verifying that the effective remote address is from the real client:

    proxy_pass http://127.0.0.1:18377;
    proxy_set_header X-ReadEra-Client-IP $remote_addr;
    proxy_set_header X-Forwarded-For "";

This is **not** the current deployed configuration and must not be copy-pasted without `nginx -T`, trusted upstream chain inspection and live verification. If any Nginx real_ip_header is configured, its set_real_ip_from may trust only explicitly controlled upstream IPs, never arbitrary public clients. Do not use `$http_x_forwarded_for` or `$proxy_add_x_forwarded_for` as an authenticated single client identity.

## Automated verification

- Rust peer_identity unit tests: default header-ignoring semantics, strict opt-in loopback origin, duplicates, malformed forms, valid IPv4/IPv6, XFF spoof rejection.
- `scripts/compare_rust_proxy_login_ip.py`: synthetic HTTP requests to actual Rust loopback with the dedicated test header + Python ASGI clients with explicitly assigned test peer addresses, on one disposable SQLite. Verifies both runtimes count failures in the same per-IP bucket, 8-failure lockout at 429 with Retry-After, other IP unaffected, two IPs plus IPv6 produce distinct digests and plaintext secrets/IPs are not stored.

The script is required in Rust CI alongside all prior checks. It **simulates**, but does not launch, actual Nginx.

## Production gates NOT met by M1j

- Inspect and preserve the *effective* deployed Nginx configuration (including nested location inheritance and realip rules) on the actual VPS.
- Demonstrate that Nginx overrides any forged inbound X-ReadEra-Client-IP header and that arbitrary XFF prefixes cannot influence the identity.
- Demonstrate real external clients A and B get independent buckets and different 429 outcomes.
- Confirm Rust's backend port cannot be accessed directly by public clients or untrusted local services.
- Prove key rotation, session revocation, cookies, CSRF and non-production backup/restore continue to work across the proxy.
- Only then consider a separately reviewed production Rust write enablement and complete UI/worker parity gates.

**This PR does not relax ERIDA_RUST_TEST_WRITES or the canonical temp SQLite safety boundary and never changes the VPS/Nginx files.**

References: [ERIDA-RUST-MASTER-01](https://app.notion.com/p/40032a84beec81d09c63f735a37a3eac), [RUST_M1_BROWSER_SESSIONS.md](RUST_M1_BROWSER_SESSIONS.md).
