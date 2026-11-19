// A small CRM lead model for the Studio component tests.
import type { FieldMeta, FieldsMeta } from "../kernel/api/types";
import { mockFetch } from "./mockApi";

const f = (name: string, type: FieldMeta["type"], string: string, extra: Partial<FieldMeta> = {}): FieldMeta => ({
  name,
  type,
  string,
  required: false,
  readonly: false,
  store: true,
  ...extra,
});

export const leadFields: FieldsMeta = {
  name: f("name", "char", "Name", { required: true }),
  partner_id: f("partner_id", "many2one", "Customer", { comodel: "contacts.partner" }),
  stage_id: f("stage_id", "many2one", "Stage", { comodel: "crm.stage" }),
  priority: f("priority", "selection", "Priority", {
    selection: [
      ["0", "Normal"],
      ["1", "High"],
    ],
  }),
  expected_revenue: f("expected_revenue", "monetary", "Expected revenue"),
  email_from: f("email_from", "char", "Email"),
  date_deadline: f("date_deadline", "date", "Expected closing"),
  is_won: f("is_won", "boolean", "Is won"),
  x_channel: f("x_channel", "selection", "Channel", {
    selection: [
      ["web", "Web"],
      ["fair", "Fair"],
    ],
  }),
};

export const leadViews = () => ({
  model: { name: "crm.lead", description: "Lead", rec_name: "name", order: "id", methods: [], access: {} },
  fields: leadFields,
  views: {
    form: {
      type: "form",
      model: "crm.lead",
      view_id: 3,
      root: {
        kind: "sheet",
        children: [
          { kind: "field", name: "name" },
          {
            kind: "group",
            children: [
              { kind: "field", name: "partner_id" },
              { kind: "field", name: "stage_id" },
            ],
          },
        ],
      },
    },
    list: {
      type: "list",
      model: "crm.lead",
      view_id: 4,
      columns: [{ field: "name" }, { field: "partner_id", optional: "hide" }, { field: "stage_id" }],
    },
    kanban: {
      type: "kanban",
      model: "crm.lead",
      view_id: 5,
      root: {
        kind: "card",
        children: [
          { kind: "field", name: "name", widget: "title" },
          { kind: "field", name: "partner_id" },
          { kind: "field", name: "priority" },
        ],
      },
    },
    search: {
      type: "search",
      model: "crm.lead",
      view_id: 6,
      fields: ["name"],
      filters: [
        { name: "mine", label: "My pipeline", domain: [["user_id", "=", "$uid"]] },
        { name: "won", label: "Won", domain: [["stage_id", "=", 4]] },
      ],
      group_bys: [{ field: "stage_id" }],
    },
  },
});

export const channelOptions = {
  label: "Channel",
  help: null,
  required: false,
  readonly: false,
  required_if: null,
  readonly_if: null,
  default: null,
  selection: [
    ["web", "Web"],
    ["fair", "Fair"],
  ],
  groups: [],
  widget: null,
  tracking: false,
  ttype: "selection",
  comodel: null,
};

export function studioRoutes(extra: Record<string, (url: URL, init: RequestInit) => unknown> = {}) {
  return mockFetch({
    "GET /api/v1/studio/models": () => [
      { model: "crm.lead", description: "Lead", module: "crm" },
      { model: "contacts.partner", description: "Contact", module: "contacts" },
    ],
    "GET /api/v1/studio/models/crm.lead/fields": () => [
      { name: "name", label: "Name", type: "char", studio: false },
      { name: "partner_id", label: "Customer", type: "many2one", studio: false },
      { name: "stage_id", label: "Stage", type: "many2one", studio: false },
      { name: "x_channel", label: "Channel", type: "selection", studio: true, options: channelOptions },
    ],
    "GET /api/v1/studio/groups": () => [
      { xmlid: "crm.group_crm_user", name: "Sales / CRM / User" },
      { xmlid: "crm.group_crm_manager", name: "Sales / CRM / Manager" },
    ],
    "GET /api/v1/models/crm.stage/name_search": () => [
      { id: 1, display_name: "New" },
      { id: 4, display_name: "Won" },
    ],
    "GET /api/v1/web/views": () => leadViews(),
    "GET /api/v1/studio/changes": () => [
      {
        id: 7,
        kind: "add_field",
        model: "crm.lead",
        summary: "Added field Competitor",
        state: "applied",
        user: "Admin",
        date: null,
      },
    ],
    "GET /api/v1/studio/snapshots": () => [{ id: 2, name: "Before demo", date: "2026-11-10T10:00:00Z" }],
    "GET /api/v1/studio/models/crm.lead/workflow": () => ({ workflow: null }),
    "POST /api/v1/studio/fields": () => ({ field: "x_competitor", change_id: 8 }),
    "PATCH /api/v1/studio/models/crm.lead/fields/x_channel": () => ({ id: 11 }),
    "POST /api/v1/studio/views/crm.lead/form/operations": () => ({ id: 9 }),
    "POST /api/v1/studio/views/crm.lead/list/operations": () => ({ id: 12 }),
    "POST /api/v1/studio/views/crm.lead/kanban/operations": () => ({ id: 13 }),
    "POST /api/v1/studio/views/crm.lead/search/operations": () => ({ id: 14 }),
    "POST /api/v1/studio/views": () => ({ id: 15 }),
    "POST /api/v1/studio/workflows": () => ({ id: 16 }),
    "POST /api/v1/studio/automations": () => ({ id: 10 }),
    "POST /api/v1/studio/changes/7/rollback": () => ({ id: 7, state: "rolled_back" }),
    "POST /api/v1/studio/snapshots/2/rollback": () => ({ rolled_back: 3 }),
    "POST /api/v1/studio/snapshots": () => ({ id: 3 }),
    ...extra,
  });
}
