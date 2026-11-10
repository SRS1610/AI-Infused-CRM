import { useQuery } from "@tanstack/react-query";
import { useNavigate, useParams } from "react-router-dom";
import { api } from "../api/client";
import { FormView } from "./FormView";

/** /m/:model/:id — open any record directly (notifications, search results, links in emails). */
export function RecordView() {
  const { model = "", id = "" } = useParams();
  const navigate = useNavigate();
  const views = useQuery({ queryKey: ["views", model, "form"], queryFn: () => api.views(model, ["form"]) });
  if (views.isLoading)
    return (
      <div className="loading" role="status">
        Loading…
      </div>
    );
  if (views.isError || !views.data?.views.form)
    return (
      <div className="alert error" role="alert">
        {(views.error as Error | undefined)?.message ?? "Cannot open this record"}
      </div>
    );
  return (
    <FormView
      model={model}
      recordId={Number(id)}
      meta={views.data}
      view={views.data.views.form}
      onSaved={() => undefined}
      onBack={() => navigate(-1)}
    />
  );
}
