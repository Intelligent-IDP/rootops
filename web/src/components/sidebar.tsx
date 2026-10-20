"use client";

import { useState, useEffect } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  LayoutDashboard,
  Brain,
  GitPullRequest,
  FileText,
  Zap,
  Users,
  Settings,
  Hexagon,
  Network,
  Menu,
  X,
} from "lucide-react";
import { clsx } from "clsx";

const NAV_SECTIONS = [
  {
    label: "Core",
    items: [
      { href: "/",             label: "Dashboard",    icon: LayoutDashboard },
      { href: "/intelligence", label: "Intelligence", icon: Brain },
      { href: "/graph",        label: "Dependency Graph", icon: Network },
      { href: "/pr-review",    label: "PR Review",    icon: GitPullRequest },
    ],
  },
  {
    label: "Tools",
    items: [
      { href: "/logs",         label: "Log Ingest",   icon: FileText },
      { href: "/auto-heal",    label: "Auto-Heal",    icon: Zap },
      { href: "/dev-profiles", label: "Dev Profiles", icon: Users },
    ],
  },
  {
    label: "System",
    items: [
      { href: "/settings", label: "Settings", icon: Settings },
    ],
  },
] as const;

export function Sidebar() {
  return (
    <>
      {/* Mobile: top bar + drawer */}
      <MobileNav />

      {/* Desktop: fixed sidebar */}
      <aside className="hidden lg:flex w-[248px] shrink-0 flex-col border-r border-white/[0.08] bg-[#0B100D] sticky top-0 h-screen">
        <NavContent />
      </aside>
    </>
  );
}

/* ── Shared nav content used by both desktop sidebar and mobile drawer ── */
function NavContent({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();

  return (
    <>
      {/* Brand */}
      <div className="relative px-6 pt-7 pb-6">
        <div className="flex items-center gap-3">
          <div className="relative w-9 h-9 rounded-[10px] bg-accent flex items-center justify-center shrink-0">
            <Hexagon size={17} className="text-[#182010]" strokeWidth={2.5} />
          </div>
          <div>
            <div className="text-[15px] font-extrabold tracking-tight text-text-bright leading-none">
              RootOps
            </div>
            <div className="text-[9px] font-semibold uppercase tracking-[0.12em] text-text-dim leading-none mt-2">
              Engineering intelligence
            </div>
          </div>
        </div>
      </div>

      <div className="h-px bg-white/[0.05] mx-4" />

      {/* Navigation */}
      <nav className="relative flex-1 px-4 py-6 space-y-7 overflow-y-auto">
        {NAV_SECTIONS.map((section) => (
          <div key={section.label}>
            <div className="px-3 mb-2.5 text-[9px] font-bold tracking-[0.14em] uppercase text-text-dim select-none">
              {section.label}
            </div>
            <div className="space-y-px">
              {section.items.map(({ href, label, icon: Icon }) => {
                const active =
                  href === "/" ? pathname === "/" : pathname.startsWith(href);
                return (
                  <Link
                    key={href}
                    href={href}
                    onClick={onNavigate}
                    className={clsx(
                      "relative flex items-center gap-3 px-3 py-2.5 rounded-lg text-[12px] font-semibold transition-colors duration-150",
                      active
                        ? "bg-accent/[0.11] text-accent"
                        : "text-text-muted hover:text-text-bright hover:bg-white/[0.045]"
                    )}
                  >
                    {active && (
                      <span className="absolute left-0 top-1/2 -translate-y-1/2 w-[3px] h-5 bg-accent rounded-r-full" />
                    )}
                    <Icon
                      size={16}
                      strokeWidth={active ? 2.2 : 1.8}
                      className="shrink-0"
                    />
                    {label}
                  </Link>
                );
              })}
            </div>
          </div>
        ))}
      </nav>

      <div className="h-px bg-white/[0.05] mx-4" />

      {/* Footer */}
      <div className="px-5 py-4 border-t border-white/[0.06]">
        <div className="text-[10px] font-mono text-text-dim truncate">
          {process.env.NEXT_PUBLIC_API_URL || "localhost:8000"}
        </div>
      </div>
    </>
  );
}

/* ── Mobile top bar + slide-out drawer ─────────────────────────── */
function MobileNav() {
  const [open, setOpen] = useState(false);
  const pathname = usePathname();

  // Close drawer on route change
  useEffect(() => {
    setOpen(false);
  }, [pathname]);

  // Prevent body scroll when drawer is open
  useEffect(() => {
    document.body.style.overflow = open ? "hidden" : "";
    return () => { document.body.style.overflow = ""; };
  }, [open]);

  return (
    <>
      {/* Top bar — visible only on mobile */}
      <header className="lg:hidden sticky top-0 z-40 flex items-center justify-between px-4 h-14 border-b border-white/[0.08] bg-[#0B100D]/95 backdrop-blur-md">
        <div className="flex items-center gap-2.5">
          <div className="w-7 h-7 rounded-[8px] bg-accent flex items-center justify-center">
            <Hexagon size={13} className="text-[#182010]" strokeWidth={2.5} />
          </div>
          <span className="text-[13px] font-bold tracking-tight text-text-bright">
            RootOps
          </span>
        </div>
        <button
          onClick={() => setOpen(!open)}
          className="p-2 -mr-2 rounded-lg text-text-dim hover:text-text-bright hover:bg-white/[0.05] transition-colors"
          aria-label={open ? "Close menu" : "Open menu"}
        >
          {open ? <X size={20} /> : <Menu size={20} />}
        </button>
      </header>

      {/* Backdrop */}
      {open && (
        <div
          className="lg:hidden fixed inset-0 z-40 bg-black/60 backdrop-blur-sm"
          onClick={() => setOpen(false)}
        />
      )}

      {/* Slide-out drawer */}
      <aside
        className={clsx(
          "lg:hidden fixed top-0 left-0 z-50 h-full w-[280px] flex flex-col bg-[#0B100D] border-r border-white/[0.08] transition-transform duration-200 ease-out",
          open ? "translate-x-0" : "-translate-x-full"
        )}
      >
        {/* Close button inside drawer */}
        <div className="absolute top-4 right-3">
          <button
            onClick={() => setOpen(false)}
            className="p-1.5 rounded-lg text-text-dim hover:text-text-bright hover:bg-white/[0.05] transition-colors"
            aria-label="Close menu"
          >
            <X size={16} />
          </button>
        </div>

        <NavContent onNavigate={() => setOpen(false)} />
      </aside>
    </>
  );
}
