import styles from "./Button.module.css";

type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "accent" | "secondary" | "ghost" | "danger";
  size?: "compact" | "normal";
};

/** Button styling for elements that must stay links (navigation), so no button is nested in an anchor. */
export function buttonClassName(variant: NonNullable<ButtonProps["variant"]> = "primary", size: NonNullable<ButtonProps["size"]> = "normal", className?: string) {
  return [styles.button, styles[variant], styles[size], className].filter(Boolean).join(" ");
}

export function Button({ className, variant = "primary", size = "normal", ...props }: ButtonProps) {
  return <button className={buttonClassName(variant, size, className)} {...props} />;
}
