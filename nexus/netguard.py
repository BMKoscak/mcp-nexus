"""SSRF guard: refuse requests to loopback, private, link-local and reserved addresses."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


class BlockedAddressError(ValueError):
    pass


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def check_url(url: str, *, block_private: bool = True) -> None:
    """Raise :class:`BlockedAddressError` unless ``url`` is http(s) to a public address."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise BlockedAddressError(f"Only http and https URLs are allowed (got {parts.scheme!r}).")
    host = parts.hostname
    if not host:
        raise BlockedAddressError("URL has no host.")
    if not block_private:
        return
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, parts.port or 443, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise BlockedAddressError(f"Could not resolve {host!r}: {exc}") from None
        ips = [ipaddress.ip_address(info[4][0]) for info in infos]
    for ip in ips:
        if not _is_public(ip):
            raise BlockedAddressError(
                f"{host!r} resolves to a non-public address ({ip}); "
                "blocked by NEXUS_FETCH_BLOCK_PRIVATE."
            )
