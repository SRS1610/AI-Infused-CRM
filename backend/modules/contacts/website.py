"""The "Company website" enrichment adapter: reads what a company publishes about itself on its home page.

Given the contact's website (or business e-mail) domain, the adapter fetches ``https://<domain>/`` and reads
the schema.org ``Organization`` data embedded as JSON-LD, then OpenGraph and ``<meta>`` tags, to suggest the
company's name, description, phone, address, social profiles and logo. Every value carries the address of
the page it was read on. No key or account is needed, and only the domain leaves the deployment.

The fetch is guarded against server-side request forgery: https only, port 443, host names only (no IP
literals), every address the name resolves to must be public (no private, loopback, link-local, multicast
or reserved range), and the connection goes to the address that was checked, so a second DNS answer cannot
point it elsewhere. Redirects are followed by hand (at most three), each hop checked the same way. The page
must be HTML, the download stops at :data:`MAX_BYTES` and the whole fetch at :data:`TIMEOUT` seconds.

Like every adapter it only runs once an administrator approves it under *Contacts → Configuration →
Enrichment sources* (adapter key ``company_website``).
"""

from __future__ import annotations

import contextlib
import html
import ipaddress
import json
import re
import socket
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

import httpx

from modules.contacts.enrichment import EnrichmentResult, register_adapter
from tessera.logging import get_logger

log = get_logger(__name__)

TIMEOUT = 8.0  # seconds for the whole fetch, redirects included
MAX_BYTES = 1_000_000
MAX_REDIRECTS = 3
USER_AGENT = "Tessera-Enrichment/1.0 (+company website lookup)"
HOSTNAME = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,62}$")
ORG_TYPES = {
    "organization",
    "corporation",
    "localbusiness",
    "ngo",
    "onlinebusiness",
    "onlinestore",
    "store",
    "professionalservice",
    "educationalorganization",
    "governmentorganization",
    "medicalorganization",
    "newsmediaorganization",
    "sportsorganization",
    "researchorganization",
    "airline",
    "consortium",
    "cooperative",
    "fundingscheme",
    "librarysystem",
    "workersunion",
    "politicalparty",
}
SOCIAL_HOSTS = {
    "linkedin.com": ("/company/", "/school/", "/in/", "/showcase/"),
    "twitter.com": None,
    "x.com": None,
    "facebook.com": None,
    "instagram.com": None,
    "youtube.com": None,
    "github.com": None,
    "xing.com": ("/pages/", "/companies/"),
    "tiktok.com": None,
    "mastodon.social": None,
    "bsky.app": ("/profile/",),
    "threads.net": None,
    "pinterest.com": None,
}
SHARE_PATHS = ("/sharer", "/share", "/intent/", "/dialog/", "/plugins/", "/home?status", "/hashtag/")

Resolver = Callable[[str, int], list[str]]


class FetchRefused(Exception):
    """The address may not be fetched (not https, not public, not HTML, too many redirects …)."""


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(ip.is_global) and not (ip.is_multicast or ip.is_reserved or ip.is_loopback or ip.is_link_local)


def check_url(url: str, resolver: Resolver) -> tuple[httpx.URL, str]:
    """(URL, public address to connect to) — or :class:`FetchRefused`."""
    try:
        parsed = httpx.URL(url)
    except Exception as exc:
        raise FetchRefused(f"Invalid address {url!r}") from exc
    if parsed.scheme != "https":
        raise FetchRefused("Only https addresses are fetched")
    if parsed.port not in (None, 443):
        raise FetchRefused("Only the standard https port is fetched")
    host = (parsed.host or "").lower().rstrip(".")
    if not HOSTNAME.match(host):
        raise FetchRefused(f"{host or 'The address'} is not a public host name")
    try:
        addresses = resolver(host, 443)
    except (OSError, UnicodeError) as exc:
        raise FetchRefused(f"{host} does not resolve") from exc
    if not addresses:
        raise FetchRefused(f"{host} does not resolve")
    private = [a for a in addresses if not is_public(a)]
    if private:
        raise FetchRefused(f"{host} resolves to a non-public address")
    return parsed, addresses[0]


@dataclass
class Page:
    url: str
    html: str


def fetch(
    url: str,
    *,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver = system_resolver,
    timeout: float = TIMEOUT,
    max_bytes: int = MAX_BYTES,
) -> Page:
    """GET a public https page with the protections described in the module docstring."""
    deadline = time.monotonic() + timeout
    with httpx.Client(transport=transport, trust_env=False, follow_redirects=False) as client:
        for _hop in range(MAX_REDIRECTS + 1):
            target, address = check_url(url, resolver)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FetchRefused("The site took too long to answer")
            pinned = target.copy_with(host=address)  # connect to the checked address, not to a new DNS answer
            request = client.build_request(
                "GET",
                pinned,
                headers={
                    "Host": target.netloc.decode("ascii"),
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml",
                },
                extensions={"sni_hostname": target.host, "timeout": httpx.Timeout(remaining).as_dict()},
            )
            try:
                resp = client.send(request, stream=True)
            except httpx.HTTPError as exc:
                raise FetchRefused(f"{target.host} could not be reached: {exc.__class__.__name__}") from exc
            try:
                if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                    url = str(target.join(resp.headers["location"]))
                    continue
                if resp.status_code != 200:
                    raise FetchRefused(f"{target.host} answered HTTP {resp.status_code}")
                kind = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if kind not in ("text/html", "application/xhtml+xml"):
                    raise FetchRefused(f"{target} is not an HTML page")
                body = bytearray()
                for chunk in resp.iter_bytes():
                    body.extend(chunk)
                    if len(body) >= max_bytes or time.monotonic() > deadline:
                        break  # the head and the structured data come first; the rest is not needed
                encoding = resp.charset_encoding or "utf-8"
                try:
                    text = bytes(body[:max_bytes]).decode(encoding, errors="replace")
                except LookupError:
                    text = bytes(body[:max_bytes]).decode("utf-8", errors="replace")
                return Page(str(target), text)
            except httpx.HTTPError as exc:
                raise FetchRefused(f"{target.host} could not be read: {exc.__class__.__name__}") from exc
            finally:
                resp.close()
    raise FetchRefused("Too many redirects")


# -------------------------------------------------------------------------------------------- parsing


@dataclass
class PageData:
    meta: dict[str, str] = field(default_factory=dict)
    json_ld: list[Any] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    title: str = ""


class _Reader(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.data = PageData()
        self._script: list[str] | None = None
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").strip().lower()
            if key and a.get("content") and key not in self.data.meta:
                self.data.meta[key] = a["content"]
        elif tag == "script" and a.get("type", "").split(";")[0].strip().lower() == "application/ld+json":
            self._script = []
        elif tag == "a" and a.get("href"):
            self.data.links.append(a["href"].strip())
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script is not None:
            with contextlib.suppress(ValueError):  # broken structured data is ignored, as search engines do
                self.data.json_ld.append(json.loads("".join(self._script)))
            self._script = None
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script.append(data)
        elif self._in_title and not self.data.title:
            self.data.title = data.strip()


def read_page(text: str) -> PageData:
    reader = _Reader()
    try:
        reader.feed(text)
        reader.close()
    except Exception:  # a malformed page still yields what was read before the error
        log.debug("html parse error")
    return reader.data


def _types(node: dict[str, Any]) -> set[str]:
    kind = node.get("@type")
    kinds = kind if isinstance(kind, list) else [kind]
    return {str(k).rsplit("/", 1)[-1].lower() for k in kinds if k}


def _nodes(value: Any) -> list[dict[str, Any]]:
    """Every JSON-LD object, nested ones included (``@graph``, ``publisher``, ``author`` …)."""
    out: list[dict[str, Any]] = []
    stack = [value]
    while stack and len(out) < 500:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(reversed(item))
        elif isinstance(item, dict):
            out.append(item)
            stack.extend(v for v in item.values() if isinstance(v, dict | list))
    return out


def _text(value: Any, limit: int) -> str:
    if isinstance(value, list):
        value = next((v for v in value if isinstance(v, str) and v.strip()), "")
    if isinstance(value, dict):
        value = value.get("@value") or value.get("name") or ""
    if not isinstance(value, str):
        return ""
    clean = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", value)).split())
    return clean[:limit].strip()


def _url(value: Any, base: str) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        value = value.get("url") or value.get("contentUrl") or value.get("@id") or ""
    if not isinstance(value, str) or not value.strip():
        return ""
    absolute = urllib.parse.urljoin(base, value.strip())
    return absolute if urllib.parse.urlsplit(absolute).scheme in ("http", "https") else ""


def social_profile(url: str) -> str:
    """``url`` normalised when it is a profile on a known social network, else ``""``."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return ""
    host = (parts.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    host = host[2:] if host.startswith(("m.", "de.", "fr.", "uk.")) else host
    if host not in SOCIAL_HOSTS:
        return ""
    path = parts.path.rstrip("/")
    if not path or any(s in (path + "?" + parts.query).lower() for s in SHARE_PATHS):
        return ""
    prefixes = SOCIAL_HOSTS[host]
    if prefixes and not any(path.lower().startswith(p.rstrip("/")) for p in prefixes):
        return ""
    www = "www." if host in ("linkedin.com", "facebook.com", "instagram.com", "youtube.com") else ""
    return f"https://{www}{host}{path}"


def organization(data: PageData, domain: str) -> dict[str, Any]:
    """The schema.org Organization the page describes: the one whose url is on ``domain`` if several."""
    orgs = [n for doc in data.json_ld for n in _nodes(doc) if _types(n) & ORG_TYPES or any(
        t.endswith(("organization", "business")) for t in _types(n))]  # fmt: skip
    if not orgs:
        return {}

    def rank(node: dict[str, Any]) -> tuple[int, int]:
        host = urllib.parse.urlsplit(_url(node.get("url"), f"https://{domain}/")).hostname or ""
        own = host.lower().removeprefix("www.") == domain
        return (1 if own else 0, sum(1 for k in ("name", "address", "telephone", "logo", "sameAs") if node.get(k)))

    return max(orgs, key=rank)


def extract(page: Page, domain: str) -> dict[str, str]:
    """Field → value read from the page (only what it states)."""
    data = read_page(page.html)
    org = organization(data, domain)
    meta = data.meta
    out: dict[str, str] = {}
    out["name"] = _text(org.get("name") or org.get("legalName"), 200) or _text(meta.get("og:site_name"), 200)
    out["description"] = (
        _text(org.get("description"), 1000)
        or _text(meta.get("og:description"), 1000)
        or _text(meta.get("description"), 1000)
    )
    contact = org.get("contactPoint")
    contact = contact[0] if isinstance(contact, list) and contact else contact
    phone = _text(org.get("telephone"), 50)
    if not phone and isinstance(contact, dict):
        phone = _text(contact.get("telephone"), 50)
    if not phone:
        tel = next((h for h in data.links if h.lower().startswith("tel:")), "")
        phone = _text(urllib.parse.unquote(tel[4:]), 50)
    out["phone"] = phone if re.search(r"\d{4}", re.sub(r"\D", "", phone) or "") else ""
    address = org.get("address")
    address = address[0] if isinstance(address, list) and address else address
    if isinstance(address, dict):
        out["street"] = _text(address.get("streetAddress"), 200)
        out["city"] = _text(address.get("addressLocality"), 100)
        out["zip"] = _text(address.get("postalCode"), 20)
        country = _text(address.get("addressCountry"), 100)
        out["country"] = country.upper() if re.fullmatch(r"[A-Za-z]{2}", country) else country
    logo = _url(org.get("logo"), page.url) or _url(meta.get("og:logo"), page.url)
    out["logo_url"] = logo[:500]
    same_as = org.get("sameAs")
    candidates = ([same_as] if isinstance(same_as, str) else list(same_as or [])) + [
        urllib.parse.urljoin(page.url, h) for h in data.links
    ]
    profiles = list(dict.fromkeys(p for c in candidates if isinstance(c, str) and (p := social_profile(c))))
    out["social_links"] = "\n".join(profiles[:8])
    return {k: v for k, v in out.items() if v}


class CompanyWebsiteAdapter:
    """Suggestions from the company's own home page (see the module docstring)."""

    key = "company_website"
    label = "Company website"
    sends: tuple[str, ...] = ("website_domain", "email_domain")  # only the domain leaves the deployment

    def __init__(self, transport: httpx.BaseTransport | None = None, resolver: Resolver = system_resolver) -> None:
        self.transport = transport
        self.resolver = resolver

    def lookup(self, query: dict[str, str]) -> list[EnrichmentResult]:
        domain = (query.get("website_domain") or query.get("email_domain") or "").strip().lower().rstrip(".")
        if not HOSTNAME.match(domain):
            return []
        try:
            page = fetch(f"https://{domain}/", transport=self.transport, resolver=self.resolver)
        except FetchRefused as exc:
            log.info("company website lookup skipped", domain=domain, reason=str(exc))
            return []
        values = extract(page, domain)
        entity = values.get("name") or domain
        return [EnrichmentResult(f, v, page.url, self.label, entity) for f, v in values.items()]


register_adapter(CompanyWebsiteAdapter())
