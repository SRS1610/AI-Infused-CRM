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
