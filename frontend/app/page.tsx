"use client";

import { ArrowRight, CheckCircle2, Download, Flame, Hourglass, ListOrdered, Play, Sparkles, TriangleAlert, Users } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { ClipPlayer, ScoreBadge } from "@/components/clips";
import { Badge, Button, Card, CardHeader, EmptyState, LinkButton, Modal, PageHeader, ProgressBar, Spinner } from "@/components/ui";
import { useApi } from "@/lib/api";
import { CATEGORY_LABELS, CLIP_STATUS, formatDuration, money } from "@/lib/format";
import type { Clip, Dashboard } from "@/lib/types";

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

function SetupChecklist({ steps }: { steps: Dashboard["setup"] }) {
  const done = steps.filter((s) => s.done).length;
  const next = steps.find((s) => !s.done);
  return (
    <Card className="overflow-hidden">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-6 py-4">
        <div>
          <p className="flex items-center gap-2 text-sm font-semibold"><Sparkles className="size-4 text-fire" /> Aan de slag</p>
          <p className="mt-1 text-xs text-muted">Daarna vindt ViralClip AI zelfstandig de beste momenten en zet ze klaar als 9:16-clips.</p>
        </div>
        <span className="text-xs tabular-nums text-muted">{done} van {steps.length} klaar</span>
      </div>
      <ol className="divide-y divide-line">
        {steps.map((s, i) => (
          <li key={s.key} className={`flex items-center gap-4 px-6 py-3 ${s === next ? "bg-panel-2/60" : ""}`}>
            {s.done ? (
              <CheckCircle2 className="size-5 shrink-0 text-ok" />
            ) : (
              <span className="flex size-5 shrink-0 items-center justify-center rounded-full border border-line-2 text-[10px] font-bold text-muted">{i + 1}</span>
            )}
            <div className="min-w-0 flex-1">
              <p className={`text-sm ${s.done ? "text-muted line-through" : "font-semibold"}`}>{s.label}</p>
              {!s.done && <p className="text-xs text-muted">{s.hint}</p>}
            </div>
            {!s.done && (
              <LinkButton href={s.href} variant={s === next ? "fire" : "secondary"} className="h-8 shrink-0 px-3 text-xs">
                Open <ArrowRight className="size-3.5" />
              </LinkButton>
            )}
          </li>
        ))}
      </ol>
    </Card>
  );
}

function TopClipsList({ clips }: { clips: Clip[] }) {
  const [preview, setPreview] = useState<Clip | null>(null);
  return (
    <Card>
      <div className="divide-y divide-line">
        {clips.map((c, i) => {
          const st = CLIP_STATUS[c.status];
          return (
            <div key={c.id} className="flex flex-wrap items-center gap-4 px-4 py-3 sm:flex-nowrap sm:px-5">
              <span className={`w-8 shrink-0 text-center text-lg font-extrabold tabular-nums ${i < 3 ? "fire-text" : "text-muted"}`}>#{i + 1}</span>
              <button onClick={() => setPreview(c)} className="relative aspect-[9/16] w-12 shrink-0 overflow-hidden rounded-md border border-line bg-panel-2" title="Preview">
                {(c.thumbnail_url || c.video_thumbnail) && (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={c.thumbnail_url ?? c.video_thumbnail ?? ""} alt="" className="size-full object-cover" />
                )}
              </button>
              <div className="min-w-0 flex-1">
                <p className="flex items-center gap-2 text-sm">
                  <span className="font-semibold">{c.creator_name ?? "Eigen upload"}</span>
                  <ScoreBadge score={c.viral_score} />
                  <span className="text-xs font-semibold tabular-nums text-ink-2">{Math.round(c.viral_score)}/100</span>
                </p>
                <Link href={`/clips/${c.id}`} className="block truncate text-sm text-ink-2 hover:text-fire">{c.title || c.hook_text}</Link>
                <p className="text-xs text-muted">
                  {formatDuration(c.duration)} · {CATEGORY_LABELS[c.category ?? "other"] ?? c.category ?? "—"} · {c.potential_label}
                </p>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                <Button size="sm" icon={<Play className="size-3.5" />} onClick={() => setPreview(c)}>Preview</Button>
                {c.download_url ? (
                  <a href={c.download_url} className="fire-gradient inline-flex h-8 items-center gap-1.5 rounded-lg px-3 text-xs font-semibold text-white hover:brightness-110">
                    <Download className="size-3.5" /> Download
                  </a>
                ) : (
                  <Link href={c.status === "awaiting_media" ? `/videos/${c.video_id}` : `/clips/${c.id}`}>
                    <Badge tone={st?.tone ?? "neutral"}>{c.status === "awaiting_media" ? "Bron nodig" : (st?.label ?? c.status)}</Badge>
                  </Link>
                )}
              </div>
            </div>
          );
        })}
      </div>
      <div className="border-t border-line px-5 py-3 text-right">
        <Link href="/clips" className="text-xs text-muted hover:text-ink">Alle clips bekijken →</Link>
      </div>
      <Modal open={!!preview} onClose={() => setPreview(null)} title={preview ? `${preview.creator_name ?? "Eigen upload"} · ${Math.round(preview.viral_score)}/100` : ""}>
        {preview && (
          <div className="space-y-4">
            <ClipPlayer clip={preview} />
            <p className="text-sm font-semibold">{preview.title || preview.hook_text}</p>
            {preview.explanation && <p className="text-xs leading-relaxed text-muted">{preview.explanation}</p>}
            <div className="flex justify-end gap-2">
              <LinkButton href={`/clips/${preview.id}`}>Details & bewerken</LinkButton>
              {preview.download_url && (
                <a href={preview.download_url} className="fire-gradient inline-flex h-10 items-center gap-2 rounded-lg px-4 text-sm font-semibold text-white hover:brightness-110">
                  <Download className="size-4" /> Download
                </a>
              )}
            </div>
          </div>
        )}
      </Modal>
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
            <span>Top Viral Clips</span>
          </span>
        }
        subtitle={
          <>
            <span className="fire-text font-semibold">{data.new_potential_viral_clips}</span> new potential viral clips in de afgelopen 24 uur
            (Viral Score 70+). Beste bovenaan.
          </>
        }
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

      {data.setup?.some((s) => !s.done) && <SetupChecklist steps={data.setup} />}

      <section>
        {data.top_clips.length === 0 ? (
          <Card>
            <EmptyState
              icon={<Hourglass className="size-5" />}
              title="Nog geen clips"
              text={
                data.creators === 0
                  ? "Voeg eerst een creator toe, bijvoorbeeld Enzo Knol, Bankzitters, Hanwe, Gio of StukTV."
                  : data.videos_awaiting_media > 0
                    ? `${data.videos_awaiting_media} video('s) wachten op de bronvideo. Lever die aan (upload, Drive/Dropbox/OneDrive-link of .srt) en de clips verschijnen hier vanzelf.`
                    : "Zodra je creators nieuwe video's plaatsen en de bron beschikbaar is, verschijnen hier de beste momenten."
              }
              action={<LinkButton href={data.creators ? "/videos?status=awaiting_media" : "/creators"}>{data.creators ? "Bron aanleveren" : "Creator toevoegen"}</LinkButton>}
            />
          </Card>
        ) : (
          <TopClipsList clips={data.top_clips.slice(0, 10)} />
        )}
      </section>

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


      <p className="text-center text-[11px] text-muted">
        De Viral Score is een voorspelling op basis van kenmerken (hook, retentie, emotie, deelbaarheid, …) — geen garantie. Gemiddelde clipduur doel: {formatDuration(15)}.
      </p>
    </div>
  );
}
