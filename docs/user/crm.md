# CRM — quick guide

**Leads** are unqualified enquiries; **opportunities** are deals you are working on (the *Pipeline*).

- **Where leads come from.** Emails to your team alias (e.g. `crm@yourdomain`), the web form you can
  embed on your site, integrations through the API, imports, or manual entry. The sender becomes the
  contact, the message the notes; attachments are kept. To embed the form, take the team's *Webform
  token* (Configuration → Sales teams) and add to your page:
  `<div data-tessera-form></div><script src="https://<your-tessera>/api/v1/crm/webform/<token>/embed.js"></script>`
- **Who gets them.** Assignment rules run first (for example "European leads → Europe team"), then the
  team's round robin shares new leads evenly between members.
- **Score.** Scoring rules add points (company known, budget above a threshold, referral …). Open the
  *Score* tab to see why a lead has its score.
- **Qualify.** *Convert to opportunity* creates the customer (company and contact) — or merges the lead
  into an opportunity you already have; its history, activities and files move with it.
- **Work the pipeline.** Drag cards between stages; each column shows the total expected revenue. Stage
  changes set the default probability; the *Weighted revenue* is revenue × probability.
- **Close.** *Won* or *Lost* (with a reason). Lost opportunities are archived and can be restored. They
  still teach: *Objection handling* (AI panel of an opportunity) suggests answers from won **and** lost
  deals you can open, citing each one, with the reason a deal was lost.
- **AI follow-up drafts** quote only prices found in the opportunity's quotations and the products it
  mentions. Any other amount — with a currency or typed as a plain number that reads like a price
  ("the total is 12,500", "a discount of 2,000") — is listed under *check the amounts* on the draft before
  you send it. Dates, times, quantities, phone numbers and percentages are not flagged.
- **Quote.** *New quotation* starts a quotation for the customer; when the customer accepts it online,
  the opportunity is marked won automatically.
- **Plan.** *Schedule meeting* books a meeting with the customer; activities appear in *Activities*.
- **Measure.** *Reporting → Forecast* (weighted revenue by expected closing month) and *Sales analysis*
  (funnel, win rate per salesperson or team, sales velocity).
- **Permissions.** Salespeople edit their own leads, their team's and unassigned ones; CRM managers edit all.
