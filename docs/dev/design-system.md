# Design system

The web client's look is defined once, in `frontend/src/styles.css`, and built from tokens (D-103). Modules
style their own components with the same tokens and classes; they never hard-code colours.

## Principles

- **Dense and crisp**: 32 px controls, 40 px grid rows, 4 px corners (6 px for cards), 1 px borders, light
  elevation. Data first; chrome stays quiet.
- **One shell**: navy suite bar, app navigation on the left, a white command band at the top of every page,
  content on a cool grey canvas.
- **One accent**: cobalt (`--primary`) for actions, selection and focus. Status colours (`--success`,
  `--warning`, `--danger`) only describe state; AI uses violet (`--ai`).
- **Both themes, equal care**: every token has a light and a dark value; components only use tokens.
- **Accessible by construction**: text pairs ≥ 4.5:1 (checked by axe in the end-to-end suite and the menu
  crawl), visible focus rings, scrollable regions focusable, icons `aria-hidden` next to a text label.

## Tokens

| Group | Tokens |
|---|---|
| Surfaces | `--bg` (canvas), `--surface` (cards, bands), `--surface-2` (headers, footers), `--surface-3` (hover) |
| Text | `--text`, `--text-muted` |
| Lines | `--border`, `--border-strong` (inputs, table header rule) |
| Accent | `--primary`, `--primary-hover`, `--primary-active`, `--primary-text`, `--primary-soft`, `--primary-soft-2` |
| Suite bar | `--suite`, `--suite-text`, `--suite-muted`, `--suite-hover`, `--suite-border` |
| State | `--success`, `--warning`, `--danger` and their `-soft` backgrounds; `--ai`, `--ai-soft` |
| Shape | `--radius` (4 px), `--radius-lg` (6 px), `--elev-1`…`--elev-3`, `--control-h`, `--suite-h` |
| Type | `--font` (Inter Variable, bundled; Segoe UI and system fonts as fallback), `--mono` |
| Apps and charts | `--app-1`…`--app-8` (app tiles, by menu position), `--chart-1`…`--chart-6` |

## Building blocks

| Class | Use |
|---|---|
| `.btn`, `.btn.primary`, `.btn.ghost`, `.btn.small`, `.btn.icon-only` | buttons; command bars use ghost buttons with an icon and a label |
| `.input` | text, select and number inputs (focus draws a 2 px accent line at the bottom) |
| `.badge` + `.badge-<value>` | status pills; the value picks the colour (`posted`, `paid`, `done` green; `sent`, `in_payment` blue; `paused`, `to_check` amber; `cancel`, `lost`, `not_paid` red) |
| `.tag` | many2many chips |
| `.avatar`, `.avatar.small` | initials of a person (`initials()` in `kernel/icons.tsx`) |
| `.control-panel` (`.cp-title-row`, `.command-bar`, `.cmd-group`) | list page header |
| `.form-toolbar`, `.record-header` (`.rh-type`, `.rh-name`, `.rh-facts`), `.process-bar`, `.sheet-card` | record page |
| `.form-panel`, `.form-panel-title` | right-hand panels on a record (AI assistants) |
| `.kanban-card` (`.card-title`, `.card-line`, `.card-amount`, `.card-foot`, `.card-owner`, `.card-stars`) | kanban cards |
| `.table-wrap`, `.table`, `.table-scroll` | grids; `.num` right-aligns numbers with tabular figures |
| `.access-denied` | the page shown for a 403 (`AccessDenied` component) |

## Icons

`kernel/icons.tsx` exposes `<Icon name="…" />` over a curated set of [Lucide](https://lucide.dev) icons (ISC
licence), imported one by one so the bundle carries only those. Add an icon by importing it there and
adding it to `ICONS`.

Root menus declare their app icon in `menus.json` (`"icon": "landmark"`); menu entries without one get an
icon from their action's external id (`menuIcon()`: invoices → receipt, bank → landmark, AI → sparkles…).
An unknown name draws a neutral circle.

## Record header and command bar

`FormView` lifts two kinds of nodes out of a form's sheet:

- a top-level `buttons` node goes to the command bar, after Save and Discard;
- a top-level field with `widget: "statusbar"` goes to the stage bar under the header.

The header shows the record's kind (the label of `move_type` or `type` when the form has one, else the model
description) and up to three facts found in the form: an amount (`amount_total`, `expected_revenue`,
`amount_residual`, `recurring_monthly`, `amount`), the owner (`user_id`) and a status (`state`,
`payment_state`, `subscription_state`, `invoice_status`).

## Lists

Totals: a list view's columns marked `"sum": true` are totalled; when none is marked, monetary columns are.
Many2many values are shown by name (`RelatedNames`, one cached request per set of ids), falling back to a
count when the user cannot read them. Pivot and graph labels for date groups read “Nov 2026”, “Q4 2026”,
“W46 2026” or “2026”, and are sorted by date.
