import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { mockFetch } from "../../test/mockApi";
import type { FieldMeta } from "../api/types";
import { FieldInput } from "./FieldInput";

const f = (type: FieldMeta["type"], extra: Partial<FieldMeta> = {}): FieldMeta => ({
  name: type,
  type,
  string: type,
  required: false,
  readonly: false,
  store: true,
  ...extra,
});

function Harness({
  field,
  initial,
  readonly = false,
  widget,
}: {
  field: FieldMeta;
  initial: any;
  readonly?: boolean;
  widget?: string;
}) {
  const [value, setValue] = useState(initial);
  return (
    <QueryClientProvider client={new QueryClient()}>
      <FieldInput
        id="f"
        field={field}
        value={value}
        record={{}}
        readonly={readonly}
        node={{ widget }}
        onChange={setValue}
      />
      <output data-testid="value">{JSON.stringify(value)}</output>
    </QueryClientProvider>
  );
}

const value = () => JSON.parse(screen.getByTestId("value").textContent || "null");

describe("field widgets", () => {
  it("edits scalar types", async () => {
    const { unmount } = render(<Harness field={f("integer")} initial={1} />);
    await userEvent.clear(screen.getByRole("spinbutton"));
    await userEvent.type(screen.getByRole("spinbutton"), "42");
    expect(value()).toBe(42);
    unmount();

    render(<Harness field={f("boolean")} initial={false} />);
    await userEvent.click(screen.getByRole("checkbox"));
    expect(value()).toBe(true);
  });

  it("edits selection, date, datetime, text and json", async () => {
    const { unmount } = render(
      <Harness
        field={f("selection", {
          selection: [
            ["a", "Alpha"],
            ["b", "Beta"],
          ],
        })}
        initial={null}
      />,
    );
    await userEvent.selectOptions(screen.getByRole("combobox"), "b");
    expect(value()).toBe("b");
    unmount();
    const d = render(<Harness field={f("date")} initial={null} />);
    fireEvent.change(d.container.querySelector("input")!, { target: { value: "2026-03-04" } });
    expect(value()).toBe("2026-03-04");
    d.unmount();
    const dt = render(<Harness field={f("datetime")} initial={null} />);
    fireEvent.change(dt.container.querySelector("input")!, { target: { value: "2026-03-04T10:30" } });
    expect(value()).toMatch(/^2026-03-04T/);
    dt.unmount();
    const t = render(<Harness field={f("text")} initial="" />);
    await userEvent.type(screen.getByRole("textbox"), "hello");
    expect(value()).toBe("hello");
    t.unmount();
    render(<Harness field={f("json")} initial={{ a: 1 }} />);
    const box = screen.getByRole("textbox");
    fireEvent.change(box, { target: { value: '{"b":2}' } });
    fireEvent.blur(box);
    expect(value()).toEqual({ b: 2 });
  });

  it("renders readonly values, badges and html", () => {
    const first = render(
      <Harness field={f("selection", { selection: [["done", "Done"]] })} initial="done" readonly widget="badge" />,
    );
    expect(screen.getByText("Done")).toHaveClass("badge");
    first.unmount();
    const second = render(<Harness field={f("html")} initial="<p><b>bold</b></p>" readonly />);
    expect(screen.getByText("bold").tagName).toBe("B");
    second.unmount();
    render(<Harness field={f("char")} initial={null} readonly />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("adds and removes many2many tags", async () => {
    mockFetch({
      "GET /api/v1/models/tag": () => ({ items: [{ id: 1, display_name: "Red" }], total: 1, limit: 1, offset: 0 }),
      "GET /api/v1/models/tag/name_search": () => [{ id: 2, display_name: "Blue" }],
    });
    render(<Harness field={f("many2many", { comodel: "tag" })} initial={[1]} />);
    expect(await screen.findByText("Red")).toBeInTheDocument();
    await userEvent.type(screen.getByRole("combobox"), "bl");
    await userEvent.click(await screen.findByRole("option", { name: "Blue" }));
    expect(value()).toEqual([1, 2]);
    await userEvent.click(screen.getByRole("button", { name: "Remove Red" }));
    expect(value()).toEqual([2]);
  });

  it("creates a many2one value from the dropdown", async () => {
    const { calls } = mockFetch({
      "GET /api/v1/models/partner/name_search": () => [],
      "POST /api/v1/models/partner": () => ({ id: 7, display_name: "Newco" }),
    });
    render(<Harness field={f("many2one", { comodel: "partner" })} initial={null} />);
    await userEvent.type(screen.getByRole("combobox"), "Newco");
    await userEvent.click(await screen.findByRole("option", { name: /Create/ }));
    await waitFor(() => expect(value()).toEqual({ id: 7, display_name: "Newco" }));
    expect(calls.find((c) => c.method === "POST")!.body.values).toEqual({ name: "Newco" });
  });

  it("lists one2many rows", async () => {
    mockFetch({
      "GET /api/v1/web/views": () => ({
        model: {},
        fields: { name: f("char", { name: "name", string: "Name" }) },
        views: { list: { columns: [{ field: "name" }] } },
      }),
      "GET /api/v1/models/line": () => ({ items: [{ id: 3, name: "Line A" }], total: 1, limit: 500, offset: 0 }),
    });
    render(<Harness field={f("one2many", { comodel: "line", string: "Lines" })} initial={[3]} />);
    expect(await screen.findByText("Line A")).toBeInTheDocument();
  });
});
