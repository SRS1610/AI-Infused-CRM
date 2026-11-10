// Client for the platform endpoints of sub-phase 1.3: chatter, workflow, reports, search, import/export.
import { request } from "./client";
import type { Domain } from "./types";

export interface ChatterMessage {
  id: number;
  type: "comment" | "note" | "notification" | "email" | "system" | "activity_done";
  internal: boolean;
  subject: string | null;
  body: string;
  date: string | null;
  author: { id: number; name: string | null } | null;
  email_from: string | null;
  tracking: { field: string; label: string; old: string | null; new: string | null }[];
  attachments: { id: number; name: string; mimetype: string | null; size: number | null }[];
  reactions: { emoji: string; count: number; users: string[]; mine: boolean }[];
  ai_assisted: boolean;
  editable: boolean;
}

export interface Activity {
  id: number;
  model: string;
  res_id: number;
  res_name: string | null;
  type: { id: number; name: string; icon: string | null };
  category: string;
  summary: string | null;
  note: string | null;
  date_deadline: string;
  state: "overdue" | "today" | "planned" | "done";
  user: { id: number; name: string };
}

export interface Followers {
  items: { id: number; user_id: number; name: string; subtypes: number[] }[];
  following: boolean;
  subtypes: { id: number; name: string; internal: boolean }[];
}

export interface StatusBar {
  workflow: { id: number; name: string; field: string; strict: boolean } | null;
  current?: string | null;
  states: { key: string; name: string; fold: boolean; final: boolean; current: boolean }[];
  transitions: { id: number; name: string; to: string; confirm: string | null; highlight: boolean }[];
}

export interface SearchHit {
  model: string;
  model_label: string;
  id: number;
  title: string;
  score: number;
}

export interface ImportJob {
  id: number;
  model: string;
  filename: string;
  headers: string[];
  preview: string[][];
  row_count: number;
  mapping: Record<string, string | null>;
  options: Record<string, unknown>;
  state: "draft" | "tested" | "queued" | "running" | "done" | "failed";
  created: number;
  updated: number;
  error_count: number;
  errors: { row: number | null; column: string | null; field: string | null; message: string }[];
  job_id: number | null;
  fields?: { name: string; label: string; type: string; required: boolean; paths?: string[] }[];
  saved_mapping?: string | null;
  model_label?: string;
  result?: { created: number; updated: number; error_count: number; errors: ImportJob["errors"] };
}

export const chatter = {
  messages: (model: string, id: number, beforeId?: number) =>
    request<{ items: ChatterMessage[]; has_more: boolean }>("GET", `/chatter/${model}/${id}/messages`, {
      query: { before_id: beforeId },
    }),
  post: (
    model: string,
    id: number,
    body: { body: string; internal?: boolean; attachment_ids?: number[]; mention_user_ids?: number[] },
  ) => request<ChatterMessage>("POST", `/chatter/${model}/${id}/messages`, { body }),
  react: (messageId: number, emoji: string) =>
    request<ChatterMessage>("POST", `/chatter/messages/${messageId}/reactions`, { body: { emoji } }),
  edit: (messageId: number, body: string) =>
    request<ChatterMessage>("PATCH", `/chatter/messages/${messageId}`, { body: { body } }),
  remove: (messageId: number) => request<void>("DELETE", `/chatter/messages/${messageId}`),
  followers: (model: string, id: number) => request<Followers>("GET", `/chatter/${model}/${id}/followers`),
  follow: (model: string, id: number) => request<Followers>("POST", `/chatter/${model}/${id}/followers`, { body: {} }),
  unfollow: (model: string, id: number) => request<Followers>("DELETE", `/chatter/${model}/${id}/followers`),
  activities: (model: string, id: number) => request<Activity[]>("GET", `/chatter/${model}/${id}/activities`),
  schedule: (
    model: string,
    id: number,
    body: { activity_type?: string | number; summary?: string; date_deadline?: string; user_id?: number },
  ) => request<Activity>("POST", `/chatter/${model}/${id}/activities`, { body }),
  done: (activityId: number, feedback?: string) =>
    request<{ ok: boolean }>("POST", `/chatter/activities/${activityId}/done`, { body: { feedback } }),
  snooze: (activityId: number, days = 1) =>
    request<Activity>("POST", `/chatter/activities/${activityId}/snooze`, { body: { days } }),
  mine: (state?: string) =>
    request<{
      summary: { model: string; label: string; overdue: number; today: number; planned: number }[];
      items: Activity[];
    }>("GET", "/chatter/activities/mine", { query: { state } }),
  mentions: (q: string) =>
    request<{ id: number; name: string; login: string }[]>("GET", "/chatter/mentions", { query: { q } }),
  upload: (file: File, model: string, resId: number) => {
    const form = new FormData();
    form.append("file", file);
    form.append("model", model);
    form.append("res_id", String(resId));
    return request<{ id: number; name: string }>("POST", "/attachments", { body: form });
  },
};

export const workflow = {
  statusbar: (model: string, id: number) => request<StatusBar>("GET", `/workflow/${model}/${id}/statusbar`),
  transition: (model: string, id: number, transitionId: number) =>
    request<StatusBar>("POST", `/workflow/${model}/${id}/transitions/${transitionId}`),
};

export const reports = {
  forModel: (model: string) =>
    request<{ id: number; xmlid: string | null; name: string }[]>("GET", `/reports/for/${model}`),
  url: (templateId: number, ids: number[], format: "pdf" | "html" = "pdf") =>
    `/api/v1/reports/${templateId}/render?ids=${ids.join(",")}&format=${format}`,
};

export const search = {
  global: (q: string) =>
    request<{ records: SearchHit[]; menus: { id: number; name: string; path: string; action: string }[] }>(
      "GET",
      "/web/search",
      { query: { q } },
    ),
};

export const dataio = {
  upload: (model: string, file: File, options: Record<string, unknown> = {}) => {
    const form = new FormData();
    form.append("file", file);
    form.append("options", JSON.stringify(options));
    return request<ImportJob>("POST", `/import/${model}`, { body: form });
  },
  get: (id: number) => request<ImportJob>("GET", `/import/jobs/${id}`),
  update: (id: number, body: { mapping?: Record<string, string | null>; save_as?: string; shared?: boolean }) =>
    request<ImportJob>("PATCH", `/import/jobs/${id}`, { body }),
  dryRun: (id: number) => request<ImportJob>("POST", `/import/jobs/${id}/dry-run`),
  start: (id: number) => request<ImportJob>("POST", `/import/jobs/${id}/start`),
  exportFields: (model: string, prefix = "") =>
    request<{ path: string; label: string; type: string; relational: boolean }[]>("GET", `/export/${model}/fields`, {
      query: { prefix },
    }),
  export: (body: { model: string; fields: string[]; domain?: Domain; ids?: number[]; format: "csv" | "xlsx" }) =>
    request<Response>("POST", "/export", { body, raw: true }),
};

export interface NotificationItem {
  id: number;
  title: string;
  body: string | null;
  kind: string;
  model: string | null;
  res_id: number | null;
  is_read: boolean;
  create_date?: string;
}
