"use client";

import { ArrowLeft, Captions, Clock, Copy, Download, ExternalLink, Minus, Plus, RefreshCw, Save, TrendingUp } from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useRef, useState } from "react";

import { CaptionEditor } from "@/components/captions";
import { ClipPlayer, FeedbackButtons, ScoreBadge, ScoreBars } from "@/components/clips";
import { errorText, useToast } from "@/components/toast";
import { Badge, Button, Card, CardHeader, cx, Field, Input, Select, Spinner } from "@/components/ui";
import { api, useApi } from "@/lib/api";
import {
  CATEGORY_LABELS,
  CLIP_STATUS,
  compactNumber,
  DIMENSION_LABELS,
  FLAG_LABELS,
  formatDate,
  formatDuration,
  formatTimestamp,
  LAYOUT_LABELS,
  pct,
  PRESET_LABELS,
  STAGE_LABELS,
} from "@/lib/format";
import type { Clip } from "@/lib/types";

const LANGUAGES: Record<string, string> = { nl: "Nederlands", en: "Engels", de: "Duits", fr: "Frans", es: "Spaans", it: "Italiaans", pt: "Portugees" };

function languageLabel(info: NonNullable<Clip["caption_language"]>): string {
  const name = (code: string) => LANGUAGES[code] ?? code.toUpperCase();
  const main = name(info.caption_language ?? "");
  const mixed = info.mixed && info.languages ? ` (gemengd: ${Object.entries(info.languages).filter(([, v]) => v >= 0.1).map(([k, v]) => `${name(k)} ${Math.round(v * 100)}%`).join(", ")})` : "";
  const sure = info.confidence != null ? `, ${Math.round(info.confidence * 100)}% zeker` : "";
  return `${main}${mixed} · niet vertaald${sure}`;
}

function Transcript({ clip }: { clip: Clip }) {
  const emph = new Set((clip.emphasis_words ?? []).map((w) => w.toLowerCase().replace(/[^\p{L}\p{N}]/gu, "")));
  const words = clip.words ?? [];
  return (
    <p className="text-sm leading-7 text-ink-2">
      {words.map(([s, , text], i) => {
        const norm = text.toLowerCase().replace(/[^\p{L}\p{N}]/gu, "");
        const hook = s - clip.start_time < 2.2;
        return (
          <span key={i} className={cx(emph.has(norm) && "font-semibold text-warn", hook && "text-ink")} title={formatTimestamp(s)}>
            {text}{" "}
          </span>
        );
      })}
    </p>
  );
}

function PerformanceForm({ clip, onSaved }: { clip: Clip; onSaved: () => void }) {
  const toast = useToast();
  const [form, setForm] = useState<Record<string, string>>({ platform: "tiktok" });
  const [busy, setBusy] = useState(false);
  const set = (k: string) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) => setForm({ ...form, [k]: e.target.value });
  async function save(e: React.FormEvent) {
    e.preventDefault();
    const body: Record<string, unknown> = { platform: form.platform };
    if (form.post_url) body.post_url = form.post_url;
    for (const k of ["views", "likes", "comments", "shares", "saves"]) if (form[k]) body[k] = Number(form[k]);
    if (form.avg_percentage_watched) body.avg_percentage_watched = Number(form.avg_percentage_watched);
    if (form.completion_rate) body.completion_rate = Number(form.completion_rate) / 100;
    setBusy(true);
    try {
      await api(`/api/clips/${clip.id}/performance`, { method: "POST", json: body });
      toast("Statistieken opgeslagen — deze voeden het persoonlijke leersysteem");
      setForm({ platform: form.platform });
      onSaved();
    } catch (err) {
      toast(errorText(err), "error");
    } finally {
      setBusy(false);
    }
  }
  return (
    <form onSubmit={save} className="space-y-3">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Field label="Platform">
          <Select value={form.platform} onChange={set("platform")} className="w-full">
            <option value="tiktok">TikTok</option>
            <option value="reels">Reels</option>
            <option value="shorts">Shorts</option>
            <option value="other">Anders</option>
          </Select>
        </Field>
        {[["views", "Views"], ["likes", "Likes"], ["comments", "Comments"], ["shares", "Shares"], ["saves", "Saves"], ["avg_percentage_watched", "Gem. % bekeken"], ["completion_rate", "Completion %"]].map(([k, label]) => (
          <Field key={k} label={label}>
            <Input type="number" min={0} step="any" value={form[k] ?? ""} onChange={set(k)} />
          </Field>
        ))}
      </div>
      <Field label="Link naar post">
        <Input placeholder="https://www.tiktok.com/@…/video/…" value={form.post_url ?? ""} onChange={set("post_url")} />
      </Field>
      <div className="flex justify-end">
        <Button type="submit" variant="primary" loading={busy} icon={<Save className="size-4" />}>Opslaan</Button>
      </div>
    </form>
  );
}

export default function ClipDetailPage() {
  const { id } = useParams<{ id: string }>();
  const toast = useToast();
  const { data: clip, mutate } = useApi<Clip>(`/api/clips/${id}`, {
    refreshInterval: (d) => (d && ["pending_render", "rendering"].includes(d.status) ? 3000 : 0),
  });
  const [trim, setTrim] = useState<{ start: number; end: number } | null>(null);
  const [saving, setSaving] = useState(false);
  const [editingCaptions, setEditingCaptions] = useState(false);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const videoRef = useRef<HTMLVideoElement>(null);

  if (!clip) return <div className="flex justify-center py-24"><Spinner /></div>;
  const st = CLIP_STATUS[clip.status];
  const start = trim?.start ?? clip.start_time;
  const end = trim?.end ?? clip.end_time;
  const breakdown = (clip.score_breakdown ?? {}) as Record<string, number | string | string[] | null>;
  const signals = (clip.signals ?? {}) as Record<string, unknown>;

  async function patch(body: Record<string, unknown>, msg: string) {
    setSaving(true);
    try {
      const updated = await api<Clip>(`/api/clips/${clip!.id}`, { method: "PATCH", json: body });
      mutate(updated, { revalidate: false });
      setTrim(null);
      toast(msg);
    } catch (e) {
      toast(errorText(e), "error");
    } finally {
      setSaving(false);
    }
  }

  async function rerender() {
    setSaving(true);
    try {
      await api(`/api/clips/${clip!.id}/render`, { method: "POST" });
      mutate();
      toast("Clip wordt opnieuw gerenderd");
    } catch (e) {
      toast(errorText(e), "error");
    } finally {
      setSaving(false);
    }
  }

  function nudge(which: "start" | "end", delta: number) {
    const next = { start, end, [which]: Math.max(0, (which === "start" ? start : end) + delta) };
    if (next.end - next.start >= 2) setTrim(next);
  }

  return (
    <div className="space-y-6">
      <Link href="/clips" className="inline-flex items-center gap-1 text-xs text-muted hover:text-ink"><ArrowLeft className="size-3.5" /> Clips</Link>
      <div className="grid gap-8 lg:grid-cols-[minmax(0,400px)_minmax(0,1fr)]">
        {/* left: player + actions */}
        <div className="space-y-4">
          {previewUrl && (
            <div className="flex items-center justify-between gap-2 rounded-lg border border-fire/30 bg-fire/10 px-3 py-2 text-xs text-ink-2">
              <span>Preview met je aangepaste captions (nog niet de download)</span>
              <Button size="sm" variant="ghost" onClick={() => setPreviewUrl(null)}>Terug naar clip</Button>
            </div>
          )}
          <ClipPlayer clip={clip} src={previewUrl} videoRef={videoRef} />
          {clip.download_url ? (
            <a href={clip.download_url} className="fire-gradient flex h-12 items-center justify-center gap-2 rounded-xl text-sm font-semibold text-white shadow-[0_10px_30px_-10px_rgba(255,80,50,0.8)] transition hover:brightness-110">
              <Download className="size-4" /> Download Clip
            </a>
          ) : (
            <div className="flex h-12 items-center justify-center gap-2 rounded-xl border border-line bg-panel-2 text-sm text-muted">
              {st?.label ?? clip.status}
            </div>
          )}
          {clip.render_error && <p className="rounded-lg border border-bad/30 bg-bad/10 p-3 text-xs text-bad">{clip.render_error.slice(-400)}</p>}
          {clip.status === "awaiting_media" && (
            <p className="rounded-lg border border-warn/30 bg-warn/10 p-3 text-xs text-warn">
              Deze clip is gevonden op basis van het transcript. Lever het bronbestand aan op de{" "}
              <Link href={`/videos/${clip.video_id}`} className="underline">videopagina</Link> (upload of deel-link); daarna wordt de 9:16-clip automatisch gerenderd.
            </p>
          )}
          {(clip.status === "failed" || clip.status === "ready") && (
            <Button className="w-full" variant="secondary" loading={saving} icon={<RefreshCw className="size-4" />} onClick={rerender}>
              Opnieuw renderen
            </Button>
          )}
          {clip.video_url && !editingCaptions && (
            <Button className="w-full" variant="secondary" icon={<Captions className="size-4" />} onClick={() => setEditingCaptions(true)}>
              Captions aanpassen
            </Button>
          )}
          {editingCaptions && (
            <CaptionEditor
              clip={clip}
              videoRef={videoRef}
              onPreview={setPreviewUrl}
              onRendered={() => { setPreviewUrl(null); mutate(); }}
              onClose={() => { setEditingCaptions(false); setPreviewUrl(null); }}
            />
          )}
          <FeedbackButtons clip={clip} onChange={(c) => mutate(c, { revalidate: false })} />

          <Card className="space-y-4 p-4">
            <p className="text-xs font-semibold text-ink-2">Bewerken & opnieuw renderen</p>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Captions" hint={clip.captions_custom ? "Met je eigen aangepaste captions" : undefined}>
                <Select className="w-full" value={clip.caption_preset ?? "dynamic"} onChange={(e) => patch({ caption_preset: e.target.value }, "Captions aangepast — clip wordt opnieuw gerenderd")}>
                  {Object.entries(PRESET_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                </Select>
              </Field>
              <Field label="Beeld (9:16)">
                <Select className="w-full" value={clip.layout ?? "auto"} onChange={(e) => patch({ layout: e.target.value }, "Layout aangepast — clip wordt opnieuw gerenderd")}>
                  {Object.entries(LAYOUT_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                </Select>
              </Field>
            </div>
            <div className="grid grid-cols-2 gap-3 text-xs">
              {(["start", "end"] as const).map((w) => (
                <div key={w} className="space-y-1.5">
                  <span className="text-ink-2">{w === "start" ? "Start" : "Einde"}</span>
                  <div className="flex items-center gap-1">
                    <Button size="sm" variant="secondary" onClick={() => nudge(w, -0.5)} aria-label="-0,5s"><Minus className="size-3" /></Button>
                    <span className="flex-1 text-center font-mono tabular-nums">{formatTimestamp(w === "start" ? start : end)}.{Math.round(((w === "start" ? start : end) % 1) * 10)}</span>
                    <Button size="sm" variant="secondary" onClick={() => nudge(w, 0.5)} aria-label="+0,5s"><Plus className="size-3" /></Button>
                  </div>
                </div>
              ))}
            </div>
            {trim && clip.captions_custom && (
              <p className="text-[11px] text-warn">Let op: bij nieuwe start/einde worden je aangepaste captions teruggezet naar automatisch.</p>
            )}
            {trim && (
              <Button variant="primary" className="w-full" loading={saving} icon={<RefreshCw className="size-4" />} onClick={() => patch({ start_time: trim.start, end_time: trim.end }, "Nieuwe grenzen opgeslagen — clip wordt opnieuw gerenderd")}>
                Toepassen ({(end - start).toFixed(1)}s)
              </Button>
            )}
          </Card>
        </div>

        {/* right: scores + explanation */}
        <div className="min-w-0 space-y-6">
          <div>
            <div className="flex flex-wrap items-center gap-2 text-sm text-muted">
              <Link href={clip.creator_id ? `/videos?creator_id=${clip.creator_id}` : "#"} className="font-semibold text-ink hover:text-fire">{clip.creator_name ?? "Eigen upload"}</Link>
              <span>·</span>
              <Link href={`/videos/${clip.video_id}`} className="truncate hover:text-ink">{clip.video_title}</Link>
            </div>
            <h1 className="mt-2 text-2xl font-bold tracking-tight sm:text-3xl">{clip.title || clip.hook_text}</h1>
            <div className="mt-4 flex flex-wrap items-center gap-3">
              <ScoreBadge score={clip.viral_score} size="lg" />
              <div>
                <p className="text-sm font-semibold">Viral Score: {Math.round(clip.viral_score)}/100</p>
                <p className="text-xs text-muted">{clip.potential_label} · voorspelling, geen garantie</p>
              </div>
              <div className="ml-auto flex flex-wrap gap-1.5">
                {clip.category && <Badge tone="fire">{CATEGORY_LABELS[clip.category] ?? clip.category}</Badge>}
                {st && <Badge tone={st.tone}>{st.label}</Badge>}
              </div>
            </div>
          </div>

          <div className="grid grid-cols-3 gap-3">
            {Object.entries(STAGE_LABELS).map(([k, s]) => (
              <Card key={k} className="p-4">
                <p className="text-xs text-muted" title={s.hint}>{s.label}</p>
                <p className="mt-1 text-2xl font-bold tabular-nums">{Math.round(clip.stage_scores?.[k] ?? 0)}</p>
                <p className="mt-1 hidden text-[11px] leading-snug text-muted sm:block">{s.hint}</p>
              </Card>
            ))}
          </div>

          <Card>
            <CardHeader title="Waarom de AI deze clip koos" />
            <div className="space-y-4 p-5">
              <p className="text-sm leading-relaxed text-ink">{clip.explanation || "—"}</p>
              {(signals.first_seconds || signals.viewer_reaction) ? (
                <div className="grid gap-3 sm:grid-cols-2">
                  {typeof signals.first_seconds === "string" && signals.first_seconds && (
                    <div className="rounded-xl bg-panel-2 p-3">
                      <p className="text-[11px] font-medium text-muted uppercase">Eerste seconden</p>
                      <p className="mt-1 text-sm text-ink-2">“{signals.first_seconds}”</p>
                    </div>
                  )}
                  {typeof signals.viewer_reaction === "string" && signals.viewer_reaction && (
                    <div className="rounded-xl bg-panel-2 p-3">
                      <p className="text-[11px] font-medium text-muted uppercase">Reactie van een scroller</p>
                      <p className="mt-1 text-sm text-ink-2">{signals.viewer_reaction}</p>
                    </div>
                  )}
                </div>
              ) : null}
              {clip.flags.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {clip.flags.map((f) => <Badge key={f} tone="warning">{FLAG_LABELS[f] ?? f}</Badge>)}
                </div>
              )}
            </div>
          </Card>

          <Card>
            <CardHeader title="Score per factor" subtitle="Gewichten zijn aan te passen in Settings" />
            <div className="p-5"><ScoreBars scores={clip.scores} labels={DIMENSION_LABELS} /></div>
            <div className="flex flex-wrap gap-x-5 gap-y-1 border-t border-line px-5 py-3 text-[11px] text-muted">
              <span>Content (funnel): {String(breakdown.content_score ?? "—")}</span>
              {breakdown.signal_score != null && Number(breakdown.signal_blend) > 0 && <span>Audio/tekst-signalen: {Math.round(Number(breakdown.signal_score))} (weegt {Math.round(Number(breakdown.signal_blend) * 100)}%)</span>}
              {Number(breakdown.crowd_bonus) > 0 && <span>Comment-hotspot bonus: +{String(breakdown.crowd_bonus)}</span>}
              {Number(breakdown.personal_adjustment) !== 0 && breakdown.personal_adjustment != null && <span>Persoonlijk model: {Number(breakdown.personal_adjustment) > 0 ? "+" : ""}{String(breakdown.personal_adjustment)}</span>}
              {Array.isArray(breakdown.penalties) && breakdown.penalties.length > 0 && <span>Strafpunten: ×{String(breakdown.penalty_multiplier)}</span>}
              {breakdown.mode ? <span>Modus: {String(breakdown.mode)}</span> : null}
            </div>
          </Card>

          <Card>
            <CardHeader
              title="Transcript"
              subtitle={
                <span className="flex flex-wrap items-center gap-2">
                  <Clock className="size-3" /> {formatTimestamp(clip.start_time)} – {formatTimestamp(clip.end_time)} · {formatDuration(clip.duration)}
                  {(clip.segments?.length ?? 0) > 1 && ` · ${clip.segments!.length - 1} stiltes eruit geknipt`}
                  {clip.caption_language?.caption_language && (
                    <span title="De captions zijn een transcriptie in de gesproken taal, niet vertaald">
                      · Taal: {languageLabel(clip.caption_language)}
                    </span>
                  )}
                </span>
              }
              action={
                <div className="flex gap-1">
                  <Button size="sm" variant="ghost" title="Kopieer caption" onClick={() => { navigator.clipboard.writeText(clip.title ?? ""); toast("Titel gekopieerd"); }}><Copy className="size-3.5" /></Button>
                  {clip.youtube_url && (
                    <a href={clip.youtube_url} target="_blank" rel="noreferrer" className="inline-flex h-8 items-center gap-1 rounded-lg px-3 text-xs text-ink-2 hover:bg-panel-2">
                      Origineel <ExternalLink className="size-3" />
                    </a>
                  )}
                </div>
              }
            />
            <div className="p-5"><Transcript clip={clip} /></div>
          </Card>

          <Card>
            <CardHeader title="Prestaties na publicatie" subtitle="Log TikTok-resultaten; het systeem leert welke clips voor jou werken." action={<TrendingUp className="size-4 text-muted" />} />
            <div className="space-y-4 p-5">
              {!!clip.performance?.length && (
                <div className="overflow-x-auto">
                  <table className="w-full min-w-[560px] text-xs">
                    <thead>
                      <tr className="text-left text-muted">
                        <th className="py-1.5 font-medium">Datum</th><th className="font-medium">Platform</th><th className="text-right font-medium">Views</th>
                        <th className="text-right font-medium">Likes</th><th className="text-right font-medium">Shares</th><th className="text-right font-medium">Completion</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-line">
                      {clip.performance.map((p) => (
                        <tr key={p.id}>
                          <td className="py-2">{formatDate(p.recorded_at)}</td>
                          <td>{p.post_url ? <a href={p.post_url} target="_blank" rel="noreferrer" className="hover:text-fire">{p.platform}</a> : p.platform}</td>
                          <td className="text-right tabular-nums">{compactNumber(p.views)}</td>
                          <td className="text-right tabular-nums">{compactNumber(p.likes)}</td>
                          <td className="text-right tabular-nums">{compactNumber(p.shares)}</td>
                          <td className="text-right tabular-nums">{pct(p.completion_rate)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <PerformanceForm clip={clip} onSaved={() => mutate()} />
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}
