// Discuss AI: channel/thread summary, "Catch me up", message translation and (through the chatter
// composer) reply drafts. Every AI text carries <AiLabel/> and <Feedback/>; affordances are hidden when
// a feature is turned off and replaced by "AI unavailable" when the gateway is down.
import { useEffect, useRef, useState } from "react";
import { request } from "../../kernel/api/client";
import { ai, aiErrorMessage, type Citation, type RunResult } from "../../kernel/ai/api";
import { AiLabel, AiUnavailable, Feedback, useAiFeature } from "../../kernel/ai/components";
import { registerAiDraft } from "../../kernel/extensions";
import { t, tn, tx } from "../../kernel/i18n";

export const SUMMARY = "discuss.summary";
export const CATCH_UP = "discuss.catch_up";
export const REPLY_DRAFT = "discuss.reply_draft";
export const TRANSLATE = "discuss.translate";

// "Draft with AI" in the channel composer: streamed, labelled, sent through the message proposal.
registerAiDraft("discuss.channel", REPLY_DRAFT);

export type AiLinks = Record<string, { channel_id: number; author?: string; date?: string }>;

/** Translation targets; the codes are sent to the API, the labels follow the UI language. */
export const languages = (): { code: string; label: string }[] => [
  { code: "en", label: t("English") },
  { code: "fr", label: t("French") },
  { code: "de", label: t("German") },
  { code: "es", label: t("Spanish") },
  { code: "it", label: t("Italian") },
  { code: "nl", label: t("Dutch") },
  { code: "pt", label: t("Portuguese") },
];

const languageLabel = (code: string) => languages().find((l) => l.code === code)?.label ?? code;

/** Accessible name of a citation link: who wrote the message and when, when known. */
function openMessageLabel(author?: string, date?: string): string {
  if (author && date) return t("Open message from {author} at {date}", { author, date });
  if (author) return t("Open message from {author}", { author });
  if (date) return t("Open message at {date}", { date });
  return t("Open message");
}

const MARKER = /\s*\[source:([a-z][a-z0-9_]*\.[a-z0-9_.]+):(\d+)\]/g;

/** AI text with its ``[source:chatter.message:N]`` markers turned into "open message" links. */
export function AiText({
  text,
  links,
  onJump,
}: {
  text: string;
  links: AiLinks;
  onJump: (channelId: number, messageId: number) => void;
}) {
  const lines = text.split("\n").filter((l) => l.trim());
  return (
    <ul className="discuss-ai-text">
      {lines.map((line, i) => {
        const refs: { model: string; id: number }[] = [];
        const plain = line
          .replace(MARKER, (_m, model: string, id: string) => {
            refs.push({ model, id: Number(id) });
            return "";
          })
          .replace(/^\s*(?:[-*•]|\d+\.)\s*/, "");
        return (
          <li key={i}>
            {plain}
            {refs
              .filter((r) => r.model === "chatter.message" && links[String(r.id)])
              .map((r) => {
                const link = links[String(r.id)];
                return (
                  <button
                    key={r.id}
                    type="button"
                    className="btn ghost small discuss-cite"
                    aria-label={openMessageLabel(link.author, link.date)}
                    title={openMessageLabel(link.author)}
                    onClick={() => onJump(link.channel_id, r.id)}
                  >
                    ↗
                  </button>
                );
              })}
          </li>
        );
      })}
    </ul>
  );
}

export type SummaryState =
  | { kind: "result"; runId: number; text: string; flags: string[]; links: AiLinks }
  | { kind: "queued"; count: number }
  | { kind: "empty" }
  | { kind: "error"; message: string };

interface SummaryResponse {
  mode: "interactive" | "background" | "empty";
  count: number;
  result?: RunResult;
  links?: AiLinks;
}

export async function requestSummary(channelId: number): Promise<SummaryState> {
  try {
    const res = await request<SummaryResponse>("POST", `/discuss/ai/channels/${channelId}/summary`, { body: {} });
    if (res.mode === "background") return { kind: "queued", count: res.count };
    if (res.mode === "empty" || !res.result) return { kind: "empty" };
    return {
      kind: "result",
      runId: res.result.run_id,
      text: res.result.text,
      flags: res.result.flags,
      links: res.links ?? {},
    };
  } catch (e) {
    return { kind: "error", message: aiErrorMessage(e) };
  }
}

/** A summary made in the background, opened from its notification (``?summary=<run id>``). */
export async function loadSummaryRun(runId: number, channelId: number): Promise<SummaryState> {
  try {
    const run = await request<{ output_text: string | null; safety_flags: string[] | null; citations: Citation[] }>(
      "GET",
      `/ai/runs/${runId}`,
    );
    const links: AiLinks = {};
    for (const c of run.citations)
      if (c.res_model === "chatter.message") links[String(c.res_id)] = { channel_id: channelId };
    return { kind: "result", runId, text: run.output_text ?? "", flags: run.safety_flags ?? [], links };
  } catch (e) {
    return { kind: "error", message: aiErrorMessage(e) };
  }
}

export function SummaryButton({ busy, onClick }: { busy: boolean; onClick: () => void }) {
  const feature = useAiFeature(SUMMARY);
  if (!feature.enabled) return null;
  if (!feature.available) return <AiUnavailable />;
  return (
    <button type="button" className="btn small ghost" disabled={busy} onClick={onClick}>
      {busy ? t("Summarising…") : `✦ ${t("Summarize")}`}
    </button>
  );
}

export function SummaryPanel({
  state,
  onClose,
  onJump,
}: {
  state: SummaryState;
  onClose: () => void;
  onJump: (channelId: number, messageId: number) => void;
}) {
  return (
    <section className="discuss-ai-panel" aria-label={t("AI summary")}>
      <header>
        <strong>{t("Summary")}</strong>
        {state.kind === "result" && <AiLabel text={t("AI-generated")} />}
        <span className="spacer" />
        <button type="button" className="btn small ghost" onClick={onClose}>
          {t("Close")}
        </button>
      </header>
      {state.kind === "queued" && (
        <p className="muted" role="status">
          {tn(
            "{count} message: the summary is being prepared in the background. You will get a notification with a link when it is ready.",
            "{count} messages: the summary is being prepared in the background. You will get a notification with a link when it is ready.",
            state.count,
          )}
        </p>
      )}
      {state.kind === "empty" && <p className="muted">{t("There are no messages to summarise.")}</p>}
      {state.kind === "error" && (
        <div className="alert error" role="alert">
          {state.message}
        </div>
      )}
      {state.kind === "result" && (
        <>
          <AiText text={state.text} links={state.links} onJump={onJump} />
          {state.flags.includes("fabricated_citation") && (
            <p className="muted small">{t("Some references could not be verified and were removed.")}</p>
          )}
          <Feedback runId={state.runId} />
        </>
      )}
    </section>
  );
}

interface CatchUpResponse {
  unread: number;
  result: RunResult | null;
  links: AiLinks;
}

/** Sidebar entry: "Catch me up" (hidden when the feature is off, "AI unavailable" when the gateway is down). */
export function CatchUpButton({ onClick }: { onClick: () => void }) {
  const feature = useAiFeature(CATCH_UP);
  if (!feature.enabled) return null;
  if (!feature.available) return <AiUnavailable />;
  return (
    <button type="button" className="btn small ghost discuss-catch-up" onClick={onClick}>
      ✦ {t("Catch me up")}
    </button>
  );
}

export function CatchUpPanel({
  onClose,
  onJump,
  load = () => request<CatchUpResponse>("POST", "/discuss/ai/catch-up", { body: {} }),
}: {
  onClose: () => void;
  onJump: (channelId: number, messageId: number) => void;
  load?: () => Promise<CatchUpResponse>;
}) {
  const [state, setState] = useState<
    { kind: "loading" } | { kind: "done"; data: CatchUpResponse } | { kind: "error"; message: string }
  >({ kind: "loading" });
  const loader = useRef(load);
  useEffect(() => {
    let live = true;
    loader.current().then(
      (data) => live && setState({ kind: "done", data }),
      (e: unknown) => live && setState({ kind: "error", message: aiErrorMessage(e) }),
    );
    return () => {
      live = false;
    };
  }, []);
  const result = state.kind === "done" ? state.data.result : null;
  return (
    <section className="discuss-ai-panel" aria-label={t("Catch me up")}>
      <header>
        <h2>{t("Catch me up")}</h2>
        {result && <AiLabel text={t("AI-generated")} />}
        <span className="spacer" />
        <button type="button" className="btn small ghost" onClick={onClose}>
          {t("Close")}
        </button>
      </header>
      {state.kind === "loading" && (
        <p className="muted" role="status">
          {t("Reading your unread messages…")}
        </p>
      )}
      {state.kind === "error" && (
        <div className="alert error" role="alert">
          {state.message}
        </div>
      )}
      {state.kind === "done" && !result && <p className="muted">{t("You're all caught up.")}</p>}
      {result && (
        <>
          <p className="muted small">
            {tn(
              "{count} unread message, most important first.",
              "{count} unread messages, most important first.",
              state.kind === "done" ? state.data.unread : 0,
            )}
          </p>
          <AiText text={result.text} links={state.kind === "done" ? state.data.links : {}} onJump={onJump} />
          <Feedback runId={result.run_id} />
        </>
      )}
    </section>
  );
}

/** Per-message translation into ``lang``; the translated text is labelled until the person moves on. */
export function TranslateControl({ messageId, lang }: { messageId: number; lang: string }) {
  const feature = useAiFeature(TRANSLATE);
  const [busy, setBusy] = useState(false);
  const [out, setOut] = useState<{ text: string; runId: number; lang: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  if (!feature.enabled || !feature.available) return null; // the conversation header shows "AI unavailable"
  const translate = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await ai.run(TRANSLATE, { inputs: { message_id: messageId, target_lang: lang } });
      setOut({ text: res.text, runId: res.run_id, lang });
    } catch (e) {
      setError(aiErrorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="discuss-translate">
      {(!out || out.lang !== lang) && (
        <button
          type="button"
          className="btn ghost small"
          disabled={busy}
          onClick={() => void translate()}
          aria-label={t("Translate message to {language}", { language: languageLabel(lang) })}
        >
          {busy ? t("Translating…") : `✦ ${t("Translate")}`}
        </button>
      )}
      {out && (
        <div className="discuss-translation" lang={out.lang}>
          <AiLabel text={t("AI translation ({language})", { language: languageLabel(out.lang) })} />
          <p>{out.text}</p>
          <Feedback runId={out.runId} />
          <button type="button" className="btn ghost small" onClick={() => setOut(null)}>
            {t("Hide translation")}
          </button>
        </div>
      )}
      {error && (
        <span className="alert error small" role="alert">
          {error}
        </span>
      )}
    </div>
  );
}

export function TranslateLanguage({ lang, onChange }: { lang: string; onChange: (lang: string) => void }) {
  const feature = useAiFeature(TRANSLATE);
  if (!feature.enabled) return null;
  if (!feature.available) return <AiUnavailable />;
  return (
    <label className="small">
      {tx("Translate to {language}", {
        language: (
          <select className="input small" value={lang} onChange={(e) => onChange(e.target.value)}>
            {languages().map((l) => (
              <option key={l.code} value={l.code}>
                {l.label}
              </option>
            ))}
          </select>
        ),
      })}
    </label>
  );
}
