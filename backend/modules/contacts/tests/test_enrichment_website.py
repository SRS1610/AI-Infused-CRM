"""The "Company website" enrichment adapter: structured data read from a home page, the SSRF guards of the
fetch, and the adapter behind an approval in the ``contacts.enrich`` feature. HTTP is a mock transport and
DNS a dictionary: no network access."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from gateway.adapters.mock import MockAdapter

from modules.contacts import ai_features as A
from modules.contacts import enrichment as E
from modules.contacts.website import (
    MAX_REDIRECTS,
    CompanyWebsiteAdapter,
    FetchRefused,
    Page,
    extract,
    fetch,
    is_public,
    social_profile,
)
from tessera.ai import client as C
from tessera.ai import proposals as P
from tessera.ai.runtime import HANDLERS, run_feature

PUBLIC = "93.184.216.34"
DNS = {
    "acme.example": [PUBLIC],
    "www.acme.example": ["93.184.216.35"],
    "v6.example": ["2606:2800:220:1:248:1893:25c8:1946"],
    "intranet.acme.example": ["10.1.2.3"],
    "metadata.example": ["169.254.169.254"],
    "loop.example": ["127.0.0.1"],
    "loop6.example": ["::1"],
    "mapped.example": ["::ffff:127.0.0.1"],
    "cgnat.example": ["100.64.0.7"],
    "mixed.example": [PUBLIC, "192.168.1.10"],
}

HOME = """<!doctype html><html><head>
<title>Acme Robotics — collaborative automation</title>
<meta property="og:site_name" content="Acme (OpenGraph name)">
<meta property="og:description" content="OpenGraph description">
<meta name="description" content="Meta description">
<script type="application/ld+json">{"@context": "https://schema.org", "@graph": [
  {"@type": "WebSite", "url": "https://acme.example/", "name": "Acme website"},
  {"@type": "Organization", "@id": "https://partner.example/#org", "name": "Partner Org", "url": "https://partner.example/"},
  {"@type": ["Organization", "Corporation"], "name": "Acme Robotics &amp; Co", "url": "https://acme.example/",
   "description": "<p>Collaborative robots for food &amp; packaging lines.</p>",
   "telephone": "+33 1 23 45 67 89",
   "address": {"@type": "PostalAddress", "streetAddress": "12 rue des Lilas", "addressLocality": "Lyon",
               "postalCode": "69002", "addressCountry": "fr"},
   "logo": {"@type": "ImageObject", "url": "/static/logo.svg"},
   "sameAs": ["https://www.linkedin.com/company/acme-robotics/", "https://x.com/acmerobotics",
              "https://example.org/not-social"]}
]}</script>
<script type="application/ld+json">{ this is not json </script>
</head><body>
<a href="https://www.facebook.com/sharer/sharer.php?u=https://acme.example">Share</a>
<a href="https://twitter.com/intent/tweet?text=hi">Tweet</a>
<a href="https://www.youtube.com/@acmerobotics">YouTube</a>
<a href="https://x.com/acmerobotics/">X again</a>
</body></html>"""

META_ONLY = """<html><head><meta property="og:site_name" content="Globex">
<meta property="og:description" content="Industrial AGVs &amp; fleet software."></head>
<body><a href="tel:+1%20206%20555%200144">Call us</a> <a href="https://www.instagram.com/globex/">Instagram</a>
<a href="https://www.linkedin.com/feed/">feed</a></body></html>"""


def resolver(host: str, port: int) -> list[str]:
    assert port == 443
    if host not in DNS:
        raise OSError("no such host")
    return DNS[host]


def transport(pages: dict[str, httpx.Response | Callable[[httpx.Request], httpx.Response]], seen: list[Any]) -> Any:
    """Routes by the Host header (the URL carries the checked IP address)."""

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        key = f"{request.headers['host']}{request.url.raw_path.decode()}"
        page = pages.get(key)
        if page is None:
            return httpx.Response(404)
        return page(request) if callable(page) else page

    return httpx.MockTransport(handle)


def html_page(body: str, **headers: str) -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8", **headers}, text=body)


def test_reads_the_organization_from_json_ld_with_sources() -> None:
    seen: list[Any] = []
    adapter = CompanyWebsiteAdapter(transport({"acme.example/": html_page(HOME)}, seen), resolver)
    results = adapter.lookup({"website_domain": "acme.example", "email_domain": "other.example"})
    values = {r.field: r.value for r in results}
    assert values == {
        "name": "Acme Robotics & Co",
        "description": "Collaborative robots for food & packaging lines.",
        "phone": "+33 1 23 45 67 89",
        "street": "12 rue des Lilas",
        "city": "Lyon",
        "zip": "69002",
        "country": "FR",
        "logo_url": "https://acme.example/static/logo.svg",
        "social_links": "https://www.linkedin.com/company/acme-robotics\nhttps://x.com/acmerobotics\n"
        "https://www.youtube.com/@acmerobotics",
    }
    assert {(r.source_url, r.source_label, r.entity) for r in results} == {
        ("https://acme.example/", "Company website", "Acme Robotics & Co")
    }
    (request,) = seen  # one request, to the checked address, for the right site
    assert request.url.host == PUBLIC and request.url.scheme == "https"
    assert request.headers["host"] == "acme.example" and request.extensions["sni_hostname"] == "acme.example"
    assert "Tessera" in request.headers["user-agent"]


def test_falls_back_to_opengraph_and_page_links() -> None:
    page = Page("https://globex.example/", META_ONLY)
    assert extract(page, "globex.example") == {
        "name": "Globex",
        "description": "Industrial AGVs & fleet software.",
        "phone": "+1 206 555 0144",
        "social_links": "https://www.instagram.com/globex",
    }
    assert extract(Page("https://empty.example/", "<html><p>Hello</p></html>"), "empty.example") == {}
    assert social_profile("https://www.linkedin.com/in/someone/") == "https://www.linkedin.com/in/someone"
    assert social_profile("https://m.facebook.com/acme") == "https://www.facebook.com/acme"
    assert social_profile("javascript:alert(1)") == social_profile("https://linkedin.com/") == ""


def test_uses_the_email_domain_when_there_is_no_website() -> None:
    seen: list[Any] = []
    adapter = CompanyWebsiteAdapter(transport({"acme.example/": html_page(HOME)}, seen), resolver)
    assert {r.field for r in adapter.lookup({"email_domain": "acme.example"})} >= {"name", "phone"}
    assert adapter.lookup({}) == [] and adapter.lookup({"website_domain": "not a domain"}) == []
    assert not [r for r in seen if r.headers["host"] != "acme.example"]


@pytest.mark.parametrize(
    "url",
    [
        "http://acme.example/",  # not https
        "https://acme.example:8443/",  # not the https port
        "https://127.0.0.1/",  # IP literal
        "https://[::1]/",
        "https://intranet.acme.example/",
        "https://metadata.example/latest/meta-data/",
        "https://loop.example/",
        "https://loop6.example/",
        "https://mapped.example/",
        "https://cgnat.example/",
        "https://mixed.example/",  # one private answer is enough to refuse
        "https://unknown.example/",
        "https://localhost/",
        "ftp://acme.example/",
    ],
)
def test_refuses_non_public_targets_without_connecting(url: str) -> None:
    seen: list[Any] = []
    with pytest.raises(FetchRefused):
        fetch(url, transport=transport({}, seen), resolver=resolver)
    assert seen == []


def test_public_address_check() -> None:
    assert is_public(PUBLIC) and is_public("2606:2800:220:1:248:1893:25c8:1946")
    for address in ("10.0.0.1", "172.16.3.4", "192.168.0.1", "127.0.0.1", "169.254.169.254", "0.0.0.0", "::1",
                    "fe80::1", "fc00::1", "::ffff:10.0.0.1", "224.0.0.1", "100.64.0.1", "198.18.0.1", "240.0.0.1"):  # fmt: skip
        assert not is_public(address), address


def test_redirects_are_checked_at_every_hop() -> None:
    seen: list[Any] = []
    pages: dict[str, Any] = {
        "acme.example/": httpx.Response(301, headers={"location": "https://www.acme.example/en/"}),
        "www.acme.example/en/": html_page(HOME),
    }
    page = fetch("https://acme.example/", transport=transport(pages, seen), resolver=resolver)
    assert page.url == "https://www.acme.example/en/" and [r.url.host for r in seen] == [PUBLIC, "93.184.216.35"]
    for location in ("https://intranet.acme.example/admin", "http://www.acme.example/", "https://169.254.169.254/"):
        seen.clear()
        pages["acme.example/"] = httpx.Response(302, headers={"location": location})
        with pytest.raises(FetchRefused):
            fetch("https://acme.example/", transport=transport(pages, seen), resolver=resolver)
        assert len(seen) == 1  # the unsafe hop is never requested
    pages["acme.example/"] = httpx.Response(302, headers={"location": "/"})  # a loop
    seen.clear()
    with pytest.raises(FetchRefused, match="Too many redirects"):
        fetch("https://acme.example/", transport=transport(pages, seen), resolver=resolver)
    assert len(seen) == MAX_REDIRECTS + 1


def test_type_size_status_and_errors() -> None:
    seen: list[Any] = []
    pages: dict[str, Any] = {
        "acme.example/": httpx.Response(200, headers={"content-type": "application/json"}, text="{}"),
    }
    with pytest.raises(FetchRefused, match="not an HTML page"):
        fetch("https://acme.example/", transport=transport(pages, seen), resolver=resolver)
    pages["acme.example/"] = httpx.Response(500)
    with pytest.raises(FetchRefused, match="HTTP 500"):
        fetch("https://acme.example/", transport=transport(pages, seen), resolver=resolver)
    big = HOME.replace("</head>", "</head>" + "<p>filler</p>" * 50_000)
    pages["acme.example/"] = html_page(big)
    page = fetch("https://acme.example/", transport=transport(pages, seen), resolver=resolver, max_bytes=20_000)
    assert len(page.html) <= 20_000 and extract(page, "acme.example")["name"] == "Acme Robotics & Co"
    pages["acme.example/"] = html_page(HOME.encode("latin-1", "replace").decode("latin-1"), **{
        "content-type": "text/html; charset=x-unknown"})  # fmt: skip
    assert "Acme" in fetch("https://acme.example/", transport=transport(pages, seen), resolver=resolver).html

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    pages["acme.example/"] = timeout
    adapter = CompanyWebsiteAdapter(transport(pages, seen), resolver)
    assert adapter.lookup({"website_domain": "acme.example"}) == []  # an unreachable site is "no result"
    with pytest.raises(FetchRefused, match="took too long"):
        fetch("https://acme.example/", transport=transport(pages, seen), resolver=resolver, timeout=0)


# -- in the contacts.enrich feature --------------------------------------------------------------------------


@pytest.fixture
def _fresh_ai() -> Any:
    MockAdapter.reset()
    C.reset_inprocess_state()
    yield
    C.set_client_factory(None)


@pytest.mark.db
def test_website_adapter_waits_for_approval_then_feeds_suggestions(
    env: Any, env_for: Any, monkeypatch: pytest.MonkeyPatch, _fresh_ai: Any
) -> None:
    seen: list[Any] = []
    monkeypatch.setitem(
        E.ADAPTERS,
        "company_website",
        CompanyWebsiteAdapter(transport({"acme.example/": html_page(HOME)}, seen), resolver),
    )
    demo = env_for("demo")
    partner = (
        env["contacts.partner"]
        .elevated()
        .create({"name": "Acme Robotics", "is_company": True, "website": "https://www.acme.example/robots"})
    )
    kwargs = HANDLERS[A.ENRICH_FEATURE](demo, partner.with_env(demo), {})
    assert len(kwargs["sources"]) == 2 and seen == []  # the contact and the fixture registry: website not called
    src = env["contacts.enrichment_source"].create(
        {"name": "Company website", "adapter": "company_website", "approved": True,
         "company_id": env.ref("core.main_company").ids[0]}
    )  # fmt: skip
    sid = f"contacts.enrichment_source:{src.ids[0]}"
    answer = {
        "suggestions": [
            {"field": f, "value": v, "source": sid, "evidence": "home page"}
            for f, v in {
                "description": "Collaborative robots for food & packaging lines.",
                "phone": "+33 1 23 45 67 89",
                "city": "Lyon",
                "country": "FR",
                "logo_url": "https://acme.example/static/logo.svg",
                "social_links": "https://www.linkedin.com/company/acme-robotics\nhttps://x.com/acmerobotics\n"
                "https://www.youtube.com/@acmerobotics",
                "street": "1 Invented Avenue",  # not on the page: dropped
            }.items()
        ],
        "citations": [sid],
    }
    p = env.ref("ai_core.provider_local")
    p.write({"options": {**(p.options or {}), "script": [{"match": {"feature": A.ENRICH_FEATURE},
                                                          "respond": {"json": answer}}]}})  # fmt: skip
    kwargs = HANDLERS[A.ENRICH_FEATURE](demo, partner.with_env(demo), {})
    website = next(s for s in kwargs["sources"] if s.id == sid)
    assert "Acme Robotics & Co | logo_url: https://acme.example/static/logo.svg | https://acme.example/" in website.text
    assert len(seen) == 1 and seen[0].headers["host"] == "acme.example"  # only the domain left the deployment
    result = run_feature(demo, A.ENRICH_FEATURE, {}, record=partner.with_env(demo), **kwargs)
    kept = {s["field"]: s for s in result.data["suggestions"]}
    assert set(kept) == {"description", "phone", "city", "country", "logo_url", "social_links"}
    assert {s["source_url"] for s in kept.values()} == {"https://acme.example/"}
    assert {d["field"]: d["why"] for d in result.data["dropped"]} == {"street": "value not found in the cited source"}
    (proposal,) = result.proposals
    assert proposal.payload["country_id"] == env.ref("core.country_fr").ids[0]
    P.accept(demo, proposal)
    assert partner.logo_url == "https://acme.example/static/logo.svg" and partner.city == "Lyon"
    assert partner.social_links.splitlines()[0] == "https://www.linkedin.com/company/acme-robotics"
    assert "packaging lines" in partner.description
    # the country may also come as a name; the comparison of links ignores scheme, www and trailing slashes
    assert A._convert(env, "country", "France") == env.ref("core.country_fr").ids[0]
    assert A.same_value(
        "social_links",
        "http://x.com/acmerobotics/\nlinkedin.com/company/a",
        "https://linkedin.com/company/a x.com/acmerobotics",
    )
    assert A.same_value("logo_url", "https://acme.example/logo.svg", "http://www.acme.example/logo.svg/")
    # a company can switch the adapter off again
    env["contacts.enrichment_source"].browse(src.ids).write({"approved": False})
    assert "company_website" not in {s.adapter for s in E.approved_sources(demo)}
