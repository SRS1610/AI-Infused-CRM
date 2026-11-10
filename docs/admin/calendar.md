# Calendar synchronisation — administration

## Google and Microsoft accounts (OAuth)

A Google Calendar or Microsoft 365 account (**Calendar → External calendars**) stores (tokens encrypted
and visible to administrators only):

| Field | Meaning |
|---|---|
| Refresh token | Long-lived grant from the provider; used to obtain access tokens |
| Access token | Short-lived bearer token; renewed automatically |
| Token expiry | When the access token expires |

Before each synchronisation the access token is renewed when it expires within five minutes, and if the
provider answers *401 Unauthorized* during a synchronisation the token is renewed once and the request
retried. Microsoft returns a new refresh token with each renewal; it replaces the stored one.

Renewal needs the OAuth client registered with the provider (Google Cloud console, Microsoft Entra app
registration, with the `offline_access` and `Calendars.ReadWrite` permissions for Microsoft). Configure it
in the server environment:

```
TESSERA_GOOGLE_OAUTH_CLIENT_ID=…
TESSERA_GOOGLE_OAUTH_CLIENT_SECRET=…
TESSERA_MICROSOFT_OAUTH_CLIENT_ID=…
TESSERA_MICROSOFT_OAUTH_CLIENT_SECRET=…
TESSERA_MICROSOFT_OAUTH_TENANT=common        # or your tenant id / domain
```

or per company with the company setting `calendar.oauth_clients` (it wins over the environment for the
users of that company; a secret may be stored encrypted with the master key):

```json
{"google": {"client_id": "…", "client_secret": "…"},
 "microsoft": {"client_id": "…", "client_secret": "enc:v1:…", "tenant": "contoso.onmicrosoft.com"}}
```

**Needs reconnection.** When the provider refuses the refresh token (`invalid_grant`: access revoked by
the user, password change, token expired after long inactivity) or no OAuth client is configured, the
account's state becomes *Needs reconnection*, its *Last error* says why, the scheduled synchronisation
skips it, and its owner receives one notification. Entering a new refresh token (or password for CalDAV)
sets it back to *Active*. A provider outage or network error only sets the state to *Error*; the next
scheduled run retries.

Accounts connected before this version kept their access token in the *Secret* field; they keep working
until that token expires and then need a refresh token.
