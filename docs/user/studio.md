# Studio

Studio lets designers (group **Studio / Designer**; administrators have it) customise the suite without
code. Open it from **Settings → Studio → Customise** and pick a model.

## Add a field

Give it a label and a type (text, long text, rich text, whole or decimal number, amount, checkbox, date,
date and time, choice, link to one or several records), then choose where it goes: after a field of the
form and, optionally, as a list column. The field exists immediately: its column is added to the
database, it appears in forms, lists, filters, exports and the API, and every other server process
picks it up on its next request. Technical names always start with `x_`.

## Change a layout

The form layout lists the form's fields in order. Drag a field onto another to move it after it (or use
the arrow buttons), tick **Read-only** to lock a field, tick **Hide** to remove it from the form, then
**Save layout**. Studio stores the change as an extension of the original view, so module upgrades keep
your layout.

## Automated actions

Pick the field whose change triggers the action and the to-do to schedule (summary and due date). More
actions (update a field, post a message, notify, call a webhook) are available in **Settings →
Automation → Automations**; Studio records the ones it creates.

## Workflows

Simple state machines (states and allowed transitions on a choice or link field) can be created through
the Studio API and are then edited under **Settings → Automation → Workflows**.

## History, rollback and snapshots

Every change appears in **Change history** with who made it and when. **Roll back** undoes one change:
for a field, it disappears from views and metadata and its column is renamed `_removed_<date>_<name>`
(the data is kept, D-007). Rolling back the same change again does nothing. **Take a snapshot** marks a
point in time; **Roll back to here** undoes everything made after it, newest first.

## Export

**Export as module** downloads `studio_customizations_<company>.zip`: an installable module with your
fields, view changes, views, automations and workflows. Install it on another database (copy it to the
module path, then `tessera module install studio_customizations_<company>`) to reproduce the
customisation exactly.
