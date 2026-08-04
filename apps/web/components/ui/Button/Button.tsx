import styles from "./Button.module.css";

type ButtonProps = React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "accent" | "secondary" | "ghost" | "danger";
  size?: "compact" | "normal";
};

export function Button({ className, variant = "primary", size = "normal", ...props }: ButtonProps) {
  return <button className={[styles.button, styles[variant], styles[size], className].filter(Boolean).join(" ")} {...props} />;
}
