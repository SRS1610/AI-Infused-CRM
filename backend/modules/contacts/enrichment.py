"""Enrichment adapters (feature ``contacts.enrich``).

An adapter looks a contact up in an outside source (a company registry, a data vendor, the
company's own website) and returns *results*: one value per field, each with the source it came
from. Adapters never write anything; the ``contacts.enrich`` feature turns their results into field
suggestions that keep their source, and the user accepts them as a write proposal.

Administrators decide which adapters may be used: an adapter only runs when a
``contacts.enrichment_source`` record approves it (for one company, or for every company when the
record has no company; a company-specific record overrides the global one, so a company can also
switch an approved adapter off).

Writing a real adapter
----------------------

.. code-block:: python

    from modules.contacts.enrichment import EnrichmentResult, register_adapter

    class CompanyRegistryAdapter:
        key = "uk_companies_house"
        label = "Companies House (UK)"
        sends = ("name", "vat")            # what leaves the deployment: keep it minimal

        def lookup(self, query: dict[str, str]) -> list[EnrichmentResult]:
            resp = httpx.get("https://api.company-information.service.gov.uk/search/companies",
                             params={"q": query["name"]}, auth=(api_key(), ""), timeout=10)
            ...
            return [EnrichmentResult("street", hit["address"]["address_line_1"], url, self.label)]

    register_adapter(CompanyRegistryAdapter())

* ``lookup`` receives only the keys listed in ``sends`` and only values the requesting user can
  read; it must not raise for "not found" (return ``[]``) and should time out quickly.
* Every result carries ``source_url`` (a link a person can open to check the value) and a label.
* Keys must be stable: they are what administrators approve. Credentials belong in the
  environment / secret store, never in the approval record.
* Register the adapter at import time of your module (as :class:`FixtureAdapter` is below), then
  approve it under *Contacts → Configuration → Enrichment sources*.

Shipped adapters: ``fixture`` (offline, tests and demo) and ``company_website``
(:mod:`modules.contacts.website`: the company's public home page, no key needed).
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

FIELDS = (
    "name",
    "website",
    "phone",
    "street",
    "city",
    "zip",
    "country",
    "industry",
    "vat",
    "description",
    "social_links",
    "logo_url",
)
QUERY_KEYS = ("name", "email_domain", "website_domain", "vat", "city", "country")


@dataclass(frozen=True)
class EnrichmentResult:
    field: str
    value: str
    source_url: str
    source_label: str
    entity: str = ""  # the outside record the value belongs to (e.g. the registry entry's name)

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


class EnrichmentAdapter(Protocol):
    key: str
    label: str
    sends: tuple[str, ...]

    def lookup(self, query: dict[str, str]) -> list[EnrichmentResult]: ...


ADAPTERS: dict[str, EnrichmentAdapter] = {}


def register_adapter(adapter: EnrichmentAdapter) -> EnrichmentAdapter:
    ADAPTERS[adapter.key] = adapter
    return adapter


def _domain(value: str | None) -> str:
    v = (value or "").strip().lower()
    v = v.rsplit("@", 1)[1] if "@" in v else re.sub(r"^[a-z]+://", "", v).split("/", 1)[0]
    return v[4:] if v.startswith("www.") else v


def _norm_name(value: str | None) -> str:
    from modules.contacts.duplicates import name_tokens

    return " ".join(name_tokens(value))


class FixtureAdapter:
    """Offline adapter over ``data/enrichment_fixture.json`` (tests, demo, CI): no network access.

    It matches on business e-mail / website domain, tax id, or exact normalised company name, and
    returns *every* fixture entry that matches, so the feature still has to pick the right one."""

    key = "fixture"
    label = "Offline company registry (fixture)"
    sends = ("name", "email_domain", "website_domain", "vat")

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path(__file__).parent / "data" / "enrichment_fixture.json"
        self._entries: list[dict[str, Any]] | None = None

    @property
    def entries(self) -> list[dict[str, Any]]:
        if self._entries is None:
            self._entries = json.loads(self.path.read_text())["companies"]
        return self._entries

    def lookup(self, query: dict[str, str]) -> list[EnrichmentResult]:
        domains = {d for d in (query.get("email_domain"), query.get("website_domain")) if d}
        vat = re.sub(r"\W", "", (query.get("vat") or "").upper())
        name = _norm_name(query.get("name"))
        out: list[EnrichmentResult] = []
        for entry in self.entries:
            hit = (
                (domains & set(entry.get("domains", [])))
                or (vat and vat == re.sub(r"\W", "", entry["fields"].get("vat", "").upper()))
                or (name and name in {_norm_name(n) for n in entry.get("names", [])})
            )
            if not hit:
                continue
            for fname, value in entry["fields"].items():
                if fname in FIELDS and value:
                    out.append(EnrichmentResult(fname, str(value), entry["source_url"], self.label, entry["names"][0]))
        return out


register_adapter(FixtureAdapter())


def query_for(partner: Model, keys: tuple[str, ...]) -> dict[str, str]:
    """The lookup query, built from what the user can read, limited to what the adapter sends."""
    from modules.contacts.duplicates import PUBLIC_DOMAINS

    partner.check_access("read")
    email_dom = _domain(partner.email) if partner.email else ""
    full = {
        "name": partner.name or "",
        "email_domain": email_dom if email_dom not in PUBLIC_DOMAINS else "",
        "website_domain": _domain(partner.website) if partner.website else "",
        "vat": partner.vat or "",
        "city": partner.city or "",
        "country": partner.country_id.name if partner.country_id else "",
    }
    return {k: v for k, v in full.items() if k in keys and v}


def approved_sources(env: Environment) -> list[Model]:
    """One ``contacts.enrichment_source`` per adapter key: the current company's record if there is one,
    else the global one; only approved ones, only adapters that are registered."""
    sources = env["contacts.enrichment_source"].elevated().search([], order="id")
    company = env.company.ids[0] if env.company else None
    by_key: dict[str, dict[str, Model]] = {}
    for src in sources:
        if src.company_id and src.company_id.ids[0] != company:
            continue
        by_key.setdefault(src.adapter, {})["company" if src.company_id else "global"] = src
    chosen = [slots.get("company") or slots["global"] for slots in by_key.values()]
    return [s for s in chosen if s.approved and s.adapter in ADAPTERS]


def lookup(partner: Model) -> list[tuple[Model, list[EnrichmentResult]]]:
    """Run every approved adapter for ``partner`` (whose env is the requesting user's)."""
    partner.check_access("read")
    out = []
    for src in approved_sources(partner.env):
        adapter = ADAPTERS[src.adapter]
        query = query_for(partner, tuple(adapter.sends))
        out.append((src, adapter.lookup(query) if query else []))
    return out
