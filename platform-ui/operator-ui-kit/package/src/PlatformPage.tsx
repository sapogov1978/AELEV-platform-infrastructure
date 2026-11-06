import type { ReactNode } from "react";

export function PlatformPage({ title, description, kicker, header, introActions, afterContent, layout = "standard", className, introClassName, contentClassName, children }: {
  title: ReactNode;
  description: ReactNode;
  kicker?: ReactNode;
  header?: ReactNode;
  introActions?: ReactNode;
  afterContent?: ReactNode;
  layout?: "standard" | "filter";
  className?: string;
  introClassName?: string;
  contentClassName?: string;
  children: ReactNode;
}) {
  const rootClassName = ["app-shell", layout === "filter" && "filter-app", "aelev-owner-ui", className].filter(Boolean).join(" ");
  const pageIntroClassName = ["page-intro", introClassName].filter(Boolean).join(" ");
  const bodyClassName = [layout === "filter" && "filter-content", contentClassName].filter(Boolean).join(" ") || undefined;
  return <main className={rootClassName}>
    {header}
    <section className={pageIntroClassName}>
      <div>{kicker && <span className="section-kicker">{kicker}</span>}<h1>{title}</h1><p>{description}</p></div>
      {introActions}
    </section>
    <div className={bodyClassName}>{children}</div>
    {afterContent}
  </main>;
}
