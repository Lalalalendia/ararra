"""Public HTTPS transport for MED-backed source collection.

Adapted from uploaded book_crawler/http_transport.py: TCP addresses are
validated at actual connection time and passed directly to the network
backend while the URL hostname stays intact for TLS SNI/certificate checking.
No HTTP proxy, private ranges, cross-origin redirects or access-control bypass.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable
from typing import Protocol
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend
from httpcore._backends.base import SOCKET_OPTION

from .source_catalog_med import IngestError, validate_url
from .source_request_limiter import SourceRequestLimiter, SourceSiteBusy
from .source_site_profiles import site_profile

USER_AGENT = "ReadEraSourceCollector/1.0 (https://github.com/Lalalalendia/ararra; public HTML; no login)"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MIN_REQUEST_INTERVAL = 1.5
TIMEOUT = httpx.Timeout(connect=8.0, read=15.0, write=8.0, pool=8.0)

Resolver = Callable[[str, int], Awaitable[tuple[str, ...]]]


async def resolve_tcp_addresses(host: str, port: int) -> tuple[str, ...]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP,
    )
    return tuple(dict.fromkeys(str(info[4][0]) for info in infos if info[4]))


def is_public_ip(value: str) -> bool:
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return False
    return bool(
        addr.is_global and not addr.is_private and not addr.is_loopback
        and not addr.is_link_local and not addr.is_reserved
        and not addr.is_multicast and not addr.is_unspecified
    )


class TCPBackend(Protocol):
    async def connect_tcp(
        self, host: str, port: int, timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream: ...

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream: ...

    async def sleep(self, seconds: float) -> None: ...


class PinnedPublicNetworkBackend(httpcore.AsyncNetworkBackend):
    """Never let the underlying TCP implementation re-resolve the hostname."""

    def __init__(
        self, *, backend: TCPBackend | None = None,
        resolver: Resolver = resolve_tcp_addresses,
    ) -> None:
        self._backend = backend or AutoBackend()
        self._resolver = resolver

    async def connect_tcp(
        self, host: str, port: int, timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            addresses = await self._resolver(host, port)
        except OSError as exc:
            raise httpcore.ConnectError("DNS resolution failed") from exc
        if not addresses or any(not is_public_ip(address) for address in addresses):
            raise httpcore.ConnectError("Resolved address is missing or not public")
        last: Exception | None = None
        for address in addresses:
            try:
                return await self._backend.connect_tcp(
                    address, port, timeout=timeout,
                    local_address=local_address, socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last = exc
        raise httpcore.ConnectError("Unable to connect to public address") from last

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("Unix sockets are forbidden")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class PinnedPublicHTTPTransport(httpx.AsyncHTTPTransport):
    """Fail closed if httpx/httpcore's internal network interface changes."""

    def __init__(self, resolver: Resolver = resolve_tcp_addresses) -> None:
        super().__init__(
            verify=True, trust_env=False, http2=False, retries=0,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=0),
        )
        pool = getattr(self, "_pool", None)
        if pool is None or not isinstance(pool, httpcore.AsyncConnectionPool):
            raise RuntimeError("Pinned public HTTP network backend unavailable")
        if not hasattr(pool, "_network_backend"):
            raise RuntimeError("No socket-pinning capability; source collector disabled")
        pool._network_backend = PinnedPublicNetworkBackend(resolver=resolver)


class PublicHtmlFetcher:
    """Robots-aware public HTTP; shared pacing when supplied a SQLite limiter."""

    def __init__(self, *, transport_factory=None,
                 request_limiter: SourceRequestLimiter | None = None) -> None:
        self._transport_factory = transport_factory or PinnedPublicHTTPTransport
        self._request_limiter = request_limiter
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}
        self._robots: dict[str, tuple[RobotFileParser, float, float]] = {}

    async def _paced(self, host: str, delay: float) -> None:
        now = asyncio.get_running_loop().time()
        remainder = self._last.get(host, 0.0) + delay - now
        if remainder > 0:
            await asyncio.sleep(remainder)
        self._last[host] = asyncio.get_running_loop().time()

    async def _raw_fetch_unadmitted(self, url: str, *, allow_robots_missing: bool = False) -> str | None:
        try:
            async with asyncio.timeout(40):
                async with httpx.AsyncClient(
                    transport=self._transport_factory(), timeout=TIMEOUT,
                    trust_env=False, follow_redirects=False,
                    headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
                ) as client:
                    async with client.stream("GET", url) as reply:
                        if allow_robots_missing and reply.status_code in (404, 410):
                            return None
                        if reply.status_code == 429 and self._request_limiter is not None:
                            # Retry-After is untrusted remote data. Accept seconds
                            # only, bounded to one hour, and share the cooldown.
                            try:
                                retry = int(reply.headers.get("retry-after", "300"))
                            except ValueError:
                                retry = 300
                            raise SourceSiteBusy(max(60, min(3600, retry)), "upstream_429")
                        if reply.status_code != 200:
                            reason = "blocked" if reply.status_code in (301, 302, 303, 307, 308, 401, 402, 403, 429, 451) else "http_error"
                            raise IngestError(reason, f"Source HTTP {reply.status_code}")
                        media = reply.headers.get("content-type", "").lower().split(";", 1)[0].strip()
                        if media not in ("text/html", "application/xhtml+xml", "text/plain"):
                            raise IngestError("invalid_response", "Unexpected source MIME type")
                        try:
                            declared_length = int(reply.headers.get("content-length", "0") or 0)
                        except ValueError as exc:
                            raise IngestError("invalid_response", "Invalid content length") from exc
                        if declared_length > MAX_RESPONSE_BYTES:
                            raise IngestError("too_large", "HTTP source exceeds size limit")
                        buf = bytearray()
                        async for chunk in reply.aiter_bytes():
                            if len(buf) + len(chunk) > MAX_RESPONSE_BYTES:
                                raise IngestError("too_large", "Decoded HTTP source exceeds size limit")
                            buf.extend(chunk)
                        raw = bytes(buf)
                        if not raw:
                            raise IngestError("missing_body", "Empty source response")
                        encodings = ("utf-8-sig", "gb18030") if "uukanshu.cc" in urlsplit(url).hostname else ("utf-8-sig",)
                        for enc in encodings:
                            try:
                                return raw.decode(enc)
                            except UnicodeDecodeError:
                                continue
                        raise IngestError("invalid_response", "Unsupported source encoding")
        except (httpx.HTTPError, OSError, RuntimeError, TimeoutError) as exc:
            if isinstance(exc, IngestError):
                raise
            raise IngestError("network_error", type(exc).__name__) from exc

    async def _raw_fetch(self, source: str, url: str, *, delay: float,
                         allow_robots_missing: bool = False) -> str | None:
        limiter = self._request_limiter
        if limiter is None:
            return await self._raw_fetch_unadmitted(
                url, allow_robots_missing=allow_robots_missing)
        host = urlsplit(url).hostname or ""
        async with limiter.slot(source, host, delay=delay):
            try:
                return await self._raw_fetch_unadmitted(
                    url, allow_robots_missing=allow_robots_missing)
            except SourceSiteBusy as exc:
                if exc.reason == "upstream_429":
                    limiter.cool_down(source, exc.retry_after)
                raise

    async def fetch(self, source: str, url: str) -> str:
        validate_url(url, source)
        parts = urlsplit(url)
        host = parts.hostname or ""
        profile = site_profile(source)
        minimum_delay = (max(MIN_REQUEST_INTERVAL, profile.minimum_delay_seconds)
                         if self._request_limiter is not None else MIN_REQUEST_INTERVAL)
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = asyncio.get_running_loop().time()
            cached = self._robots.get(host)
            if cached is None or now - cached[2] > 300:
                await self._paced(host, minimum_delay)
                robots_text = await self._raw_fetch(
                    source, f"https://{host}/robots.txt", delay=minimum_delay,
                    allow_robots_missing=True)
                parser = RobotFileParser()
                if robots_text is None:
                    parser.parse(["User-agent: *", "Allow: /"])
                else:
                    if "<html" in robots_text[:1000].lower() or len(robots_text) > 250_000:
                        raise IngestError("blocked", "Invalid robots policy response")
                    parser.parse(robots_text.splitlines())
                delay = max(minimum_delay, float(parser.crawl_delay(USER_AGENT) or 0.0))
                if delay > 20:
                    raise IngestError("blocked", "Robots crawl-delay exceeds interactive collection limit")
                self._robots[host] = (parser, delay, now)
            parser, delay, _ = self._robots[host]
            if not parser.can_fetch(USER_AGENT, url):
                raise IngestError("blocked", "Site robots policy disallows requested path")
            await self._paced(host, delay)
            result = await self._raw_fetch(source, url, delay=delay)
            if result is None:
                raise IngestError("missing_body", "Unexpected empty source")
            return result
