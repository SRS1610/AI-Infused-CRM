# SAML single sign-on — administration

The `auth_saml` module lets people sign in through a SAML 2.0 identity provider (IdP) such as Microsoft
Entra ID, Okta, Keycloak, Google Workspace, ADFS or Shibboleth. It ships in Phase 2 (D-013, D-069) and
complements OpenID Connect (**Settings → Users & Companies → Identity providers**), which remains the
simpler choice when the IdP offers both.

Install it with `uv run tessera module install auth_saml` (from `backend/`). It depends on `core` and `web`.

## How a login works

Tessera is the service provider (SP). Only **SP-initiated** login is supported:

1. On the login page the person clicks the provider's button (`GET /api/v1/auth/saml/<id>/login`).
2. Tessera records a pending request (random ID, ten-minute lifetime), sets a short-lived HttpOnly cookie that
   binds it to this browser, and redirects to the IdP with a deflated `AuthnRequest` (HTTP-Redirect binding)
   and an opaque `RelayState`.
3. The IdP authenticates the person and posts a `SAMLResponse` to the assertion consumer service
   (`POST /api/v1/auth/saml/<id>/acs`, HTTP-POST binding).
4. Tessera validates the response (see [Security](#security)), maps it to an account and opens a session
   exactly like a password login (same cookie, CSRF token and session lifetime). The browser lands on `/`.

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/auth/saml/providers` | Public list (id and name only) used by the login page |
| `GET /api/v1/auth/saml/<id>/login` | Starts a login |
| `POST /api/v1/auth/saml/<id>/acs` | Assertion consumer service (ACS) |
| `GET /api/v1/auth/saml/<id>/metadata` | SP metadata to give to the IdP |

URLs are built from `TESSERA_PUBLIC_URL`; set it to the address people use (behind the reverse proxy).

## Setting up a provider

1. **Settings → Users & Companies → SAML providers → New.** Give it a name (the button label).
2. Save once: the form then shows this application's values on the **This application** tab:
   - *SP entity ID* — defaults to the metadata URL, e.g. `https://erp.example.com/api/v1/auth/saml/3/metadata`;
   - *Assertion consumer service URL* — `https://erp.example.com/api/v1/auth/saml/3/acs`;
   - *Metadata URL* — most IdPs can import it directly.
3. At the IdP, create a SAML application with those values (Entra: *Identifier (Entity ID)* and *Reply URL*;
   Okta: *Audience URI* and *Single sign on URL*; Keycloak: a SAML client whose *Client ID* is the SP entity
   ID). Ask the IdP to **sign assertions** with SHA-256 and **not to encrypt** them.
4. Copy from the IdP's metadata into the **Identity provider** tab:
   - *IdP entity ID* — the IdP's `entityID` (the `Issuer` of its responses);
   - *IdP single sign-on URL* — the `SingleSignOnService` location for the **HTTP-Redirect** binding;
   - *IdP signing certificates* — the signing certificate in PEM (a bare base64 body from the metadata is
     accepted too). During a certificate rotation paste the new certificate next to the old one; remove the
     old one once the IdP has switched.
5. Choose the account policy on the **Accounts** tab (below), then activate the provider. Inactive providers
   are not offered on the login page and refuse logins; their metadata stays available to administrators.

### Attribute mapping

| Setting | Default | Meaning |
|---|---|---|
| NameID format | Unspecified | Requested in the `AuthnRequest`. *Persistent* or *E-mail address* are recommended; *transient* NameIDs are refused because they change at every login |
| E-mail attribute | empty | Attribute holding the e-mail address. Empty: the NameID itself is the e-mail address. Examples: `email`, `mail`, `http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress`. An attribute's `FriendlyName` works too |
| Display name attribute | `displayName` | Used as the name of accounts created on first login |

The **subject** of a person is the NameID value. It identifies them for this provider from then on.

### Linking accounts

When a response is valid, the account is found in this order:

1. **An existing link** (*SAML account links*: provider + NameID → user). Administrators create links by
   hand under **Settings → Users & Companies → SAML account links** (or on the provider's *Accounts* tab)
   for people who already have an account. This is the safe default.
2. **By e-mail**, only when *Link accounts by e-mail* (`trust_email`) is on: the first login is linked to the
   account whose login equals the asserted e-mail address. Turn it on only for an IdP that guarantees the
   address belongs to the person (your own directory, where users cannot edit their address). With it off,
   a person whose e-mail matches an existing account is refused with a message asking an administrator to
   link the account — otherwise anyone able to choose their address at the IdP could take over that account
   (the same rule as OIDC's `email_verified`, D-067).
3. **New account**, only when *Create accounts on first login* (`allow_signup`) is on (off by default): the
   account gets the asserted e-mail as login, the display name attribute as name and **only the Internal user
   group**; an administrator grants application rights afterwards.

A linked account that is archived is refused. When the provider has a **company**, only accounts allowed in
that company can sign in through it and new accounts are created in it; administrators who do not belong to
that company do not see the provider (standard multi-company rule).

### Signing login requests

Most IdPs do not require signed requests. If yours does, open the provider and click **Generate signing
key** (an RSA-3072 key and a ten-year self-signed certificate), or paste your own PEM key and certificate and
tick *Sign login requests*. The key is stored encrypted with `TESSERA_MASTER_KEY` and visible to
administrators only; the certificate is published in the SP metadata (`AuthnRequestsSigned="true"`). Give the
IdP the new metadata after changing the key. Requests are signed with RSA-SHA256 (or ECDSA-SHA256 for an EC
key) in the query string, as the HTTP-Redirect binding prescribes.

### Second factor

As with OIDC, the IdP is trusted for the strength of the authentication: a SAML login does not ask for the
person's Tessera authenticator code even when they enabled one for password logins. Enforce multi-factor
authentication at the IdP.

## Security

A response is accepted only if all of the following hold; otherwise the login is refused with a short
message and a `saml login refused` warning (provider id, reason code, client address — never the NameID) is
logged. Successful logins log `saml login succeeded` with the user id and assertion ID, and update the user's
*Last login*. Provider and link changes are recorded in the audit log.

- **Parsing.** The XML is parsed with no DTD loading, no entity expansion, no network access and no huge
  trees; any document type declaration or entity is refused. Responses larger than 512 KB are refused.
- **Signatures.** Verified with [signxml](https://github.com/XML-Security/signxml) against the configured
  IdP certificates **only** — a certificate inside the message is never trusted. SHA-1, HMAC and DSA are
  refused. By default the **assertion** must be signed; *Require signed assertions* can be turned off for
  IdPs that sign only the response, which must then wrap exactly the assertion used.
- **Signature wrapping.** A response must contain exactly one assertion anywhere in the document, as a direct
  child of the response; the signature must cover the element it is in; and only the canonical element the
  verifier returns as signed is read (comments and unsigned siblings are ignored).
- **Protocol checks.** `Status` is Success; `Issuer` equals the IdP entity ID (assertion and, when present,
  response); every `AudienceRestriction` names the SP entity ID; the response `Destination` (required when the
  response is signed) and the bearer `Recipient` equal the ACS URL; `NotBefore`, `NotOnOrAfter`,
  the bearer confirmation's `NotOnOrAfter` and `SessionNotOnOrAfter` hold, with the provider's clock skew
  (default 120 s, at most 600 s); an `AuthnStatement` is present.
- **Requests and replay.** `InResponseTo` must name a pending request issued to the browser holding the
  binding cookie, together with its `RelayState`; the request is used up by the login. Accepted assertion
  IDs are kept until they expire and a second use is refused. Both checks are safe against concurrent
  submissions (row update and unique index). Expired requests and replay-cache entries are purged hourly
  (*Purge expired SAML login requests* scheduled job).
- **Rate limiting.** Posts to the ACS share the per-address login limit (`TESSERA_LOGIN_RATE_LIMIT_PER_MINUTE`).
- **Cookies.** In production (secure cookies) the binding cookie is `SameSite=None; Secure` so that the IdP's
  cross-site form post carries it; in development it is `SameSite=Lax`, which works with an IdP on the same
  site (e.g. `localhost`). The session cookie is the usual `SameSite=Lax` cookie. A same-site IdP posting the
  form while an older session cookie is present is handled as anonymous (no CSRF failure).

## Limitations

- **Encrypted assertions** (`EncryptedAssertion`, `EncryptedID`, encrypted attributes) are refused with the
  message "Encrypted assertions are not supported…": turn assertion encryption off for this application at
  the IdP. Transport is protected by HTTPS.
- **IdP-initiated login** (unsolicited responses, e.g. a tile in the IdP portal) is refused: point the tile at
  `/api/v1/auth/saml/<id>/login` instead.
- **Single logout** (SLO) is not implemented: signing out of Tessera ends the Tessera session only.
- Only the HTTP-Redirect binding for requests and HTTP-POST for responses; no Artifact binding, no metadata
  auto-refresh (paste a rotated certificate yourself), no attribute-based group mapping (rights are managed in
  Tessera).
- signxml rejects IdP certificates outside their validity period: renew an expired IdP certificate.
- A failed login does not consume the pending request (the transaction is rolled back), so the same browser
  may retry within the ten minutes, e.g. after an administrator links the account.
