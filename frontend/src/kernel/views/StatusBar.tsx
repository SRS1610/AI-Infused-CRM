import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError } from "../api/client";
import { workflow } from "../api/platform";
import { t } from "../i18n";

/** Workflow states with the transitions the user can take now (guards evaluated server-side). */
export function StatusBar({ model, resId, onChanged }: { model: string; resId: number; onChanged: () => void }) {
  const queryClient = useQueryClient();
  const bar = useQuery({ queryKey: ["statusbar", model, resId], queryFn: () => workflow.statusbar(model, resId) });
  const [error, setError] = useState<string | null>(null);
  if (!bar.data?.workflow) return null;
  const visible = bar.data.states.filter((s) => !s.fold || s.current);
  return (
    <div className="statusbar" aria-label={t("{name} workflow", { name: bar.data.workflow.name })}>
      <div className="statusbar-actions">
        {bar.data.transitions.map((tr) => (
          <button
            key={tr.id}
            type="button"
            className={`btn small${tr.highlight ? " primary" : ""}`}
            onClick={async () => {
              if (tr.confirm && !window.confirm(tr.confirm)) return;
              setError(null);
              try {
                await workflow.transition(model, resId, tr.id);
                await queryClient.invalidateQueries({ queryKey: ["statusbar", model, resId] });
                onChanged();
              } catch (e) {
                setError(e instanceof ApiError ? e.message : String(e));
                void bar.refetch();
              }
            }}
          >
            {tr.name}
          </button>
        ))}
      </div>
      <ol className="statusbar-states">
        {visible.map((s) => (
          <li key={s.key} className={s.current ? "current" : undefined} aria-current={s.current ? "step" : undefined}>
            {s.name}
          </li>
        ))}
      </ol>
      {error && (
        <div className="alert conflict" role="alert">
          {error}
        </div>
      )}
    </div>
  );
}
