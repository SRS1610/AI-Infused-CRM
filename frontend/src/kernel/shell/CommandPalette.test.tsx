import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CommandPalette, flattenMenus, fuzzyScore } from "./CommandPalette";
import { matches } from "../hotkeys";

describe("command palette", () => {
  const menus = [
    {
      id: 1,
      xmlid: null,
      name: "Settings",
      sequence: 1,
      icon: null,
      action: null,
      action_id: null,
      children: [
        {
          id: 2,
          xmlid: null,
          name: "Users",
          sequence: 1,
          icon: null,
          action: "web.action_users",
          action_id: 9,
          children: [],
        },
        {
          id: 3,
          xmlid: null,
          name: "Companies",
          sequence: 2,
          icon: null,
          action: "web.action_companies",
          action_id: 10,
          children: [],
        },
      ],
    },
  ];

  it("flattens menus and ranks matches", () => {
    const open = vi.fn();
    const items = flattenMenus(menus, open);
    expect(items.map((i) => i.label)).toEqual(["Users", "Companies"]);
    expect(items[0].hint).toBe("Settings");
    expect(fuzzyScore("usr", "Users")).toBeGreaterThan(0);
    expect(fuzzyScore("zz", "Users")).toBe(0);
    items[1].run();
    expect(open).toHaveBeenCalledWith("web.action_companies");
  });

  it("filters and runs with the keyboard", async () => {
    const open = vi.fn();
    const onClose = vi.fn();
    render(<CommandPalette items={flattenMenus(menus, open)} onClose={onClose} />);
    const input = screen.getByRole("combobox", { name: "Go to menu or record" });
    expect(input).toHaveFocus();
    await userEvent.type(input, "comp");
    expect(screen.getAllByRole("option")).toHaveLength(1);
    await userEvent.keyboard("{Enter}");
    expect(open).toHaveBeenCalledWith("web.action_companies");
    expect(onClose).toHaveBeenCalled();
  });

  it("matches shortcuts", () => {
    const ev = new KeyboardEvent("keydown", { key: "k", ctrlKey: true });
    expect(matches(ev, "mod+k")).toBe(true);
    expect(matches(new KeyboardEvent("keydown", { key: "s", ctrlKey: true, shiftKey: true }), "mod+s")).toBe(false);
  });
});
