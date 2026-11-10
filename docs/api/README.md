# Tessera HTTP API

Every installed model is reachable over REST and JSON-RPC with **exactly the caller's permissions**
(access rights, record rules, field groups, multi-company). Interactive reference: `/api/docs`
(OpenAPI JSON at `/api/openapi.json`).

## Authentication

| Method | How | CSRF |
|---|---|---|
| Session (browser) | `POST /api/v1/auth/login {"login","password"}` sets an HttpOnly cookie and returns `csrf_token` | unsafe methods must send `X-CSRF-Token` |
| Two-factor | if the login answer is `{"totp_required": true, "pre_auth_token": …}`, `POST /api/v1/auth/totp {"pre_auth_token","code"}` — `code` may be a TOTP code or a one-time recovery code | — |
| API key | `Authorization: Bearer tsk_…` (create with `POST /api/v1/auth/api-keys`; the key is shown once) | not needed |
| OIDC SSO | browser goes to `/api/v1/auth/oidc/<provider>/start` (authorization code + PKCE) | — |

Five consecutive failed passwords lock an account for 15 minutes (`TESSERA_LOGIN_LOCKOUT_*`);
login endpoints are rate-limited per IP, the API per key/IP. Other auth endpoints: `GET /auth/session`,
`POST /auth/logout`, `POST /auth/password`, `POST /auth/totp/setup|enable|disable`,
`GET|POST|DELETE /auth/api-keys`, `GET /auth/providers`.

Optional headers: `X-Tessera-Companies: 1,2` (selected companies, must be allowed for the user),
`X-Tessera-Company: 1` (current company), `X-Tessera-Lang: fr_FR`, `X-Tessera-Tz: Europe/Paris`.

## REST — `/api/v1/models/{model}`

| Call | Notes |
|---|---|
| `GET /models/crm.lead?domain=[…]&fields=name,stage_id&order=priority desc&limit=80&offset=0` | `domain` is the JSON domain DSL (`[["a","=",1]]`, `{"any":[…]}`); returns `items`, `total` |
| `GET /models/crm.lead?after=1200&limit=500` | keyset pagination (order by id); response carries `next_cursor` |
| `GET /models/crm.lead?expand=partner_id` | many2one values replaced by the full readable target record |
| `GET /models/crm.lead/42?fields=…` | `ETag` header |
| `POST /models/crm.lead {"values": {…} \| [{…}], "fields": […]}` | single or batch create; x2many commands as JSON arrays: `["set",[1,2]]`, `["add",3]`, `["create",{…}]`, `["update",id,{…}]`, `["remove",id]`, `["delete",id]`, `["clear"]` |
| `PATCH /models/crm.lead/42 {"values": {…}}` | send `If-Match: <ETag>` for optimistic concurrency (409 on conflict) |
| `DELETE /models/crm.lead/42` | soft-delete models go to the trash |
| `POST /models/crm.lead/read_group {"domain","aggregates":["expected_revenue:sum"],"groupby":["stage_id","create_date:month"]}` | |
| `GET /models/crm.lead/fields`, `GET /models/crm.lead/name_search?q=acme` | |
| `POST /models/crm.lead/42/call/action_set_won {"args":[],"kwargs":{}}` | only methods decorated `@api.expose`; never `_private` ones |

Errors are JSON: `{"error": "AccessError", "message": "…", "request_id": "…"}` with 400/401/403/404/409/422/429/503.

## JSON-RPC 2.0 — `POST /api/v1/jsonrpc`

Methods: `search_read`, `search`, `search_count`, `read`, `create`, `write`, `unlink`, `read_group`,
`fields_get`, `name_search`, `default_get`, `call`, `ref`. `params.model` names the model. Batches
(arrays) are supported; each call runs in its own savepoint, so one failing call does not undo the others.

## Webhooks

Create a `core.webhook` record (model, events `create|write|unlink`, optional domain and field list,
URL, secret, run-as user). Deliveries are sent after commit with `X-Tessera-Event`,
`X-Tessera-Delivery` and `X-Tessera-Signature: sha256=<HMAC-SHA256(secret, body)>`. Failed deliveries
retry with exponential backoff (2^attempts minutes, capped at 2 h, 8 attempts); every attempt is in the
delivery log.

## Realtime — `GET /api/v1/ws` (WebSocket)

Authenticated by the session cookie (or `?api_key=`). Send `{"op":"subscribe","channels":[…]}`;
channels are authorised against the caller's permissions:
`user:<uid>` (own only), `record:<model>:<id>` (must be readable), `model:<model>` (read right).
Events: `{"channel","event":"create|write|unlink","data":{"id","fields","uid"}}`.

## Client metadata (`web` module)

`GET /api/v1/web/menus`, `GET /api/v1/web/actions/{xmlid|id}`,
`GET /api/v1/web/views?model=crm.lead&types=form,list,search`, `GET /api/v1/web/filters?model=…`.
Views are returned fully composed (inheritance applied) with fields the caller cannot read removed.
