import styles from "./BrandLogo.module.css";

type BrandLogoProps = {
  className?: string;
  compact?: boolean;
  light?: boolean;
  priority?: boolean;
};

export function BrandLogo({ className, compact = false, light = false, priority = false }: BrandLogoProps) {
  const src = compact
    ? "/brand/buenapro-mark.svg"
    : light
      ? "/brand/buenapro-logo-light.svg"
      : "/brand/buenapro-logo.svg";

  return (
    <img
      alt="BuenaPro"
      className={[styles.logo, compact ? styles.compact : styles.horizontal, className].filter(Boolean).join(" ")}
      decoding="async"
      fetchPriority={priority ? "high" : "auto"}
      height={compact ? 64 : 72}
      src={src}
      width={compact ? 64 : 292}
    />
  );
}
