import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { mockFetch } from "../../test/mockApi";
import { selectionActionsFor } from "../../kernel/extensions";
import { MergeDialog } from "./MergeDialog";

describe("contact merge wizard", () => {
  it("is offered for two or more selected contacts", () => {
    const actions = selectionActionsFor("contacts.partner");
    expect(actions.map((a) => [a.label, a.min])).toEqual([["Merge", 2]]);
  });

  it("picks the survivor and conflicting values, merges and can undo", async () => {
    const bodies: any[] = [];
    const { calls } = mockFetch({
      "POST /api/v1/contacts/merge/preview": () => ({
        records: [
          { id: 1, display_name: "Contoso", name: "Contoso", city: null },
          { id: 2, display_name: "Contoso Ltd", name: "Contoso Ltd", city: "London" },
        ],
        fields: ["name", "city"],
        conflicts: ["name"],
      }),
      "POST /api/v1/contacts/merge": (_u, init) => {
        bodies.push(JSON.parse(String(init.body)));
        return { merge_id: 9, survivor_id: 1 };
      },
      "POST /api/v1/contacts/merge/9/undo": () => ({ undone: true }),
    });
    const done = vi.fn();
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <MergeDialog ids={[1, 2]} onDone={done} />
      </QueryClientProvider>,
    );
    await userEvent.click(await screen.findByLabelText("Use name from #2"));
    await userEvent.click(screen.getByRole("button", { name: "Merge" }));
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toEqual({ ids: [1, 2], survivor_id: 1, field_choices: { name: 2 } });
    await userEvent.click(await screen.findByRole("button", { name: "Undo" }));
    await waitFor(() => expect(done).toHaveBeenCalled());
    expect(calls.some((c) => c.path.endsWith("/undo"))).toBe(true);
  });
});
