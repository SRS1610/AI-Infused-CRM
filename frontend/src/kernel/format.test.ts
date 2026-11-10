import { configureFormat, displayValue, formatMonetary } from "./format";

describe("format", () => {
  beforeAll(() => configureFormat("en_US", "UTC"));

  it("formats values by field type", () => {
    expect(
      displayValue(
        { name: "n", type: "many2one", string: "P", required: false, readonly: false, store: true },
        { id: 1, display_name: "Acme" },
      ),
    ).toBe("Acme");
    expect(
      displayValue(
        {
          name: "s",
          type: "selection",
          string: "S",
          required: false,
          readonly: false,
          store: true,
          selection: [["a", "Alpha"]],
        },
        "a",
      ),
    ).toBe("Alpha");
    expect(
      displayValue({ name: "b", type: "boolean", string: "B", required: false, readonly: false, store: true }, true),
    ).toBe("Yes");
    expect(
      displayValue(
        { name: "d", type: "date", string: "D", required: false, readonly: false, store: true },
        "2026-01-31",
      ),
    ).toBe("Jan 31, 2026");
    expect(
      displayValue(
        { name: "t", type: "many2many", string: "T", required: false, readonly: false, store: false },
        [1, 2],
      ),
    ).toBe("2 records");
    expect(
      displayValue(
        { name: "h", type: "html", string: "H", required: false, readonly: false, store: true },
        "<p>Hi <b>there</b></p>",
      ),
    ).toBe("Hi there");
  });

  it("formats money with the record currency", () => {
    expect(formatMonetary(1234.5, "USD")).toBe("$1,234.50");
    expect(formatMonetary(1234.5, null)).toBe("1,234.50");
    const field = {
      name: "price",
      type: "monetary" as const,
      string: "Price",
      required: false,
      readonly: false,
      store: true,
      currency_field: "currency_id",
    };
    expect(displayValue(field, 12, { currency_id: { id: 2, display_name: "EUR" } })).toBe("€12.00");
  });
});
