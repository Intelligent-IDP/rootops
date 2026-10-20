/**
 * Shared UI primitives — badges, status, headers, cards, buttons.
 */

import { clsx } from "clsx";


// ── Page header ──────────────────────────────────────────────────

export function PageHeader({
  title,
  subtitle,
  action,
}: {
  title: string;
  subtitle?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3 pb-6 mb-7 border-b border-white/[0.08]">
      <div className="min-w-0">
        <h1 className="text-[24px] font-bold tracking-normal text-text-bright leading-tight">
          {title}
        </h1>
        {subtitle && (
          <p className="text-[12.5px] text-text-dim mt-1.5 leading-relaxed">
            {subtitle}
          </p>
        )}
      </div>
      {action && <div className="shrink-0 ml-6 mt-0.5">{action}</div>}
    </div>
  );
}

// ── Section title ────────────────────────────────────────────────

export function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[10px] font-bold uppercase tracking-[0.13em] text-text-muted mb-3 select-none">
      {children}
    </div>
  );
}

// ── Metric card ──────────────────────────────────────────────────

export function Metric({
  label,
  value,
  icon,
  accent,
}: {
  label: string;
  value: string | number;
  icon?: React.ReactNode;
  accent?: boolean;
}) {
  return (
    <div
      className={clsx(
        "relative rounded-lg px-4 py-4 border transition-colors duration-150 overflow-hidden group",
        accent
          ? "bg-accent/[0.07] border-accent/[0.2] hover:border-accent/35"
          : "bg-bg-card border-white/[0.08] hover:border-white/[0.15]"
      )}
    >
      {accent && (
        <div className="absolute inset-0 bg-gradient-to-br from-accent/[0.07] to-transparent pointer-events-none" />
      )}
      <div className="relative min-w-0">
        <div className="flex items-center gap-1.5 mb-2">
          {icon && (
            <span className={clsx("shrink-0", accent ? "text-accent/60" : "text-text-dim/60")}>
              {icon}
            </span>
          )}
          <div className="text-[9.5px] font-semibold uppercase tracking-[0.1em] text-text-dim/80 truncate">
            {label}
          </div>
        </div>
        <div
          className={clsx(
            "text-[24px] font-bold tracking-tight leading-none truncate",
            accent ? "text-accent" : "text-text-bright"
          )}
        >
          {value}
        </div>
      </div>
    </div>
  );
}

// ── Connection status ────────────────────────────────────────────

export function ConnectionStatus({ ok, extra }: { ok: boolean; extra?: string }) {
  return (
    <div className="flex items-center gap-2.5 px-3.5 py-2.5 bg-white/[0.025] border border-white/[0.06] rounded-xl min-w-0">
      <span
        className={clsx(
          "w-2 h-2 rounded-full shrink-0",
          ok
            ? "bg-success shadow-[0_0_8px_rgba(16,185,129,0.55)]"
            : "bg-error shadow-[0_0_8px_rgba(239,68,68,0.55)]"
        )}
      />
      <span className="text-xs font-medium text-text-muted truncate">
        {ok ? "Connected" : "Offline"}
        {extra && (
          <span className="text-text-dim ml-1.5">· {extra}</span>
        )}
      </span>
    </div>
  );
}

// ── Badges ───────────────────────────────────────────────────────

export function LevelBadge({ level }: { level: string }) {
  // Colours driven by .badge-level[data-level="…"] in globals.css
  return (
    <span
      data-level={level.toUpperCase()}
      className="badge-level inline-flex items-center px-2 py-0.5 rounded-md text-[10px] font-bold tracking-wider border"
    >
      {level.toUpperCase()}
    </span>
  );
}

export function LangBadge({ lang }: { lang: string }) {
  // Colours driven by .badge-lang[data-lang="…"] in globals.css
  return (
    <span
      data-lang={lang.toLowerCase()}
      className="badge-lang inline-flex items-center px-2 py-0.5 rounded-md text-[10px] font-semibold uppercase tracking-wide border"
    >
      {lang}
    </span>
  );
}

const STATUS_CLASSES: Record<string, string> = {
  default: "text-text-muted  bg-white/[0.06]        border-white/[0.1]",
  success: "text-success     bg-success/[0.1]        border-success/25",
  warning: "text-warning     bg-warning/[0.1]        border-warning/25",
  error:   "text-error       bg-error/[0.1]          border-error/25",
};

export function StatusBadge({
  label,
  variant = "default",
}: {
  label: string;
  variant?: "default" | "success" | "warning" | "error";
}) {
  return (
    <span
      className={`inline-flex items-center px-2.5 py-0.5 rounded-md text-[10px] font-bold uppercase tracking-wide border ${STATUS_CLASSES[variant] ?? STATUS_CLASSES.default}`}
    >
      {label}
    </span>
  );
}

// ── Card ─────────────────────────────────────────────────────────

export function Card({
  children,
  className,
  variant = "default",
}: {
  children: React.ReactNode;
  className?: string;
  variant?: "default" | "inset";
}) {
  return (
    <div
      className={clsx(
        "rounded-lg border transition-colors duration-150 overflow-hidden",
        variant === "inset"
          ? "bg-white/[0.02] border-white/[0.07] p-5"
          : "bg-bg-card border-white/[0.08] p-5 hover:border-white/[0.14]",
        className
      )}
    >
      {children}
    </div>
  );
}

// ── Empty state ──────────────────────────────────────────────────

export function EmptyState({
  title,
  description,
  action,
}: {
  title: string;
  description?: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center text-center py-14 px-6">
      <div className="w-11 h-11 rounded-full bg-white/[0.03] border border-white/[0.07] flex items-center justify-center mb-4">
        <div className="w-3 h-3 rounded-full bg-white/[0.08]" />
      </div>
      <div className="text-[13px] font-medium text-text-muted mb-1">{title}</div>
      {description && (
        <p className="text-[12px] text-text-dim max-w-xs leading-relaxed">
          {description}
        </p>
      )}
      {action && <div className="mt-5">{action}</div>}
    </div>
  );
}

// ── Button ───────────────────────────────────────────────────────

export function Button({
  children,
  variant = "primary",
  size = "md",
  disabled,
  className,
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md";
}) {
  return (
    <button
      disabled={disabled}
      className={clsx(
        "inline-flex items-center justify-center gap-2 font-semibold tracking-normal transition-all duration-150 select-none whitespace-nowrap",
        size === "sm"
          ? "px-3.5 py-2 rounded-md text-[11.5px]"
          : "px-5 py-2.5 rounded-lg text-[12.5px]",
        variant === "primary" && [
          "bg-accent text-[#192110]",
          "hover:brightness-105 hover:-translate-y-px",
          "active:translate-y-0 active:brightness-100",
          "disabled:opacity-35 disabled:cursor-not-allowed disabled:hover:translate-y-0 disabled:hover:brightness-100",
        ],
        variant === "secondary" && [
          "bg-white/[0.04] text-text border border-white/[0.09]",
          "hover:bg-white/[0.07] hover:border-white/[0.15] hover:text-text-bright",
          "disabled:opacity-30 disabled:cursor-not-allowed",
        ],
        variant === "ghost" && [
          "text-text-muted hover:text-text hover:bg-white/[0.04]",
          "disabled:opacity-30 disabled:cursor-not-allowed",
        ],
        variant === "danger" && [
          "bg-error/[0.08] text-error border border-error/[0.18]",
          "hover:bg-error/[0.14] hover:border-error/30",
          "disabled:opacity-30 disabled:cursor-not-allowed",
        ],
        className
      )}
      {...props}
    >
      {children}
    </button>
  );
}
