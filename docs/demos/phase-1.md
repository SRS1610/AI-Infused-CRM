# Phase 1 demo: foundation, CRM and AI

A walkthrough of everything Phase 1 delivers, in the order of the build plan (§2.8). It runs against the
demo scenario, with the deterministic **Local test provider** standing in for a model provider, so it
needs no API keys. With a real provider configured (**Settings → AI → Providers / Routing**) the same
steps show real model output.

The automated version of this walkthrough is `frontend/e2e/demo.spec.ts` (see "Executing the demo").

## Prepare

```bash
docker compose -f deploy/compose/docker-compose.yml up -d
```

```bash
docker compose -f deploy/compose/docker-compose.yml exec api tessera seed demo --phase 1
```

Without Docker: start PostgreSQL with pgvector, then from `backend/` run `tessera seed demo` and
`tessera serve`, and from `frontend/` run `npm run dev`. Set `TESSERA_AI_GATEWAY_URL=inprocess` to run
the AI gateway inside the API process, or start it with `gateway serve` (port 8100).

`tessera seed demo` installs every Phase 1 module with demo data, builds the retrieval index, trains the
lead-scoring models (companies with too little history fall back to the scoring rules), assesses deal
risk, computes the forecast and gives the test provider realistic answers for the steps below.

Users: `admin` / `admin1234` (administrator), `demo` / `demo1234` (Dana Demo, two companies, English),
`marie` / `demo1234` (Marie Martin, European company only, French).

## 1. Sign in, companies and languages

1. Sign in as `admin`. The home page lists the installed apps.
2. Open the company switcher in the top bar: **My Company** (USD) and **My Company (Europe)** (EUR).
3. Sign out and sign in as `marie`: the interface is in French and she only sees the European company.

## 2. Contacts: duplicates, merge and undo

1. **Contacts → Contacts → New**: create *Northwind Traders* (company), then *Lena Ortiz* and *Lena
   Ortíz* with the same e-mail, both linked to Northwind.
2. Open one of the two Lenas: the **Possible duplicates** panel beside the form lists the other with a
   score and the reasons (same e-mail, similar name).
3. **Propose merge** creates a merge proposal (labelled AI-drafted). Accept it: the two contacts become
   one, and every reference to the removed one now points to the survivor.
4. Open the merge from **Contacts → Merges** and **Undo** it within 7 days.

## 3. Discuss: realtime, mentions and "Catch me up"

1. Open **Discuss**, channel **#sales**, post a message mentioning `@demo`.
2. In a second browser signed in as `demo`, the message arrives without reloading and the bell shows a
   notification.
3. As `demo`, click **✦ Catch me up** in the sidebar: a ranked digest of unread messages, mentions
   first, each with a link to its channel. **✦ Summarize** in a channel header summarises it with links
   to the cited messages; **✦ Translate** under a message translates it into the user's language.

## 4. Calendar: rooms, conflicts and scheduling in plain words

1. **Calendar → New**: book the meeting room for tomorrow 10:00–11:00. Book the same room for 10:30:
   the second booking is refused with the conflicting meeting named.
2. In the calendar's search bar, use **✦ Schedule with AI** and type *lunch with Dana next Tuesday
   12:30*. A proposed event appears (title, date, time, attendee Dana Demo) with the AI-drafted label.
   Accept it: the event is created as you, nothing before.
3. Open the meeting: the **Meeting brief** panel summarises who attends and the related deals, citing
   only records you may read; **Suggested times** lists free slots for all attendees.

## 5. CRM intake: e-mail and web form

1. Send this e-mail to the CRM alias (`crm@` the catch-all domain; in Docker, through Mailpit):

   > Hello, I'm Priya Shah, Head of Operations at Lumen Robotics (priya.shah@lumen-robotics.example,
   > +44 20 7946 0958). We need 8 collaborative robots for our Leeds plant by the end of March; our
   > budget is about 90,000. Could someone call me this week?

   A lead is created. The extraction job proposes the contact name, job title, company, phone, city and
   expected revenue; the salesperson gets a notification with a link. Open the lead and accept the
   proposal in the **AI suggestions** panel.
2. Submit the web form (**CRM → Configuration → Teams → Sales → Web form**, or the embed script) for a
   second lead.

## 6. Routing

1. On an unassigned lead, **Suggest salesperson** proposes an assignee with reasons (assignment rules,
   track record on similar deals, current load). Accept it.
2. From the leads list, select several unassigned leads and run **Suggest salespeople (AI)**: review
   the proposals row by row. Without AI, teams still assign round-robin.

## 7. Pipeline

Open **CRM → Pipeline**, drag a card to the next stage: the column totals update, the chatter shows the
stage change, and the probability follows the stage.

## 8. Convert

Open the Lumen Robotics lead, **Convert to opportunity**, choose to link it to the existing contact.

## 9. Opportunity insights

Open *Acme: 6 cobots for line 2*:

- **AI insights**: the score (0–100, labelled AI) and win probability; **Why this score** lists the top
  five factors. Personal attributes are never model inputs.
- **Next best action**: up to three ranked actions with their rationale; **Schedule** turns one into a
  proposed to-do.
- A **deal risk** badge with its reasons appears on deals that stalled.

## 10. Follow-up e-mail with AI

In the opportunity's chatter, **Send message → ✦ Draft with AI**: the draft streams in with the
**AI-drafted** label. Edit it (the label disappears: a person now owns the text) and **Send**. Prices
in a draft must come from the quotations or products in its sources; anything else is flagged.

## 11. Call notes

In the **Summarise call notes** panel, paste:

> Call with Jordan Lee (Acme Robotics). They confirmed 6 cobots for line 2, maybe 8 if the pilot goes
> well. Budget approved at 75,600. Decision by 15 December. Jordan wants a revised quote with training
> included by Friday. Their new procurement contact is Sam Patel, Purchasing Manager.

When the job finishes (notification), the panel shows the summary and one proposal per change (expected
revenue, probability, closing date, priority, contact, the next step as a to-do, the summary as a note).
Accept some and reject others. The **AI run audit** lists the run: prompt version, provider, model,
tokens, cost and each decision.

## 12. Search in plain words

In **CRM → Pipeline**, **Ask AI**: *my open deals over 10k closing this quarter*. Review the suggested
conditions (edit or remove any), **Apply filter**, then save the search as a favourite.

## 13. Quotation and customer portal

From the opportunity, **New quotation**, add products, **Print** (PDF with the company layout and a QR
code), **Send**. Open the link from the e-mail as the customer, accept the quotation: the opportunity is
marked won.

## 14. Reports

**CRM → Reporting → Sales analysis**: win rate by salesperson and team, sales velocity, funnel, and the
**Forecast** section with P10 / P50 / P90 per month and its explanation. **Forecast** opens the pivot and
graph views of the pipeline.

## 15. Studio

**Settings → Studio → Customise**, model *Lead / opportunity*:

1. **Add a field**: label *Competitor*, type *Text*, after *Customer*, shown in the list.
2. **Automated action**: when *Stage* changes, schedule the to-do *Check in with the customer*.
3. Open an opportunity: the Competitor field is there; change the stage and save: the to-do appears.
4. In **Change history**, roll back the automation and the field: the field disappears from the form
   (its data is kept in a renamed column).

## 16. Administration

- **Settings → AI → Usage and cost**: requests, tokens, spend, latency p50/p95, acceptance rate per
  company and feature; switch **Reply draft** (Discuss) off for *My Company (Europe)*: Marie no longer
  sees the button, and the API answers `feature_disabled` without calling any provider.
- **Settings → Technical → Import**: import leads from CSV, run the **dry run** first (row-level errors,
  nothing written), then import.
- **Settings → Technical → Background jobs**: the job ledger with attempts, retries and results.

## Executing the demo

`frontend/e2e/demo.spec.ts` runs the AI steps of this script in a browser against the seeded stack
(steps 2, 3, 4, 5, 9, 11, 12, 14 and 16; steps 10 and 15 are covered by `e2e/ai.spec.ts` and
`e2e/studio.spec.ts`; steps 7, 8 and 13 by `e2e/apps.spec.ts`):

```bash
cd frontend && npx playwright test e2e/demo.spec.ts e2e/ai.spec.ts e2e/studio.spec.ts e2e/apps.spec.ts
```

It was last executed on 2026-11-10 against `tessera seed demo` with the in-process gateway and the
Local test provider. Docker Compose itself could not be started in that environment; the stack was run
directly (see the [Phase 1 summary](../phase-1-summary.md)).
