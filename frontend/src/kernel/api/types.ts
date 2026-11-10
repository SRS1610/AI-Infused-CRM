// Types mirroring the backend contract (docs/api/README.md).

export type Domain = unknown; // nested JSON domain DSL: [path, op, value] | {all|any: [...]} | {not: ...}

export interface Many2oneValue {
  id: number;
  display_name: string | null;
}

export type FieldType =
  | "char"
  | "text"
  | "html"
  | "integer"
  | "float"
  | "monetary"
  | "boolean"
  | "date"
  | "datetime"
  | "selection"
  | "json"
  | "binary"
  | "image"
  | "many2one"
  | "one2many"
  | "many2many"
  | "reference";

export interface FieldMeta {
  name: string;
  type: FieldType;
  string: string;
  help?: string;
  required: boolean;
  readonly: boolean;
  store: boolean;
  comodel?: string | null;
  selection?: [string, string][] | null;
  currency_field?: string;
  translate?: boolean;
  related?: string | null;
  compute?: string | null;
  inverse_name?: string;
  domain?: Domain;
  readonly_if?: Domain;
}

export type FieldsMeta = Record<string, FieldMeta>;
export type RecordData = Record<string, any> & { id?: number };

export interface ViewNode {
  kind?: string;
  name?: string;
  field?: string;
  widget?: string;
  title?: string;
  label?: string;
  text?: string;
  columns?: number | ViewNode[];
  children?: ViewNode[];
  pages?: ViewNode[];
  readonly?: boolean;
  invisible?: Domain;
  method?: string;
  options?: Record<string, any>;
  [key: string]: any;
}

export interface ListColumn {
  field: string;
  widget?: string;
  optional?: "show" | "hide";
  label?: string;
  sum?: boolean;
}

export interface SearchFilterDef {
  name: string;
  label: string;
  domain: Domain;
}

export interface ViewDef {
  type: string;
  model: string;
  generated?: boolean;
  view_id?: number;
  root?: ViewNode;
  columns?: ListColumn[];
  chatter?: boolean;
  fields?: string[];
  filters?: SearchFilterDef[];
  group_bys?: { field: string; label?: string }[];
  default_group_by?: string;
  date_start?: string;
  measures?: string[];
  editable?: boolean;
  [key: string]: any;
}

export interface ViewsResponse {
  model: {
    name: string;
    description: string;
    rec_name: string;
    order: string;
    methods: string[];
    access: Record<"read" | "write" | "create" | "unlink", boolean>;
  };
  fields: FieldsMeta;
  views: Record<string, ViewDef>;
}

export interface ActionDef {
  id: number;
  xmlid: string | null;
  name: string;
  kind: "window" | "server" | "url" | "client" | "report";
  model: string | null;
  view_types: string[];
  view_refs: Record<string, string>;
  search_view_ref: string | null;
  domain: Domain | null;
  context: Record<string, any>;
  target: "current" | "new" | "fullscreen";
  url: string | null;
  client_tag: string | null;
  limit: number;
  help: string | null;
}

export interface MenuNode {
  id: number;
  xmlid: string | null;
  name: string;
  sequence: number;
  icon: string | null;
  action: string | null;
  action_id: number | null;
  children: MenuNode[];
}

export interface SessionInfo {
  user: {
    id: number;
    login: string;
    name: string;
    email: string | null;
    lang: string;
    tz: string;
    share: boolean;
    totp_enabled: boolean;
    is_admin: boolean;
  };
  company: { id: number; name: string } | null;
  allowed_companies: { id: number; name: string }[];
  selected_companies: number[];
  groups: string[];
  auth: string;
  csrf_token: string | null;
  lang: string;
  modules: string[];
}

export interface ListResponse<T = RecordData> {
  items: T[];
  total: number | null;
  limit: number;
  offset: number;
  next_cursor?: number | null;
}

export interface GroupRow {
  __count: number;
  __domain: Domain;
  [key: string]: any;
}

export interface ApiErrorBody {
  error: string;
  message: string;
  request_id?: string;
  [key: string]: any;
}
