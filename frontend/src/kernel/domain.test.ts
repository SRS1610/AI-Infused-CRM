import { and, bindRecord, evaluate, isEmpty, or, textSearch } from "./domain";

describe("domain helpers", () => {
  it("combines and simplifies", () => {
    expect(and([], null, [["a", "=", 1]])).toEqual(["a", "=", 1]);
    expect(and([["a", "=", 1]], [["b", "=", 2]])).toEqual({
      all: [
        ["a", "=", 1],
        ["b", "=", 2],
      ],
    });
    expect(and({ all: [["a", "=", 1]] }, ["c", "=", 3])).toEqual({
      all: [
        ["a", "=", 1],
        ["c", "=", 3],
      ],
    });
    expect(or([["a", "=", 1]], [["b", "=", 2]])).toEqual({
      any: [
        ["a", "=", 1],
        ["b", "=", 2],
      ],
    });
    expect(or()).toEqual([]);
    expect(isEmpty({ all: [] })).toBe(true);
    expect(textSearch(["name", "email"], " ann ")).toEqual({
      any: [
        ["name", "ilike", "ann"],
        ["email", "ilike", "ann"],
      ],
    });
    expect(textSearch(["name"], "  ")).toEqual([]);
  });

  it("binds record values into $placeholders", () => {
    expect(bindRecord([["country_id", "=", "$country_id"]], { country_id: { id: 5, display_name: "FR" } })).toEqual([
      ["country_id", "=", 5],
    ]);
    expect(bindRecord({ all: [["x", "=", "$y"]] }, { y: 3 })).toEqual({ all: [["x", "=", 3]] });
  });

  it("evaluates view modifiers", () => {
    const rec = { state: "draft", amount: 10, partner_id: { id: 2, display_name: "A" }, tags: [] };
    expect(evaluate([["state", "=", "draft"]], rec)).toBe(true);
    expect(
      evaluate(
        {
          any: [
            ["state", "=", "done"],
            ["amount", ">", 5],
          ],
        },
        rec,
      ),
    ).toBe(true);
    expect(evaluate({ not: ["partner_id", "=", 2] }, rec)).toBe(false);
    expect(evaluate([["tags", "is_not_set"]], rec)).toBe(true);
    expect(evaluate([["state", "in", ["a", "b"]]], rec)).toBe(false);
    expect(evaluate(undefined, rec)).toBe(false);
  });
});
