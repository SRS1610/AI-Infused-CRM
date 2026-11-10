# Email: outgoing, incoming, notifications

## Outgoing mail

Every email Tessera sends is queued in `core.mail_queue` (Settings → Email → Outgoing email) and sent by
the `core.send_mail` job (6 attempts, exponential backoff from 60 s). Failed attempts are recorded on the
email itself (`state`, `error`, `attempts`), so they stay visible even though the job's transaction rolls
back.

**Servers.** Settings → Email → Outgoing mail servers. A server can be limited to one company and to
senders on one domain (`from_filter`); the first matching active server (by sequence) is used. Without
any server record, the environment settings apply:

| Setting | Default | Meaning |
|---|---|---|
| `TESSERA_SMTP_HOST` / `TESSERA_SMTP_PORT` | `localhost` / `1025` | SMTP relay (Mailpit in the dev stack) |
| `TESSERA_SMTP_USER` / `TESSERA_SMTP_PASSWORD` | — | Authentication |
| `TESSERA_SMTP_TLS` | `false` | STARTTLS |
| `TESSERA_MAIL_FROM` | `noreply@tessera.local` | Default sender; its domain is used for Message-IDs |
| `TESSERA_MAIL_CATCHALL_DOMAIN` | sender domain | Domain of aliases and `reply+` addresses |

Server passwords are stored encrypted with the master key (`enc:v1:` envelope).

**Bounces.** Delivery status notifications (RFC 3464 `multipart/report`) that reach Tessera through
any inbound channel mark the original queued email `bounced`.

## Notifications

When someone posts on a record's thread:

* followers subscribed to the message's subtype get an in-app notification (bell icon, live), and an
  email if their preference is *Email*;
* `@login` mentions notify the mentioned user even if they do not follow the record;
* internal notes go only to internal users and are never emailed to followers (mentions excepted);
* nobody is notified about a record they cannot read.

Users who enable **Daily email digest** receive one email at 07:00 UTC listing unread notifications not
yet included in a digest.

## Incoming mail

Three entry points feed the same router:

1. **IMAP** — Settings → Discussion → Incoming mail servers. Polled every 2 minutes; unseen messages are
   processed, then flagged `\Seen`. One bad message never blocks the mailbox.
2. **HTTP** — your MTA posts the raw RFC 822 message to `POST /api/v1/chatter/inbound` with header
   `X-Tessera-Inbound-Token: $TESSERA_MAIL_INBOUND_TOKEN` (or an administrator API key).
3. **Command line / tests** — `process_email(env, raw_bytes)`.

Routing, in order:

1. `reply+<token>@<catchall>` — the token names the record and is HMAC-signed with the server's secret
   key, so it cannot be forged; the email is appended to that record's thread.
2. `In-Reply-To` / `References` matching a known message — appended to that thread.
3. `<alias>@<catchall>` — a **mail alias** (Settings → Email → Mail aliases) creates a record of its
   model via `message_new()` with the alias defaults, as the alias's user and company, then posts the
   email on it. `contact_policy = partners` rejects unknown senders.

Auto-replies (`Auto-Submitted`, `X-Autoreply`) are ignored, duplicates (same Message-ID) are skipped,
and attachments are stored on the record. The sender becomes the message author when their address
matches a user.

## Development

The compose stack includes Mailpit (SMTP on 1025, web UI on http://localhost:8025). Mailpit has no IMAP;
test incoming mail with the HTTP endpoint:

```bash
curl -X POST http://localhost:8000/api/v1/chatter/inbound \
  -H "X-Tessera-Inbound-Token: $TESSERA_MAIL_INBOUND_TOKEN" --data-binary @message.eml
```
