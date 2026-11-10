# Tessera

An open, modular, multi-company business suite with a CRM at its core that grows into a full ERP, with AI built into every module — proposing, never committing, and always bound by the user's permissions.

> **Status:** Phase 1 (foundation, CRM and AI) is complete: kernel, web client, platform services,
> Contacts, Discuss, Calendar, CRM, Products, Quotations, the AI platform with 21 AI features, and Studio,
> in English and French. Every Phase 1 acceptance criterion is met except the live AI evaluation run,
> which needs a provider key. Details, scores and open items:
> [docs/phase-1-summary.md](docs/phase-1-summary.md). Security review:
> [docs/security/asvs-phase-1.md](docs/security/asvs-phase-1.md).

## Try it

With Docker:

```bash
make setup
```

```bash
make up
```

```bash
docker compose -f deploy/compose/docker-compose.yml exec api tessera seed demo --phase 1
```

Open http://localhost:5173 and sign in as `admin` / `admin1234`. Without Docker, or for the guided
tour of every feature, follow [docs/demos/phase-1.md](docs/demos/phase-1.md). No AI provider key is
needed for the demo: a deterministic test provider answers until you configure one under
**Settings → AI**.

## Repository

| Path | What it holds |
|---|---|
| `backend/` | Python 3.12 API, metadata ORM, module system, CLI (`tessera`), and the modules in `backend/modules/` |
| `gateway/` | AI gateway service: provider adapters, routing, budgets, redaction, ledger |
| `frontend/` | React + TypeScript web client, unit tests and Playwright end-to-end tests |
| `deploy/` | Docker Compose stack and container images |
| `docs/` | Architecture, user, administrator, developer and AI guides, demo script |

Common tasks: `make test`, `make e2e`, `make lint`; `make help` lists the rest.

## Documents

| Document | What it covers |
|---|---|
| [Architecture](docs/architecture/ARCHITECTURE.md) | Stack, kernel (metadata ORM, modules, security, multi-company/currency/language), platform services, web client, tenancy, performance, testing |
| [Module dependency map](docs/architecture/MODULE_DEPENDENCY_MAP.md) | Every module by phase and layer, dependency graphs, bridge-module convention, install order |
| [Core ERD](docs/architecture/ERD_CORE.md) | Entities and relationships for the kernel, platform modules, Phase 1 apps and the AI platform |
| [AI architecture note](docs/architecture/AI_ARCHITECTURE.md) | Gateway, provider adapters, routing, tools under user permissions, retrieval with record-rule filtering, proposals, guardrails, audit, evaluation, compliance |
| [Build plan](docs/architecture/BUILD_PLAN.md) | Phases 1–8 with scope, AI features and acceptance criteria |
| [Phase 1 summary](docs/phase-1-summary.md) | What works, what is stubbed, known gaps, AI feature scores |
| [Phase 1 demo](docs/demos/phase-1.md) | Walkthrough of every Phase 1 feature on the demo data |
| [User guides](docs/user/) · [Administration](docs/admin/) · [Developers](docs/dev/) · [AI](docs/ai/) · [API](docs/api/README.md) | How to use, run and extend each part |
| [DECISIONS.md](DECISIONS.md) | Non-blocking decisions taken with their rationale |
| [CHANGELOG.md](CHANGELOG.md) | Per-phase change log |

## Stack

Python 3.12 · FastAPI · PostgreSQL 16 + pgvector · Redis · Celery · React + TypeScript · Docker Compose · GitHub Actions. See the architecture document for the reasoning behind the custom metadata ORM.

## Licence

Apache-2.0 (proposed, see DECISIONS.md D-018).
