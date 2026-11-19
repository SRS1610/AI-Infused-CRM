// Minimal gettext-style translation keyed by the English source text.
//
//   t("Save")                                  -> "Enregistrer" when the user's language is fr_FR
//   t("Welcome, {name}", { name })             -> placeholders are replaced after translation
//   tn("{count} record", "{count} records", n) -> plural form chosen with Intl.PluralRules
//   tx("Press {key} to save", { key: <kbd>Enter</kbd> }) -> same, with React elements as values
//
// The session calls setLanguage() before it stores the signed-in user, so the re-render that follows
// sign-in is already translated; useT()/useLanguage() subscribe a component to later switches.
// Pass literal strings: `npm run i18n:extract` only collects literal first arguments.
//
// Catalogues live in src/i18n/<lang>.json (English -> translation) and are bundled; an unknown
// string or an empty translation falls back to English. `npm run i18n:extract` keeps them in sync.
import { createElement, Fragment, useSyncExternalStore, type ReactNode } from "react";

export type Catalogue = Record<string, string>;
export type Vars = Record<string, string | number | null | undefined>;

const SOURCE_LANGUAGE = "en_US";

const catalogues: Record<string, Catalogue> = Object.fromEntries(
  Object.entries(import.meta.glob<Catalogue>("../i18n/*.json", { eager: true, import: "default" })).map(
    ([path, catalogue]) => [path.replace(/^.*\/|\.json$/g, ""), catalogue],
  ),
);

let language = SOURCE_LANGUAGE;
let catalogue: Catalogue = {};
let plurals = new Intl.PluralRules("en-US");
const listeners = new Set<() => void>();

/** Languages with a catalogue, plus the English source. */
export function availableLanguages(): string[] {
  return [SOURCE_LANGUAGE, ...Object.keys(catalogues).sort()];
}

/** Best catalogue for a code such as "fr_FR", "fr-BE" or "fr"; English when there is none. */
export function resolveLanguage(lang: string | null | undefined): string {
  const code = (lang ?? "").replace("-", "_");
  if (catalogues[code]) return code;
  const base = code.split("_")[0].toLowerCase();
  return Object.keys(catalogues).find((c) => c.split("_")[0].toLowerCase() === base) ?? SOURCE_LANGUAGE;
}

export function getLanguage(): string {
  return language;
}

/** Switch the UI language (called when the session loads). Components using useT() re-render. */
export function setLanguage(lang: string | null | undefined): void {
  const next = resolveLanguage(lang);
  if (next === language) return;
  language = next;
  catalogue = catalogues[next] ?? {};
  try {
    plurals = new Intl.PluralRules(next.replace("_", "-"));
  } catch {
    plurals = new Intl.PluralRules("en-US");
  }
  if (typeof document !== "undefined") document.documentElement.lang = next.split("_")[0];
  listeners.forEach((listener) => listener());
}

function interpolate(text: string, vars?: Vars): string {
  if (!vars) return text;
  return text.replace(/\{(\w+)\}/g, (match, name: string) => (name in vars ? String(vars[name] ?? "") : match));
}

/** Translate an English source string; `{name}` placeholders are filled from `vars`. */
export function t(source: string, vars?: Vars): string {
  return interpolate(catalogue[source] || source, vars);
}

/** Plural-aware translation; `{count}` is available as a placeholder. */
export function tn(singular: string, plural: string, count: number, vars?: Vars): string {
  const source = plurals.select(count) === "one" ? singular : plural;
  return interpolate(catalogue[source] || source, { count, ...vars });
}

/** Translate a sentence whose placeholders are React elements, e.g. `{key}` -> <kbd>Esc</kbd>. */
export function tx(source: string, vars: Record<string, ReactNode>): ReactNode {
  const parts = (catalogue[source] || source).split(/(\{\w+\})/);
  return createElement(
    Fragment,
    null,
    ...parts.map((part, i) => {
      const name = /^\{(\w+)\}$/.exec(part)?.[1];
      return createElement(Fragment, { key: i }, name !== undefined && name in vars ? vars[name] : part);
    }),
  );
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Re-render the calling component when the language changes; returns the current language. */
export function useLanguage(): string {
  return useSyncExternalStore(subscribe, getLanguage, getLanguage);
}

/** `const t = useT()` in a component that must refresh on a language switch without other re-renders. */
export function useT(): typeof t {
  useLanguage();
  return t;
}

// Before sign-in, follow the browser when a catalogue exists for its language.
if (typeof navigator !== "undefined" && navigator.language) setLanguage(navigator.language);
