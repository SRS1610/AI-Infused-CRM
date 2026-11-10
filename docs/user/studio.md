# Studio

Studio lets designers (group **Studio / Designer**; administrators have it) customise the suite without
code. Open it from **Settings → Studio → Customise** and pick a model. The page has five tabs (Fields,
Views, Search, Automations, Workflow); the **Change history** stays on the side.

## Fields

### Add a field

Give it a label and a type (text, long text, rich text, whole or decimal number, amount, checkbox, date,
date and time, choice, link to one or several records), then choose where it goes: after a field of the
form, optionally as a list column and on the kanban card. The field exists immediately: its column is
added to the database, it appears in forms, lists, filters, exports and the API, and every other server
process picks it up on its next request. Technical names always start with `x_`.

**More options** sets, for the new field:

- **Widget**: how the field is shown where Studio places it. Text fields can be an email address, a web
  address or a badge; choice fields a status bar or a badge; links to one record a status bar. Other
  types use their standard input.
- **Default value**: the value of new records (for a link, pick the record; for a checkbox, ticked or not).
- **Required** and **Read-only**: *No*, *Always*, or *When…* a condition, written as rows of
  *field – operator – value* ("Stage is Won"), all or any of them. A conditional requirement is checked
  by the form and by the server: a record that matches the condition cannot be saved without a value.
  A read-only condition locks the field on forms.
- **Visible to**: the groups that may see the field (empty: everyone). Other users do not get the field
  in forms, lists or the API.
- **Help text**, shown under the field on forms.

### Edit a field

Pick a field added with Studio to change its label, help, widget, default, conditions and groups. Choice
fields can gain options or have them renamed (one per line); options cannot be removed, since records
may use them. The type and the technical name cannot change. Each save is one change in the history,
and rolling it back restores the previous values (and the previous widget on the views).

## Views

Choose the view to edit: **Form**, **List** or **Kanban**. In each editor, drag an item onto another to
move it after it, or use the arrow buttons, then **Save layout**.

- **Form**: tick **Read-only** to lock a field, **Hide** to remove it from the form.
- **List**: reorder columns, **Hide** them, or make them optional (*Optional, shown* or *Optional,
  hidden*: users can then show or hide the column from the list's column menu). **Add column** puts any
  field of the model at the end.
- **Kanban**: choose the fields shown on each card and their order; **Add to the card** appends a field.

Studio stores every saved layout as an extension of the original view (view inheritance), so module
upgrades keep your layout.

**Create a view** makes a new form, list or kanban view that starts from the layout generated from the
model's fields. Keep **Use it as the default view** ticked to make users (and the editors above) use it;
otherwise it is kept aside for menus that refer to it explicitly. Menus that already name a specific view
(for example the *Leads* list) keep using that view.

## Search

The **Search filters** editor lists the predefined filters and the grouping options of the model's search
panel. Reorder them, remove them, **Edit** a filter's name and condition, add a filter (a name and a
condition), or add a grouping on a link, choice, date, text or checkbox field. **Save search filters**
stores everything as one change.

## Automated actions

Pick the field whose change triggers the action and the to-do to schedule (summary and due date). More
actions (update a field, post a message, notify, call a webhook) are available in **Settings →
Automation → Automations**; Studio records the ones it creates.

## Workflow

A workflow is a simple state machine on a record: its states, in order, and the transitions between
them, which become buttons on the record's status bar.

1. Name the workflow and choose its **State field**: a choice or link field of the model, or **A new
   choice field** (Studio adds it, with the states as options and the first state as default). For an
   existing field, **Use the field's values as states** fills the states in.
2. Add the **States** in the order records go through them. **Folded** hides a state from the status bar
   unless the record is in it; **Done** marks a final state.
3. Add the **Transitions**: the button label, the states it starts **From** (none: from every state), the
   state it goes **To**, optionally the group allowed to use it and a condition (**Only when…**), and
   whether it is the main button.
4. Keep **Change the state only through the transitions** ticked so the state cannot be edited directly.

The **Preview** (and, once saved, the overview) lists each state with the transitions leaving it. A model
has one workflow: **Change this workflow** replaces it (rolling the change back restores the previous
one). Workflows are also visible under **Settings → Automation → Workflows**.

## History, rollback and snapshots

Every change appears in **Change history** with who made it and when. **Roll back** undoes one change:
for a field, it disappears from views and metadata, together with the layout changes, option edits and
workflow that use it, and its column is renamed `_removed_<date>_<name>` (the data is kept, D-007).
Rolling back the same change again does nothing. A layout change that a later change builds on (for
example a column that a later change moved) cannot be rolled back alone: roll back the later one first.
**Take a snapshot** marks a point in time; **Roll back to here** undoes everything made after it, newest
first.

## Export

**Export as module** downloads `studio_customizations_<company>.zip`: an installable module with your
fields (and their options), view changes, new views, search filters, automations and workflows. Install
it on another database (copy it to the module path, then
`tessera module install studio_customizations_<company>`) to reproduce the customisation exactly.
Workflow states on a link field are matched to the linked records by their external id.
