import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { NotificationItem } from "../api/platform";
import { useRealtime } from "../realtime";
import { useSession } from "../session";
import { t } from "../i18n";

/** Unread notifications, live over the user's realtime channel. */
export function NotificationBell() {
  const { session } = useSession();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const list = useQuery({
    queryKey: ["notifications"],
    queryFn: () =>
      api.list("core.notification", {
        domain: [["is_read", "=", false]],
        fields: ["title", "body", "kind", "model", "res_id", "is_read", "create_date"],
        order: "id desc",
        limit: 20,
        count: true,
      }),
    enabled: !!session,
  });
  useRealtime(session ? `user:${session.user.id}` : null, (event) => {
    if (event.event !== "notification") return;
    setToast(String(event.data.title ?? t("New notification")));
    window.setTimeout(() => setToast(null), 5000);
    void list.refetch();
  });
  const items = (list.data?.items ?? []) as unknown as NotificationItem[];
  const unread = list.data?.total ?? items.length;
  const markRead = async (ids: number[]) => {
    for (const id of ids) await api.call("core.notification", id, "action_mark_read");
    await list.refetch();
  };
  return (
    <div className="dropdown">
      <button
        type="button"
        className="btn ghost small bell"
        aria-label={t("Notifications: {count} unread", { count: unread })}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
      >
        🔔{unread > 0 && <span className="badge count">{unread > 99 ? "99+" : unread}</span>}
      </button>
      {open && (
        <div className="dropdown-menu right notification-panel" role="dialog" aria-label={t("Notifications")}>
          <div className="panel-head">
            <strong>{t("Notifications")}</strong>
            {items.length > 0 && (
              <button type="button" className="btn small ghost" onClick={() => void markRead(items.map((n) => n.id))}>
                {t("Mark all read")}
              </button>
            )}
          </div>
          {items.length === 0 && <p className="muted">{t("You're all caught up.")}</p>}
          <ul>
            {items.map((n) => (
              <li key={n.id}>
                <button
                  type="button"
                  className="btn ghost block notification"
                  onClick={async () => {
                    await markRead([n.id]);
                    setOpen(false);
                    if (n.model && n.res_id) navigate(`/m/${n.model}/${n.res_id}`);
                  }}
                >
                  <strong>{n.title}</strong>
                  {n.body && <span className="muted small">{n.body}</span>}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}
      {toast && (
        <div className="toast" role="status">
          {toast}
        </div>
      )}
    </div>
  );
}
