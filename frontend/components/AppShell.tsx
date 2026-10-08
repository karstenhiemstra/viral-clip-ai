"use client";

import { ChartColumn, Film, Flame, LayoutDashboard, ListOrdered, Menu, Scissors, Settings, Users, Wand2, X } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { type ReactNode, useState } from "react";

import { cx } from "@/components/ui";
import { useApi } from "@/lib/api";

const NAV = [
  { href: "/", label: "Dashboard", icon: LayoutDashboard },
  { href: "/creators", label: "Creators", icon: Users },
  { href: "/videos", label: "Videos", icon: Film },
  { href: "/queue", label: "Analysis Queue", icon: ListOrdered, badge: "queue" as const },
  { href: "/clips", label: "Clips", icon: Scissors },
  { href: "/edit", label: "Auto Edit", icon: Wand2 },
  { href: "/analytics", label: "Analytics", icon: ChartColumn },
  { href: "/settings", label: "Settings", icon: Settings },
];

function Logo() {
  return (
    <Link href="/" className="flex items-center gap-2.5 px-2">
      <span className="fire-gradient flex size-8 items-center justify-center rounded-xl shadow-[0_8px_24px_-8px_rgba(255,90,46,0.8)]">
        <Flame className="size-4.5 text-white" strokeWidth={2.5} />
      </span>
      <span className="text-[15px] font-bold tracking-tight">
        ViralClip <span className="fire-text">AI</span>
      </span>
    </Link>
  );
}

function NavLinks({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  const { data } = useApi<{ counts: Record<string, number> }>("/api/jobs?status=active&limit=1", { refreshInterval: 5000 });
  const active = (data?.counts?.running ?? 0) + (data?.counts?.queued ?? 0);
  return (
    <nav className="space-y-0.5">
      {NAV.map(({ href, label, icon: Icon, badge }) => {
        const isActive = href === "/" ? pathname === "/" : pathname.startsWith(href);
        return (
          <Link
            key={href}
            href={href}
            onClick={onNavigate}
            className={cx(
              "group flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition",
              isActive ? "bg-panel-3 text-ink" : "text-muted hover:bg-panel-2 hover:text-ink",
            )}
          >
            <Icon className={cx("size-4", isActive ? "text-fire" : "text-muted group-hover:text-ink-2")} />
            <span className="flex-1">{label}</span>
            {badge === "queue" && active > 0 && (
              <span className="rounded-full bg-fire/15 px-1.5 text-[11px] font-semibold text-fire">{active}</span>
            )}
          </Link>
        );
      })}
    </nav>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="flex min-h-dvh">
      <div className="hidden w-60 shrink-0 border-r border-line bg-panel/60 lg:block">
        <aside className="sticky top-0 flex h-dvh flex-col px-3 py-5">
          <Logo />
          <div className="mt-7 flex-1">
            <NavLinks />
          </div>
          <p className="px-3 text-[11px] leading-relaxed text-muted">
            Scores voorspellen potentie op basis van kenmerken — geen garantie op views.
          </p>
        </aside>
      </div>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-40 flex items-center justify-between border-b border-line bg-bg/85 px-4 py-3 backdrop-blur lg:hidden">
          <Logo />
          <button onClick={() => setOpen(true)} className="rounded-lg p-2 text-ink-2 hover:bg-panel-2" aria-label="Menu openen">
            <Menu className="size-5" />
          </button>
        </header>
        {open && (
          <div className="fixed inset-0 z-50 bg-black/70 lg:hidden" onClick={() => setOpen(false)}>
            <div className="h-full w-72 border-r border-line bg-panel px-3 py-5" onClick={(e) => e.stopPropagation()}>
              <div className="mb-6 flex items-center justify-between">
                <Logo />
                <button onClick={() => setOpen(false)} className="rounded-lg p-2 text-muted hover:bg-panel-2" aria-label="Menu sluiten">
                  <X className="size-5" />
                </button>
              </div>
              <NavLinks onNavigate={() => setOpen(false)} />
            </div>
          </div>
        )}
        <main className="mx-auto w-full max-w-[1400px] flex-1 px-4 py-6 sm:px-6 lg:px-10 lg:py-9">{children}</main>
      </div>
    </div>
  );
}
