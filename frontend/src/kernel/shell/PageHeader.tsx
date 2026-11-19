import type { ReactNode } from "react";

/** The white band at the top of a page that is not a generic list view (reports, dashboards, tools):
 *  title and actions on the first row, the page's own controls (filters, period…) on the command row. */
export function PageHeader({
  title,
  subtitle,
  actions,
  children,
  titleId,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
  titleId?: string;
}) {
  return (
    <div className="page-head">
      <div className="cp-row cp-title-row">
        <div className="page-title-block">
          <h1 className="page-title" id={titleId}>
            {title}
          </h1>
          {subtitle && <div className="page-subtitle">{subtitle}</div>}
        </div>
        {actions && <div className="page-actions">{actions}</div>}
      </div>
      {children && <div className="cp-row command-bar">{children}</div>}
    </div>
  );
}
