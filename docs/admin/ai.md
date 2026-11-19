# Administering AI

Menu: **Settings → AI**. Everything here is restricted to administrators.

## Providers

Add one row per provider account: kind (Anthropic, OpenAI, Azure OpenAI, Google Gemini, self-hosted
OpenAI-compatible such as vLLM or Ollama, Voyage for embeddings), API key (stored encrypted; leave
empty to use the gateway's environment key), endpoint, **region** and whether it is **approved for
personal data**.

Useful options (`options`, JSON):

| Key | Providers | Meaning |
|---|---|---|
| `inference_geo` | Anthropic | where inference runs (`"us"`); reported back per call |
| `deployments` | Azure OpenAI | model name → deployment name |
| `api_version` | Azure OpenAI | API version |
| `json_mode` | self-hosted | `schema`, `object` or `none`, depending on the model server |
| `supports_tools` | self-hosted | `false` for models without tool calling |
| `pricing` | any | `{"model": {"input": 0.5, "output": 1.5}}` per million tokens, for negotiated or self-hosted rates |
| `timeout`, `max_retries` | any | per-request timeout (s) and SDK retries |

### From the command line

```bash
GATEWAY_ANTHROPIC_API_KEY=... tessera ai provider --kind anthropic --region us
```

creates (or updates) an *Anthropic* provider without storing a key — the gateway reads
`GATEWAY_<KIND>_API_KEY` from its environment, in-process mode included — and points the default
routes of every task class it serves at the default model tiers: Haiku for classification, extraction,
translation and search, Sonnet for summaries and drafts, Opus for reasoning and vision (D-016).
Company-specific routes and the embedding route are left alone.

## Live evaluation

`tessera ai eval run --mode live --save` runs every feature's evaluation set against the configured
providers and stores accuracy, latency p95 and cost per feature (shown under **Usage and cost**). The
nightly workflow (`.github/workflows/nightly.yml`) does this when the repository secret
`ANTHROPIC_API_KEY` is set. Evaluations run in a rolled-back sandbox on their own corpus, so any
database gives the same scores and nothing is written.

## Routing

One route per task class (and optionally per company): a primary provider and model, and a fallback.
Three consecutive outages of the primary open its circuit for 30 seconds; calls go to the fallback and
are flagged in the ledger. Residency: in **Company settings**, list the regions a company accepts;
providers outside them are never used for that company.

## Budgets and quotas

- A **company budget** (spend, tokens or requests per day or month) refuses calls once used up
  (`budget_exceeded`); users see the manual path.
- A **user quota** (set *User quota*) limits one person without blocking colleagues.
- A **feature cap** (set *Feature*) limits one feature.
- *Soft* budgets never refuse; they flag responses once the alert percentage is reached.

## Kill switches

**Usage and cost → Features and kill switches**: switch a feature off everywhere or for one company.
The change applies within seconds; the feature's buttons disappear and its API answers
`404 feature_disabled` without calling any provider. **Company settings → AI enabled** switches off all AI
for a company; the global row (no company) switches off AI entirely.

## Usage and cost

The dashboard shows, per company and feature: requests, tokens, spend, latency p50/p95, how often
people accepted the AI's proposals, fallback rate and cache hit rate. Figures come from the gateway
ledger (`ai_gateway.ledger`) and the run audit.

## Audit

**Runs (audit)** lists every AI invocation with its prompt version, provider, model, cost, outcome and
the decision taken on its proposals; **Proposals** lists every proposed change. Prompt inputs and
outputs are purged after the company's retention period (default 90 days).

## Enrichment sources

**Contacts → Configuration → Enrichment sources** decides which outside sources *Find missing details* may
query; a source is used only when a row approves it, for one company or for all (a company's own row wins,
so a company can also switch a source off).

| Adapter key | Source | What leaves the deployment |
|---|---|---|
| `fixture` | Offline registry shipped for the demo and tests | nothing |
| `company_website` | The company's public home page (`https://<website or e-mail domain>/`), read for schema.org Organization data, OpenGraph and meta tags: name, description, phone, address, social profiles, logo URL | the domain, in an HTTPS request to the company's own site |

The website source needs no key. It only fetches `https://` addresses on port 443 whose host name resolves
to public addresses only (never private, loopback, link-local or reserved ranges; IP literals are refused),
connects to the address it checked, follows at most three redirects (each checked again), reads at most
1 MB of HTML and gives up after 8 seconds. Each suggestion links to the page it was read on.

## Running the gateway

The gateway is its own service (`gateway serve`, port 8100) sharing the application database
(`GATEWAY_DATABASE_URL`) and master key (`GATEWAY_MASTER_KEY`) so it can read the configuration above
and decrypt keys. The API reaches it at `TESSERA_AI_GATEWAY_URL` with the shared
`TESSERA_AI_GATEWAY_TOKEN` / `GATEWAY_SERVICE_TOKEN`. Metrics are exposed at `/metrics` (Prometheus).
For a single-process installation set `TESSERA_AI_GATEWAY_URL=inprocess` (requires the `tessera-gateway`
package in the API's environment).
