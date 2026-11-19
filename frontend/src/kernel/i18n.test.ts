import { act, renderHook } from "@testing-library/react";
import { isValidElement } from "react";
import { resolve } from "node:path";
import { extractFromSource, placeholders, scanSources } from "../../scripts/i18n-extract.mjs";
import fr from "../i18n/fr_FR.json";
import { availableLanguages, getLanguage, resolveLanguage, setLanguage, t, tn, tx, useT } from "./i18n";

const catalogue: Record<string, string> = fr;

describe("i18n runtime", () => {
  afterEach(() => setLanguage("en_US"));

  it("returns the English source and fills placeholders by default", () => {
    expect(getLanguage()).toBe("en_US");
    expect(t("Save")).toBe("Save");
    expect(t("Welcome, {name}", { name: "Ada" })).toBe("Welcome, Ada");
    expect(t("{a} and {b}", { a: 1 })).toBe("1 and {b}"); // unknown placeholders stay visible
  });

  it("translates, interpolates and falls back to English for unknown strings", () => {
    setLanguage("fr_FR");
    expect(getLanguage()).toBe("fr_FR");
    expect(document.documentElement.lang).toBe("fr");
    expect(t("Save")).toBe("Enregistrer");
    expect(t("Welcome, {name}", { name: "Ada" })).toBe("Bienvenue, Ada");
    expect(t("A string nobody translated")).toBe("A string nobody translated");
  });

  it("chooses plural forms with the language's rules", () => {
    expect(tn("{count} record", "{count} records", 1)).toBe("1 record");
    expect(tn("{count} record", "{count} records", 0)).toBe("0 records");
    setLanguage("fr_FR");
    expect(tn("{count} record", "{count} records", 0)).toBe("0 enregistrement"); // French: 0 is singular
    expect(tn("{count} record", "{count} records", 2)).toBe("2 enregistrements");
  });

  it("resolves regional variants and unknown languages", () => {
    expect(resolveLanguage("fr-BE")).toBe("fr_FR");
    expect(resolveLanguage("fr")).toBe("fr_FR");
    expect(resolveLanguage("de_DE")).toBe("en_US");
    expect(resolveLanguage(undefined)).toBe("en_US");
    expect(availableLanguages()).toEqual(expect.arrayContaining(["en_US", "fr_FR"]));
  });

  it("re-renders useT() consumers on a language switch", () => {
    const { result, unmount } = renderHook(() => useT()("Save"));
    expect(result.current).toBe("Save");
    act(() => setLanguage("fr_FR"));
    expect(result.current).toBe("Enregistrer");
    unmount();
  });

  it("interpolates React elements with tx()", () => {
    setLanguage("fr_FR");
    const node = tx("Editing row — {enter} to save, {escape} to cancel", { enter: "⏎", escape: "⎋" });
    expect(isValidElement(node)).toBe(true);
  });
});

describe("extraction", () => {
  it("finds literal t/tn/tx calls and flags dynamic ones", () => {
    const code = [
      't("Save")',
      "t('It\\'s {n}', { n })",
      'tn(\n  "{count} row",\n  "{count} rows",\n  n,\n)',
      'tx("Press {key}", { key })',
      "t(label)",
      'format("x")',
      'obj.t("not a call of ours")',
    ].join("\n");
    const found = extractFromSource(code);
    expect(found.keys).toEqual(["Save", "It's {n}", "Press {key}", "{count} row", "{count} rows"]);
    expect(found.dynamic).toEqual([9]);
  });
});

describe("fr_FR catalogue", () => {
  const { keys, dynamic } = scanSources(resolve(__dirname, ".."));

  it("has only extractable translation calls", () => {
    expect(dynamic).toEqual([]);
    expect(keys.size).toBeGreaterThan(300);
  });

  it("covers at least 95% of the strings", () => {
    const translated = [...keys.keys()].filter((k) => catalogue[k]);
    const missing = [...keys.keys()].filter((k) => !catalogue[k]);
    expect(translated.length / keys.size, `untranslated: ${missing.join(" | ")}`).toBeGreaterThanOrEqual(0.95);
  });

  it("keeps every placeholder in translations", () => {
    const broken = Object.entries(catalogue).filter(
      ([source, translation]) => translation && placeholders(source).join() !== placeholders(translation).join(),
    );
    expect(broken).toEqual([]);
  });
});
