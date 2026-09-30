"use client";

import { Ban, Film, Plus, Search, Wand2 } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { ScoreBadge } from "@/components/clips";
import { errorText, useToast } from "@/components/toast";
import { Badge, Button, Card, EmptyState, Input, PageHeader, ProgressBar, Select, Spinner } from "@/components/ui";
import { AddVideoModal, ImportLinkButton, UploadMediaButton } from "@/components/uploads";
import { api, useApi } from "@/lib/api";
import { compactNumber, formatDuration, relativeTime, VIDEO_STATUS } from "@/lib/format";
import type { Creator, Video } from "@/lib/types";

function VideosInner() {
  const toast = useToast();
  const router = useRouter();
  const params = useSearchParams();
  const creatorId = params.get("creator_id") ?? "";
  const status = params.get("status") ?? "";
  const [q, setQ] = useState(params.get("q") ?? "");
  const [adding, setAdding] = useState(false);
  const qs = new URLSearchParams();
  if (creatorId) qs.set("creator_id", creatorId);
  if (status) qs.set("status", status);
  if (params.get("q")) qs.set("q", params.get("q")!);
  qs.set("limit", "100");
  const { data, mutate, isLoading } = useApi<{ items: Video[]; total: number }>(`/api/videos?${qs}`, { refreshInterval: 6000 });
  const { data: creators } = useApi<Creator[]>("/api/creators");

  function setParam(key: string, value: string) {
    const next = new URLSearchParams(params.toString());
    if (value) next.set(key, value);
    else next.delete(key);
    router.replace(`/videos?${next}`);
  }

  async function analyze(v: Video) {
    if (v.status === "analyzed" && !confirm("Deze video is al geanalyseerd. Opnieuw analyseren kost opnieuw AI-tegoed. Doorgaan?")) return;
    try {
      await api(`/api/videos/${v.id}/analyze`, { method: "POST", json: { force: true } });
      toast("Analyse ingepland");
      mutate();
    } catch (e) {
      toast(errorText(e), "error");
    }
  }
  async function skip(v: Video) {
    await api(`/api/videos/${v.id}/skip`, { method: "POST" });
    mutate();
  }

  return (
    <div>
      <PageHeader
        title="Videos"
        subtitle={data ? `${data.total} gevonden YouTube-video's en uploads` : "Alle gevonden YouTube-video's en uploads"}
        actions={<Button variant="fire" icon={<Plus className="size-4" />} onClick={() => setAdding(true)}>Video toevoegen</Button>}
      />
      <div className="mb-4 flex flex-wrap gap-2">
        <form
          className="relative min-w-[220px] flex-1"
          onSubmit={(e) => {
            e.preventDefault();
            setParam("q", q);
          }}
        >
          <Search className="pointer-events-none absolute top-3 left-3 size-4 text-muted" />
          <Input className="pl-9" placeholder="Zoek op titel of kanaal" value={q} onChange={(e) => setQ(e.target.value)} />
        </form>
        <Select value={creatorId} onChange={(e) => setParam("creator_id", e.target.value)}>
          <option value="">Alle creators</option>
          {creators?.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </Select>
        <Select value={status} onChange={(e) => setParam("status", e.target.value)}>
          <option value="">Alle statussen</option>
          <option value="pending">In behandeling</option>
          {Object.entries(VIDEO_STATUS).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
        </Select>
      </div>
      <Card>
        {isLoading && !data ? (
          <div className="flex justify-center py-16"><Spinner /></div>
        ) : !data?.items.length ? (
          <EmptyState
            icon={<Film className="size-5" />}
            title="Geen video's gevonden"
            text="Voeg creators toe voor automatische discovery, plak een YouTube-URL of upload je eigen video."
            action={<Button variant="fire" icon={<Plus className="size-4" />} onClick={() => setAdding(true)}>Video toevoegen</Button>}
          />
        ) : (
          <div className="divide-y divide-line">
            {data.items.map((v) => {
              const st = VIDEO_STATUS[v.status] ?? { label: v.status, tone: "neutral" as const };
              const running = v.job_status === "running";
              return (
                <div key={v.id} className="flex flex-col gap-3 px-4 py-3 sm:flex-row sm:items-center sm:px-5">
                  <Link href={`/videos/${v.id}`} className="flex min-w-0 flex-1 items-center gap-4">
                    <div className="relative aspect-video w-32 shrink-0 overflow-hidden rounded-lg border border-line bg-panel-2">
                      {v.thumbnail_url && (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img src={v.thumbnail_url} alt="" className="size-full object-cover" loading="lazy" />
                      )}
                      {v.duration_seconds && (
                        <span className="absolute right-1 bottom-1 rounded bg-black/75 px-1 text-[10px] font-medium text-white">{formatDuration(v.duration_seconds)}</span>
                      )}
                    </div>
                    <div className="min-w-0 flex-1">
                      <p className="line-clamp-2 text-sm font-semibold hover:text-fire">{v.title || "Zonder titel"}</p>
                      <p className="mt-0.5 truncate text-xs text-muted">
                        {v.creator_name ?? (v.source === "upload" ? "Eigen upload" : "—")} · {relativeTime(v.published_at ?? v.discovered_at)}
                        {v.view_count != null && ` · ${compactNumber(v.view_count)} views`}
                        {v.prescore != null && ` · prioriteit ${Math.round(v.prescore)}`}
                      </p>
                      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                        <Badge tone={st.tone}>{st.label}</Badge>
                        {v.has_media && <Badge tone="muted">Bron ✓</Badge>}
                        {v.has_transcript && <Badge tone="muted">Transcript ✓</Badge>}
                        {v.crowd_hotspots?.length > 0 && <Badge tone="muted">{v.crowd_hotspots.length} comment-hotspots</Badge>}
                        {v.skip_reason && v.status !== "analyzed" && <span className="text-[11px] text-muted">{v.skip_reason}</span>}
                      </div>
                      {running && (
                        <div className="mt-2 max-w-sm">
                          <ProgressBar value={v.job_progress ?? 0} />
                          <p className="mt-1 truncate text-[11px] text-muted">{v.job_message}</p>
                        </div>
                      )}
                    </div>
                  </Link>
                  <div className="flex shrink-0 items-center gap-2 sm:justify-end">
                    {!!v.clip_count && (
                      <Link href={`/clips?video_id=${v.id}`} className="flex items-center gap-1.5 text-xs text-muted hover:text-ink">
                        {v.clip_count} clips {v.best_score != null && <ScoreBadge score={v.best_score} />}
                      </Link>
                    )}
                    {!v.has_media && v.status !== "skipped" && (
                      <>
                        <UploadMediaButton video={v} onDone={() => mutate()} />
                        <ImportLinkButton video={v} onDone={() => mutate()} />
                      </>
                    )}
                    <Button size="sm" variant="ghost" title={v.status === "analyzed" ? "Opnieuw analyseren" : "Analyseren"} onClick={() => analyze(v)}><Wand2 className="size-3.5" /></Button>
                    {v.status !== "skipped" && v.status !== "analyzed" && (
                      <Button size="sm" variant="ghost" title="Overslaan" onClick={() => skip(v)}><Ban className="size-3.5" /></Button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </Card>
      <AddVideoModal open={adding} onClose={() => setAdding(false)} onAdded={() => mutate()} />
    </div>
  );
}

export default function VideosPage() {
  return (
    <Suspense fallback={<div className="flex justify-center py-24"><Spinner /></div>}>
      <VideosInner />
    </Suspense>
  );
}
