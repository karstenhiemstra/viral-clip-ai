"use client";

import { ArrowRight, Flame, Hourglass, KeyRound, ListOrdered, Sparkles, TriangleAlert, Upload, Users } from "lucide-react";
import Link from "next/link";

import { ClipCard, ScoreBadge } from "@/components/clips";
import { Card, CardHeader, EmptyState, LinkButton, PageHeader, ProgressBar, Spinner } from "@/components/ui";
import { useApi } from "@/lib/api";
import { formatDuration, money } from "@/lib/format";
import type { Dashboard } from "@/lib/types";

function Stat({ label, value, hint, accent }: { label: string; value: React.ReactNode; hint?: React.ReactNode; accent?: boolean }) {
  return (
    <Card className={accent ? "relative overflow-hidden border-fire/30" : ""}>
      {accent && <div className="fire-gradient absolute -top-16 -right-16 size-40 rounded-full opacity-20 blur-3xl" />}
      <div className="relative p-5">
        <p className="text-xs font-medium text-muted">{label}</p>
        <p className={accent ? "fire-text mt-2 text-4xl font-extrabold tabular-nums" : "mt-2 text-3xl font-bold tabular-nums text-ink"}>{value}</p>
        {hint && <p className="mt-1 text-xs text-muted">{hint}</p>}
      </div>
    </Card>
  );
}

function Onboarding() {
  const steps = [
    { icon: KeyRound, title: "API keys instellen", text: "YouTube Data API (discovery) en OpenAI of Anthropic (AI-scoring). Zonder AI-key werkt een lokale heuristiek.", href: "/settings", cta: "Naar Settings" },
    { icon: Users, title: "Creators toevoegen", text: "Zoek bijvoorbeeld Enzo Knol, Bankzitters of StukTV. Nieuwe uploads worden elke 2 uur gecontroleerd.", href: "/creators", cta: "Creators" },
    { icon: Upload, title: "Bronvideo aanleveren", text: "Upload het videobestand (bijv. via het clipping-programma van de creator) of plaats het in de inbox-map.", href: "/videos", cta: "Videos" },
  ];
  return (
    <Card className="overflow-hidden">
      <div className="border-b border-line px-6 py-5">
        <p className="flex items-center gap-2 text-sm font-semibold"><Sparkles className="size-4 text-fire" /> Aan de slag in 3 stappen</p>
        <p className="mt-1 text-xs text-muted">Daarna vindt ViralClip AI zelfstandig de beste momenten en zet ze klaar als 9:16-clips.</p>
      </div>
      <div className="grid divide-y divide-line md:grid-cols-3 md:divide-x md:divide-y-0">
        {steps.map((s, i) => (
          <div key={s.title} className="space-y-3 p-6">
            <div className="flex items-center gap-3">
              <span className="flex size-8 items-center justify-center rounded-lg bg-panel-3 text-xs font-bold text-ink-2">{i + 1}</span>
              <s.icon className="size-4 text-fire" />
            </div>
            <p className="text-sm font-semibold">{s.title}</p>
            <p className="text-xs leading-relaxed text-muted">{s.text}</p>
            <LinkButton href={s.href} className="h-8 px-3 text-xs">
              {s.cta} <ArrowRight className="size-3.5" />
            </LinkButton>
          </div>
        ))}
      </div>
    </Card>
  );
}

export default function DashboardPage() {
  const { data, isLoading } = useApi<Dashboard>("/api/dashboard", { refreshInterval: 10000 });

  if (isLoading && !data) {
    return <div className="flex justify-center py-24"><Spinner /></div>;
  }
  if (!data) return null;
  const q = data.queue;

  return (
    <div className="space-y-8">
      <PageHeader
        title={
          <span className="flex flex-wrap items-center gap-3">
            <Flame className="size-7 text-fire" />
            <span>
              <span className="fire-text">{data.new_potential_viral_clips}</span> new potential viral clips
            </span>
          </span>
        }
        subtitle="Clips met een Viral Score van 70+ die de afgelopen 24 uur zijn gevonden."
        actions={
          <>
            <LinkButton href="/clips?sort=score">Alle clips</LinkButton>
            <LinkButton href="/creators" variant="fire">
              <Users className="size-4" /> Creators
            </LinkButton>
          </>
        }
      />

      {data.warnings.length > 0 && (
        <div className="space-y-2">
          {data.warnings.map((w) => (
            <Link
              key={w.text}
              href="/settings"
              className={`flex items-start gap-2 rounded-xl border px-4 py-3 text-xs transition hover:brightness-110 ${
                w.level === "error" ? "border-bad/30 bg-bad/10 text-bad" : "border-warn/25 bg-warn/5 text-warn"
              }`}
            >
              <TriangleAlert className="mt-0.5 size-3.5 shrink-0" />
              <span>{w.text}</span>
            </Link>
          ))}
        </div>
      )}

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Stat label="🔥 High potential (24u)" value={data.new_potential_viral_clips} hint={`${data.unrated_high_potential} nog niet beoordeeld`} accent />
        <Stat label="Nieuwe clips (24u)" value={data.new_clips_24h} hint={`${data.videos_analyzed_24h} video's geanalyseerd`} />
        <Stat
          label="Analysis Queue"
          value={q.running + q.queued}
          hint={
            <Link href="/queue" className="hover:text-ink">
              {q.running} bezig · {q.queued} wachtend{q.waiting ? ` · ${q.waiting} wacht op bron` : ""}
            </Link>
          }
        />
        <Stat label="AI-kosten vandaag" value={money(data.usage.cost_today_usd)} hint={`YouTube quota: ${Math.round(data.usage.youtube_quota_used)} / ${data.usage.youtube_quota_limit}`} />
      </div>

      {data.running_jobs.length > 0 && (
        <Card>
          <CardHeader title="Nu bezig" action={<Link href="/queue" className="text-xs text-muted hover:text-ink">Wachtrij →</Link>} />
          <div className="divide-y divide-line">
            {data.running_jobs.map((j) => (
              <div key={j.id} className="flex items-center gap-4 px-5 py-3">
                <ListOrdered className="size-4 shrink-0 text-info" />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm">{j.title}</p>
                  <p className="truncate text-xs text-muted">{j.message}</p>
                </div>
                <div className="w-32 shrink-0"><ProgressBar value={j.progress} /></div>
              </div>
            ))}
          </div>
        </Card>
      )}

      {data.today_by_creator.length > 0 && (
        <Card>
          <CardHeader title="Vandaag gevonden" subtitle="Beste clip per creator in de afgelopen 24 uur" />
          <div className="grid divide-y divide-line sm:grid-cols-2 sm:divide-y-0">
            {data.today_by_creator.map((c) => (
              <Link key={c.id} href={`/clips/${c.id}`} className="flex items-center gap-3 border-line px-5 py-3 transition hover:bg-panel-2 sm:border-b">
                {c.creator_thumbnail ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={c.creator_thumbnail} alt="" className="size-9 rounded-full object-cover" />
                ) : (
                  <span className="size-9 rounded-full bg-panel-3" />
                )}
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-semibold">{c.creator_name ?? "Eigen upload"}</p>
                  <p className="truncate text-xs text-muted">{c.title}</p>
                </div>
                <ScoreBadge score={c.viral_score} />
              </Link>
            ))}
          </div>
        </Card>
      )}

      <section>
        <div className="mb-4 flex items-end justify-between">
          <div>
            <h2 className="text-lg font-bold">Top clips</h2>
            <p className="text-xs text-muted">Hoogste Viral Score van de afgelopen 7 dagen</p>
          </div>
          <Link href="/clips" className="text-xs text-muted hover:text-ink">Alles bekijken →</Link>
        </div>
        {data.top_clips.length === 0 ? (
          data.creators === 0 ? (
            <Onboarding />
          ) : (
            <Card>
              <EmptyState
                icon={<Hourglass className="size-5" />}
                title="Nog geen clips"
                text={
                  data.videos_awaiting_media > 0
                    ? `${data.videos_awaiting_media} video('s) wachten op het bronbestand of transcript. Upload ze op de Videos-pagina of zet ze in de inbox-map.`
                    : "Zodra je creators nieuwe video's plaatsen en de bron beschikbaar is, verschijnen hier de beste momenten."
                }
                action={<LinkButton href="/videos">Naar Videos</LinkButton>}
              />
            </Card>
          )
        ) : (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-6">
            {data.top_clips.map((c) => (
              <ClipCard key={c.id} clip={c} />
            ))}
          </div>
        )}
      </section>

      <p className="text-center text-[11px] text-muted">
        De Viral Score is een voorspelling op basis van kenmerken (hook, retentie, emotie, deelbaarheid, …) — geen garantie. Gemiddelde clipduur doel: {formatDuration(15)}.
      </p>
    </div>
  );
}
