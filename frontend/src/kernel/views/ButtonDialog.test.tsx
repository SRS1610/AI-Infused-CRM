import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ButtonDialog, cleanParams, type ButtonParam } from "./ButtonDialog";

const params: ButtonParam[] = [
  { name: "amount", label: "Amount", type: "number" },
  { name: "date", label: "Date", type: "date" },
  { name: "reason", label: "Reason", type: "text", required: true },
  {
    name: "mode",
    label: "Mode",
    type: "select",
    options: [
      ["refund", "Draft"],
      ["cancel", "Full refund"],
    ],
  },
  { name: "notify", label: "Notify", type: "boolean" },
];

describe("cleanParams", () => {
  it("drops empty values and converts numbers", () => {
    expect(cleanParams(params, { amount: "12.5", date: "", reason: "x", mode: "cancel", notify: false })).toEqual({
      amount: 12.5,
      reason: "x",
      mode: "cancel",
      notify: false,
    });
  });
});

describe("ButtonDialog", () => {
  it("asks for parameters and submits them", () => {
    const onSubmit = vi.fn();
    render(
      <ButtonDialog
        node={{ kind: "button", method: "action_reverse", label: "Credit note", params }}
        onSubmit={onSubmit}
        onCancel={() => {}}
      />,
    );
    expect(screen.getByRole("dialog", { name: "Credit note" })).toBeTruthy();
    const confirm = screen.getByRole("button", { name: "Confirm" }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true); // reason is required
    fireEvent.change(screen.getByLabelText(/Reason/), { target: { value: "damaged" } });
    fireEvent.change(screen.getByLabelText("Amount"), { target: { value: "40" } });
    fireEvent.change(screen.getByLabelText("Mode"), { target: { value: "cancel" } });
    fireEvent.click(screen.getByLabelText("Notify"));
    fireEvent.click(screen.getByRole("button", { name: "Confirm" }));
    expect(onSubmit).toHaveBeenCalledWith(
      expect.objectContaining({ amount: 40, reason: "damaged", mode: "cancel", notify: true }),
    );
  });

  it("shows a confirmation and can be cancelled", () => {
    const onCancel = vi.fn();
    render(
      <ButtonDialog
        node={{ kind: "button", method: "button_cancel", label: "Cancel entry", confirm: "Are you sure?" }}
        onSubmit={() => {}}
        onCancel={onCancel}
      />,
    );
    expect(screen.getByText("Are you sure?")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalled();
  });
});
