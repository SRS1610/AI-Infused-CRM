"""Outbound requests to addresses that users configure (webhooks, automation webhooks, CalDAV servers).

:func:`check_outbound_url` refuses URLs that would let a user make the server call into its own network
(server-side request forgery): only ``http``/``https``, and — in production, unless
``TESSERA_OUTBOUND_ALLOW_PRIVATE`` says otherwise — the host must resolve to public addresses only (no
private, loopback, link-local such as the cloud metadata service, multicast or reserved range).

The check resolves the name once; the HTTP client resolves it again when it connects, so a name whose
answer changes in between (DNS rebinding) is not covered. Callers never follow redirects. The enrichment
fetcher (``modules/contacts/website.py``) pins the checked address and is the model for a stricter client.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable

import httpx

from tessera.config import get_settings
from tessera.exceptions import UserError
from tessera.i18n import _

Resolver = Callable[[str, int], list[str]]


class OutboundRefused(UserError):
    """The address may not be called from the server."""


def system_resolver(host: str, port: int) -> list[str]:
    return list(dict.fromkeys(str(info[4][0]) for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))


def is_public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_global) and not (ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local)


def check_outbound_url(url: str, *, resolver: Resolver = system_resolver, allow_private: bool | None = None) -> str:
    """Return ``url`` if the server may call it, else raise :class:`OutboundRefused`."""
    try:
        parsed = httpx.URL(str(url))
    except Exception as exc:
        raise OutboundRefused(_("'{url}' is not a valid address", url=url)) from exc
    if parsed.scheme not in ("http", "https") or not parsed.host:
        raise OutboundRefused(_("Only http and https addresses can be called"))
    if allow_private is None:
        allow_private = get_settings().outbound_private_allowed
    if allow_private:
        return str(url)
    host = parsed.host.rstrip(".").lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addresses = resolver(host, port)
        except (OSError, UnicodeError) as exc:
            raise OutboundRefused(_("{host} does not resolve", host=host)) from exc
    if not addresses or not all(is_public_address(a) for a in addresses):
        raise OutboundRefused(_("{host} is not a public address and cannot be called from the server", host=host))
    return str(url)
