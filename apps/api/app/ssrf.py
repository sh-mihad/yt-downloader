"""Server-Side Request Forgery (SSRF) guard for the downloader's URL input.

Trimmed from Converter's app/core/ssrf.py: keeps only the synchronous
`validate_public_url` check this feature needs (DNS resolve + block of private,
link-local and cloud-metadata addresses). The original module also has async
redirect-following helpers (`open_public`, `read_capped`, `validate_public_url_async`)
that the downloader never calls, so they're dropped here along with the httpx
dependency they needed.
"""

import ipaddress
import socket
from urllib.parse import urlparse

from fastapi import HTTPException

# Blocked private and link-local networks (IPv4 and IPv6); `is_ip_blocked` also refuses
# anything not globally routable.
BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),  # Loopback
    ipaddress.ip_network("10.0.0.0/8"),  # Private RFC 1918
    ipaddress.ip_network("172.16.0.0/12"),  # Private RFC 1918
    ipaddress.ip_network("192.168.0.0/16"),  # Private RFC 1918
    ipaddress.ip_network("100.64.0.0/10"),  # Carrier-grade NAT (RFC 6598)
    ipaddress.ip_network("169.254.0.0/16"),  # Link-local / Cloud metadata
    ipaddress.ip_network("0.0.0.0/8"),  # Current network
    ipaddress.ip_network("::1/128"),  # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),  # IPv6 Unique Local Address
    ipaddress.ip_network("fe80::/10"),  # IPv6 Link-local
]
NAT64 = ipaddress.ip_network("64:ff9b::/96")  # carries an IPv4 address in its last 32 bits

BLOCKED = "That address can't be used. Try a public video link."
NOT_FOUND = "We couldn't find that link. Check the address and try again."
NOT_A_URL = "That doesn't look like a web address. Check it and try again."


def is_ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if `ip` falls inside any blocked private/link-local range, or isn't globally
    routable. IPv6 forms that carry an IPv4 address (::ffff:a.b.c.d, 6to4, NAT64) are
    judged by that address too."""
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour
        if embedded is None and ip in NAT64:
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None and is_ip_blocked(embedded):
            return True
    return (
        any(ip in net for net in BLOCKED_NETWORKS)
        or not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _checked(url_str: str, add_scheme: bool) -> tuple[str, str]:
    url_str = url_str.strip()
    if not url_str:
        raise HTTPException(400, "Paste a link first.")

    if not url_str.startswith(("http://", "https://")):
        if not add_scheme:
            raise HTTPException(400, "That doesn't look like a web address. It should start with http:// or https://.")
        url_str = f"https://{url_str}"

    try:
        parsed = urlparse(url_str)
    except Exception:
        raise HTTPException(400, NOT_A_URL) from None

    if parsed.scheme not in ("http", "https"):
        raise HTTPException(400, "That doesn't look like a web address. It should start with http:// or https://.")

    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(400, NOT_A_URL)

    lower_host = hostname.lower().rstrip(".")
    if lower_host in ("localhost", "127.0.0.1", "::1", "metadata.google.internal") or lower_host.endswith(
        (".local", ".localhost")
    ):
        raise HTTPException(400, BLOCKED)
    return url_str, hostname


def _public_ips(addr_info: list) -> list[str]:
    ips: list[str] = []
    for item in addr_info:
        raw_ip = str(item[4][0])
        try:
            ip_obj = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue
        if is_ip_blocked(ip_obj):
            raise HTTPException(400, BLOCKED)
        ips.append(raw_ip)
    if not ips:
        raise HTTPException(400, NOT_FOUND)
    return sorted(dict.fromkeys(ips), key=lambda ip: ":" in ip)


def validate_public_url(url_str: str, *, add_scheme: bool = True) -> str:
    """Validates that a URL is a public HTTP(S) address — raises HTTPException(400) for a
    missing/private/internal/blocked one, else returns the (possibly scheme-completed) URL.
    `add_scheme=False` refuses a URL without http(s):// instead of assuming https (for a
    stream URL a site handed us, not one a person typed)."""
    url_str, hostname = _checked(url_str, add_scheme)
    try:
        addr_info = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise HTTPException(400, NOT_FOUND) from None
    _public_ips(addr_info)
    return url_str
