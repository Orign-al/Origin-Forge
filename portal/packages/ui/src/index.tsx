import type {
  ButtonHTMLAttributes,
  HTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
} from "react";
import { clsx } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...values: Array<string | undefined | false>): string {
  return twMerge(clsx(values));
}

export function Button({
  className,
  tone = "neutral",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  tone?: "neutral" | "primary" | "danger";
}): ReactNode {
  return (
    <button
      className={cn("ui-button", `ui-button-${tone}`, className)}
      {...props}
    />
  );
}

export function Badge({
  label,
  tone = "neutral",
}: {
  label: string;
  tone?: "neutral" | "success" | "warning" | "danger" | "info";
}): ReactNode {
  return <span className={cn("ui-badge", `ui-badge-${tone}`)}>{label}</span>;
}

export function Card({
  className,
  ...props
}: HTMLAttributes<HTMLElement>): ReactNode {
  return <section className={cn("ui-card", className)} {...props} />;
}

export function Input(props: InputHTMLAttributes<HTMLInputElement>): ReactNode {
  return <input className={cn("ui-input", props.className)} {...props} />;
}

export function EmptyState({
  title,
  detail,
}: {
  title: string;
  detail?: string;
}): ReactNode {
  return (
    <div className="ui-empty">
      <strong>{title}</strong>
      {detail ? <span>{detail}</span> : null}
    </div>
  );
}

export function StatusBadge({
  value,
}: {
  value: string | null | undefined;
}): ReactNode {
  const normalized = (value ?? "UNKNOWN").toUpperCase();
  const tone = [
    "OK",
    "AVAILABLE",
    "ACTIVE",
    "RUNNING",
    "SUCCEEDED",
    "SET",
    "CLEAR",
  ].includes(normalized)
    ? "success"
    : [
          "DRAIN",
          "IDLE+DRAIN",
          "DEFERRED",
          "PENDING",
          "APPROVAL_PENDING",
          "UNKNOWN",
          "PARTIAL",
        ].includes(normalized)
      ? "warning"
      : ["FAILED", "DENIED", "BLOCKED", "ERROR", "LOCKED", "REJECTED"].includes(
            normalized,
          )
        ? "danger"
        : "info";
  return <Badge label={value ?? "UNKNOWN"} tone={tone} />;
}

export function DataTable({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}): ReactNode {
  return (
    <div className={cn("ui-table-wrap", className)}>
      <table className="ui-table">{children}</table>
    </div>
  );
}
