import { Link } from "react-router-dom";
import { ApiError } from "../api/client";
import { Icon } from "../icons";
import { t } from "../i18n";

/** True for errors the user cannot fix by retrying: their role does not include the page. */
export function isAccessError(error: unknown): boolean {
  return error instanceof ApiError && error.status === 403;
}

/** Shown instead of a page the user's role does not include (no model or record names). */
export function AccessDenied() {
  return (
    <div className="access-denied" role="alert">
      <span className="app-glyph" aria-hidden="true">
        <Icon name="lock" size={22} />
      </span>
      <h1>{t("You don't have access to this page")}</h1>
      <p>{t("Your role does not include it. Ask an administrator if you need it.")}</p>
      <Link to="/" className="btn primary">
        <Icon name="home" />
        {t("Go to Home")}
      </Link>
    </div>
  );
}
