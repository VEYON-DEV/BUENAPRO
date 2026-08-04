import styles from "./GlassSurface.module.css";

export function GlassSurface({
  children,
  className,
  tone = "ambient",
  padding = "medium",
}: {
  children: React.ReactNode;
  className?: string;
  tone?: "ambient" | "strong" | "work";
  padding?: "none" | "small" | "medium" | "large";
}) {
  return (
    <section className={[styles.surface, styles[tone], styles[padding], className].filter(Boolean).join(" ")}>
      {children}
    </section>
  );
}
