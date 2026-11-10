// Extension points for application modules' client code (registered at import time from src/modules).
import type { ReactElement } from "react";

export interface SelectionAction {
  label: string;
  /** Minimum number of selected records (default 1). */
  min?: number;
  render: (props: { model: string; ids: number[]; onDone: () => void }) => ReactElement;
}

const selectionActions = new Map<string, SelectionAction[]>();
const clientActions = new Map<string, () => ReactElement>();

export function registerSelectionAction(model: string, action: SelectionAction): void {
  const list = selectionActions.get(model) ?? [];
  if (!list.some((a) => a.label === action.label)) list.push(action);
  selectionActions.set(model, list);
}

export function selectionActionsFor(model: string): SelectionAction[] {
  return selectionActions.get(model) ?? [];
}

export function registerClientAction(tag: string, render: () => ReactElement): void {
  clientActions.set(tag, render);
}

export function clientAction(tag: string | null | undefined): (() => ReactElement) | undefined {
  return tag ? clientActions.get(tag) : undefined;
}

const aiDrafts = new Map<string, string>();

/** The AI feature that drafts a reply in the chatter composer of ``model`` (streamed, reviewed, sent by the user). */
export function registerAiDraft(model: string, feature: string): void {
  aiDrafts.set(model, feature);
}

export function aiDraftFeature(model: string): string | undefined {
  return aiDrafts.get(model);
}

export interface FormPanel {
  /** Accessible name and heading of the panel. */
  title: string;
  /** Shown beside the form of an existing record (above the chatter). Rendered outside the <form>. */
  render: (props: {
    model: string;
    resId: number;
    record: Record<string, unknown>;
    reload: () => void;
  }) => ReactElement;
}

const formPanels = new Map<string, FormPanel[]>();

export function registerFormPanel(model: string, panel: FormPanel): void {
  const list = formPanels.get(model) ?? [];
  if (!list.some((p) => p.title === panel.title)) list.push(panel);
  formPanels.set(model, list);
}

export function formPanelsFor(model: string): FormPanel[] {
  return formPanels.get(model) ?? [];
}

export interface SearchAddon {
  key: string;
  /** Rendered in the search bar of ``model``; ``apply`` adds a domain as a removable custom filter. */
  render: (props: { model: string; apply: (domain: unknown) => void }) => ReactElement;
}

const searchAddons = new Map<string, SearchAddon[]>();

export function registerSearchAddon(model: string, addon: SearchAddon): void {
  const list = searchAddons.get(model) ?? [];
  if (!list.some((a) => a.key === addon.key)) list.push(addon);
  searchAddons.set(model, list);
}

export function searchAddonsFor(model: string): SearchAddon[] {
  return searchAddons.get(model) ?? [];
}
