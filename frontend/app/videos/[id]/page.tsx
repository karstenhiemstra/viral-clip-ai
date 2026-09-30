"use client";

import { ArrowLeft, Check, ExternalLink, Inbox, MessageCircle, Trash2, Wand2 } from "lucide-react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";

import { ClipCard, ScoreBadge } from "@/components/clips";
import { errorText, useToast } from "@/components/toast";
import { Badge, Button, Card, CardHeader, EmptyState, PageHeader, ProgressBar, Spinner } from "@/components/ui";
import { UploadMediaButton, UploadTranscriptButton } from "@/components/uploads";
import { api, useApi } from "@/lib/api";
import { CATEGORY_LABELS, compactNumber, formatDate, formatDuration, formatNumber, formatTimestamp, money, VIDEO_STATUS } from "@/lib/format";
import type { Clip, Video } from "@/lib/types";

export default function VideoDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const toast = useToast();
  const { data: v, mutate } = useApi<Video>(`/api/videos/${id}`, { refreshInterval: (d) => (d?.job_status ? 3000 : 15000) });
  const { data: clips, mutate: mutateClips } = useApi<{ items: Clip[] }>(`/api/clips?video_id=${id}&include_rejected=true`, { refreshInterval: 8000 });

  if (!v) return <div className="flex justify-center py-24"><Spinner /></div>;
  const st = VIDEO_STATUS[v.status] ?? { label: v.status, tone: "neutral" as const };
  const run = v.latest_run;
  const warnings = run?.stage_log.find((s) => s.stage === "warnings")?.items ?? [];
  const refresh = () => { mutate(); mutateClips(); };

  async function analyze() {
    try {
      await api(`/api/videos/${v!.id}/analyze`, { method: "POST", json: { force: true } });
      toast("Analyse ingepland");
      refresh();
    } catch (e) {
      toast(errorText(e), "error");
    }
  }
  async function remove() {
    if (!confirm("Video, bronbestand en alle clips verwijderen?")) return;
    await api(`/api/videos/${v!.id}`, { method: "DELETE" });
    router.push("/videos");
  }

  return (
    <div className="space-y-6">
      <Link href="/videos" className="inline-flex items-center gap-1 text-xs text-muted hover:text-ink"><ArrowLeft className="size-3.5" /> Videos</Link>
      <PageHeader
        title={<span className="line-clamp-2">{v.title || "Zonder titel"}</span>}
        subtitle={
          <span className="flex flex-wrap items-center gap-2">
            <Badge tone={st.tone}>{st.label}</Badge>
            {v.creator_name ?? "Eigen upload"} · {formatDate(v.published_at ?? v.discovered_at)} · {formatDuration(v.duration_seconds)}
            {v.youtube_url && (
              <a href={v.youtube_url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 hover:text-ink">
                YouTube <ExternalLink className="size-3" />
              </a>
            )}
          </span>
        }
        actions={
          <>
            <Button icon={<Wand2 className="size-4" />} onClick={analyze}>Opnieuw analyseren</Button>
            <Button variant="ghost" icon={<Trash2 className="size-4" />} onClick={remove}>Verwijderen</Button>
          </>
        }
      />

      {v.job_status && (
        <Card className="p-5">
          <div className="flex items-center justify-between text-sm">
            <span>{v.job_status === "waiting" ? "Wacht op bronmateriaal" : "Analyse bezig"}</span>
            <span className="tabular-nums text-muted">{Math.round(v.job_progress ?? 0)}%</span>
          </div>
          <ProgressBar value={v.job_progress ?? 0} className="mt-2" />
          <p className="mt-2 text-xs text-muted">{v.job_message}</p>
        </Card>
      )}

      <div className="grid gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader title="Bronmateriaal" subtitle="ViralClip downloadt bewust niets van YouTube (Terms of Service). Lever de bron aan via een toegestane route." />
          <div className="grid gap-4 p-5 sm:grid-cols-2">
            <div className="space-y-2 rounded-xl border border-line bg-panel-2 p-4">
              <p className="flex items-center gap-2 text-sm font-semibold">
                {v.has_media ? <Check className="size-4 text-ok" /> : <span className="size-4 rounded-full border border-line-2" />} Videobestand
              </p>
              <p className="text-xs text-muted">
                {v.has_media
                  ? `${String(v.media_meta.width ?? "?")}×${String(v.media_meta.height ?? "?")} · ${formatDuration(Number(v.media_meta.duration ?? 0))} · via ${v.media_origin}`
                  : "Nodig voor audio-analyse en het renderen van verticale clips."}
              </p>
              <UploadMediaButton video={v} onDone={refresh} />
            </div>
            <div className="space-y-2 rounded-xl border border-line bg-panel-2 p-4">
              <p className="flex items-center gap-2 text-sm font-semibold">
                {v.has_transcript ? <Check className="size-4 text-ok" /> : <span className="size-4 rounded-full border border-line-2" />} Transcript
              </p>
              <p className="text-xs text-muted">
                {v.has_transcript ? `${formatNumber(v.transcript_words ?? 0)} woorden · bron: ${v.transcript_source}` : "Wordt automatisch gemaakt met Whisper zodra het videobestand er is, of upload SRT/VTT."}
              </p>
              <UploadTranscriptButton video={v} onDone={refresh} />
            </div>
            {v.youtube_video_id && !v.has_media && (
              <p className="flex items-start gap-2 text-xs text-muted sm:col-span-2">
                <Inbox className="mt-0.5 size-3.5 shrink-0" />
                <span>
                  Automatisch koppelen: zet het bestand in de inbox-map met het video-ID in de naam, bijv. <code className="rounded bg-panel-3 px-1 text-ink-2">titel [{v.youtube_video_id}].mp4</code>
                </span>
              </p>
            )}
          </div>
        </Card>

        <Card>
          <CardHeader title="Metadata" />
          <dl className="grid grid-cols-2 gap-4 p-5 text-sm">
            <div><dt className="text-xs text-muted">Views</dt><dd className="font-semibold">{compactNumber(v.view_count)}</dd></div>
            <div><dt className="text-xs text-muted">Likes</dt><dd className="font-semibold">{compactNumber(v.like_count)}</dd></div>
            <div><dt className="text-xs text-muted">Comments</dt><dd className="font-semibold">{compactNumber(v.comment_count)}</dd></div>
            <div><dt className="text-xs text-muted">Prioriteit</dt><dd className="font-semibold">{v.prescore != null ? Math.round(v.prescore) : "—"}</dd></div>
            {Object.entries(v.prescore_details ?? {}).map(([k, val]) => (
              <div key={k}><dt className="text-xs text-muted">{k}</dt><dd className="text-ink-2 tabular-nums">{Math.round(val)}</dd></div>
            ))}
          </dl>
        </Card>
      </div>

      {v.crowd_hotspots?.length > 0 && (
        <Card>
          <CardHeader title="Audience hotspots" subtitle="Momenten die kijkers noemen in de YouTube-comments — sterk bewijs voor deelbaarheid." />
          <div className="divide-y divide-line">
            {v.crowd_hotspots.slice(0, 8).map((h) => (
              <div key={h.time} className="flex items-start gap-4 px-5 py-3 text-sm">
                <span className="w-14 shrink-0 font-mono text-xs text-fire">{formatTimestamp(h.time)}</span>
                <MessageCircle className="mt-0.5 size-3.5 shrink-0 text-muted" />
                <p className="min-w-0 flex-1 truncate text-ink-2">{h.sample}</p>
                <span className="shrink-0 text-xs text-muted">{h.count}× · {Math.round(h.strength * 100)}%</span>
              </div>
            ))}
          </div>
        </Card>
      )}

      <section>
        <h2 className="mb-3 text-lg font-bold">Clips uit deze video</h2>
        {clips?.items.length ? (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-6">
            {clips.items.map((c) => <ClipCard key={c.id} clip={c} />)}
          </div>
        ) : (
          <Card><EmptyState icon={<Wand2 className="size-5" />} title="Nog geen clips" text="Na de analyse verschijnen hier de beste momenten." /></Card>
        )}
      </section>

      {run && (
        <Card>
          <CardHeader
            title="Laatste analyse"
            subtitle={`${run.provider ?? "—"}${Object.keys(run.models ?? {}).length ? ` (${Object.values(run.models).filter((m, i, a) => a.indexOf(m) === i).join(", ")})` : ""} · ${formatDate(run.finished_at ?? run.started_at)} · ${formatNumber(run.input_tokens + run.output_tokens)} tokens · ${money(run.estimated_cost_usd)}`}
          />
          {run.error && <p className="border-b border-line px-5 py-3 text-xs text-bad">{run.error}</p>}
          {warnings.length > 0 && (
            <ul className="space-y-1 border-b border-line px-5 py-3 text-xs text-warn">
              {warnings.map((w) => <li key={w}>⚠ {w}</li>)}
            </ul>
          )}
          <div className="flex flex-wrap gap-2 border-b border-line px-5 py-3">
            {run.stage_log.filter((s) => s.stage !== "warnings").map((s) => (
              <Badge key={s.stage} tone="muted">{s.stage} · {s.seconds}s</Badge>
            ))}
          </div>
          <div className="overflow-x-auto">
            <table className="w-full min-w-[760px] text-sm">
              <thead>
                <tr className="border-b border-line text-left text-[11px] tracking-wide text-muted uppercase">
                  <th className="px-5 py-2.5 font-medium">Moment</th>
                  <th className="px-3 py-2.5 font-medium">Bron</th>
                  <th className="px-3 py-2.5 font-medium">Categorie</th>
                  <th className="px-3 py-2.5 font-medium">Waarom (niet)</th>
                  <th className="px-5 py-2.5 text-right font-medium">Score</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {[...run.candidates]
                  .sort((a, b) => (b.viral_score ?? -1) - (a.viral_score ?? -1))
                  .map((c) => (
                    <tr key={c.id} className={c.selected ? "bg-fire/5" : ""}>
                      <td className="px-5 py-2.5 font-mono text-xs whitespace-nowrap text-ink-2">
                        {formatTimestamp(c.final_start ?? c.start)}–{formatTimestamp(c.final_end ?? c.end)}
                        {c.selected && <Check className="ml-2 inline size-3.5 text-ok" />}
                      </td>
                      <td className="px-3 py-2.5 text-xs text-muted">{c.sources.join(" + ")}</td>
                      <td className="px-3 py-2.5 text-xs">{CATEGORY_LABELS[c.category ?? "other"] ?? c.category ?? "—"}</td>
                      <td className="max-w-md px-3 py-2.5 text-xs text-ink-2"><p className="line-clamp-2">{c.why || c.reason || c.description || c.note}</p></td>
                      <td className="px-5 py-2.5 text-right">{c.viral_score != null ? <ScoreBadge score={c.viral_score} /> : <span className="text-xs text-muted">—</span>}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}
