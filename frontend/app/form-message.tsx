import type { CSSProperties, ReactNode } from "react";

// The message line under a form or button. It is always mounted, even when
// empty: a live region that appears together with its text is often not
// announced by screen readers. While empty, `.form-message:empty` in
// globals.css takes it out of the layout.
export function FormMessage({
  text,
  kind = "error",
  className = "",
  style,
}: {
  text?: ReactNode;
  kind?: "error" | "success";
  className?: string;
  style?: CSSProperties;
}) {
  return (
    <p aria-live="polite" className={`form-message ${kind} ${className}`.trim()} style={style}>
      {text}
    </p>
  );
}
