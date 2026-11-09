# Tessera

An open, modular, multi-company business suite with a CRM at its core that grows into a full ERP, with AI built into every module — proposing, never committing, and always bound by the user's permissions.

> **Status:** planning. Architecture, module map, core ERD, AI architecture and the phased build plan are in [`docs/architecture/`](docs/architecture/) and are awaiting approval before Phase 1 code is written. Nothing here runs yet.

## Documents

| Document | What it covers |
|---|---|
| [Architecture](docs/architecture/ARCHITECTURE.md) | Stack, kernel (metadata ORM, modules, security, multi-company/currency/language), platform services, web client, tenancy, performance, testing |
| [Module dependency map](docs/architecture/MODULE_DEPENDENCY_MAP.md) | Every module by phase and layer, dependency graphs, bridge-module convention, install order |
| [Core ERD](docs/architecture/ERD_CORE.md) | Entities and relationships for the kernel, platform modules, Phase 1 apps and the AI platform |
| [AI architecture note](docs/architecture/AI_ARCHITECTURE.md) | Gateway, provider adapters, routing, tools under user permissions, retrieval with record-rule filtering, proposals, guardrails, audit, evaluation, compliance |
| [Build plan](docs/architecture/BUILD_PLAN.md) | Phases 1–8 with scope, AI features and acceptance criteria |
| [DECISIONS.md](DECISIONS.md) | Non-blocking decisions taken with their rationale |
| [CHANGELOG.md](CHANGELOG.md) | Per-phase change log |

## Planned stack

Python 3.12 · FastAPI · PostgreSQL 16 + pgvector · Redis · Celery · React + TypeScript · Docker Compose · GitHub Actions. See the architecture document for the reasoning behind the custom metadata ORM.

## Licence

Apache-2.0 (proposed, see DECISIONS.md D-018).
