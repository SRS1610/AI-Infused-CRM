// Light / dark / system theme stored per browser.
export type Theme = "light" | "dark" | "system";
const KEY = "tessera.theme";

export function getTheme(): Theme {
  try {
    const v = window.localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

export function applyTheme(theme: Theme = getTheme()): void {
  const root = document.documentElement;
  if (theme === "system") delete root.dataset.theme;
  else root.dataset.theme = theme;
}

export function setTheme(theme: Theme): void {
  try {
    window.localStorage.setItem(KEY, theme);
  } catch {
    /* ignore */
  }
  applyTheme(theme);
}

export function toggleTheme(): Theme {
  const dark =
    document.documentElement.dataset.theme === "dark" ||
    (getTheme() === "system" && window.matchMedia?.("(prefers-color-scheme: dark)").matches);
  const next: Theme = dark ? "light" : "dark";
  setTheme(next);
  return next;
}
