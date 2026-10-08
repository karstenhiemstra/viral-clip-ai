"use client";

import { ArrowDown, ArrowUp, Download, Music, RefreshCw, SlidersHorizontal, Trash2, Upload, Wand2 } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";

import { errorText, useToast } from "@/components/toast";
import { Badge, Button, Card, CardHeader, EmptyState, Field, Input, PageHeader, ProgressBar, Select, Spinner, Toggle } from "@/components/ui";
import { api, uploadFile, useApi } from "@/lib/api";
import { formatDuration, relativeTime } from "@/lib/format";
import type { AutoEdit, EditShot, EditSource, EditTransition } from "@/lib/types";

const STYLES = [
  ["auto", "Automatisch"],
  ["hype", "Hype"],
  ["cinematic", "Cinematic"],
  ["fast", "Fast / Aggressive"],
  ["clean", "Clean"],
  ["football", "Football Edit"],
] as const;
const EXAMPLES = ["Maak een edit van Neymar", "Maak een snelle voetbal edit van Mbappé", "Maak een hype edit van Ronaldo", "Maak een cinematic edit van Messi"];
const TRANSITIONS: Record<EditTransition, string> = { cut: "Harde cut", flash: "Flits", zoom: "Zoom punch", whip: "Whip pan", glitch: "Glitch", dip: "Fade (zwart)" };
const EFFECT_LABELS: Record<string, string> = {
  camera_shake: "camera shake", colour_grade: "kleurgrading", fade_in_out: "fade in/uit", freeze_frame: "freeze frame",
  motion_blur: "motion blur", slow_motion: "slow motion", speed_ramp: "speed ramp", letterbox: "cinema-balken",
  "transition:flash": "flits", "transition:zoom": "zoom punch", "transition:whip": "whip pan", "transition:glitch": "glitch",
  "transition:dip": "fade", "zoom:in": "zoom in", "zoom:out": "zoom uit", "zoom:punch": "punch-in",
};

type Sources = { videos: EditSource[]; music: { name: string; size: number }[] };

function UploadButton({ label, accept, path, extra, onDone }: { label: string; accept: string; path: string; extra?: Record<string, string>; onDone: () => void }) {
  const toast = useToast();
  const ref = useRef<HTMLInputElement>(null);
  const [progress, setProgress] = useState<number | null>(null);
  async function pick(file: File | undefined) {
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    Object.entries(extra ?? {}).forEach(([k, v]) => v && form.append(k, v));
    setProgress(0);
    try {
      await uploadFile(path, form, setProgress);
      toast(`${file.name} toegevoegd`);
      onDone();
    } catch (e) {
      toast(errorText(e), "error");
    } finally {
      setProgress(null);
      if (ref.current) ref.current.value = "";
    }
  }
  return (
    <>
      <input ref={ref} type="file" accept={accept} className="hidden" onChange={(e) => pick(e.target.files?.[0])} />
      <Button size="sm" variant="secondary" icon={<Upload className="size-3.5" />} loading={progress !== null} onClick={() => ref.current?.click()}>
        {progress !== null ? `${Math.round(progress * 100)}%` : label}
      </Button>
    </>
  );
}

function ShotEditor({ edit, onSaved }: { edit: AutoEdit; onSaved: (e: AutoEdit) => void }) {
  const toast = useToast();
  const [shots, setShots] = useState<EditShot[]>(edit.plan?.shots ?? []);
  const [music, setMusic] = useState(edit.plan?.music_enabled ?? false);
  const [saving, setSaving] = useState(false);
  const titles = Object.fromEntries(edit.sources.map((s) => [s.id, s.title]));
  const set = (i: number, patch: Partial<EditShot>) => setShots((all) => all.map((s, k) => (k === i ? { ...s, ...patch } : s)));
  const move = (i: number, d: number) =>
    setShots((all) => {
      const j = i + d;
      if (j < 0 || j >= all.length) return all;
      const next = [...all];
      [next[i], next[j]] = [next[j], next[i]];
      return next;
    });
  const total = shots.filter((s) => s.enabled).reduce((a, s) => a + s.out, 0);
  async function save() {
    setSaving(true);
    try {
      onSaved(await api<AutoEdit>(`/api/edits/${edit.id}/plan`, { method: "PUT", json: { shots, music } }));
      toast("Aanpassingen opgeslagen — de edit wordt opnieuw gerenderd");
    } catch (e) {
      toast(errorText(e), "error");
    } finally {
      setSaving(false);
    }
  }
  return (
    <Card className="space-y-3 p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-sm font-semibold">Edit aanpassen <span className="font-normal text-muted">· {shots.length} clips · {total.toFixed(1)} s</span></p>
        {edit.plan?.music ? <Toggle checked={music} onChange={setMusic} label={`Muziek (${edit.plan.music.file})`} /> : <span className="text-xs text-muted">Geen muziek in deze edit</span>}
      </div>
      <div className="space-y-2">
        {shots.map((s, i) => (
          <div key={i} className={`grid grid-cols-[auto_1fr] items-center gap-x-3 gap-y-2 rounded-lg border border-line p-2 text-xs sm:grid-cols-[auto_1.2fr_7.5rem_8rem_auto_auto] ${s.enabled ? "" : "opacity-50"}`}>
            <span className="font-mono text-muted">#{i + 1}</span>
            <span className="truncate" title={titles[s.video_id]}>{titles[s.video_id] ?? `Video ${s.video_id}`} · {formatDuration(s.start)}</span>
            <label className="flex items-center gap-1">Duur<Input className="w-20 min-w-20" type="number" step={0.1} min={0.3} max={8} value={s.out} onChange={(e) => set(i, { out: Number(e.target.value) })} />s</label>
            <Select value={s.transition} onChange={(e) => set(i, { transition: e.target.value as EditTransition })}>
              {Object.entries(TRANSITIONS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </Select>
            <div className="flex flex-wrap gap-1">
              {([["zoom", "Zoom"], ["shake", "Shake"], ["slow", "Slow-mo"], ["freeze", "Freeze"]] as const).map(([key, label]) => {
                const on = key === "zoom" ? !!s.zoom : key === "shake" ? s.shake : key === "slow" ? s.speed < 0.9 || s.ramp : s.freeze > 0;
                const toggle = () =>
                  set(i, key === "zoom" ? { zoom: s.zoom ? null : "in" } : key === "shake" ? { shake: !s.shake } : key === "slow" ? { speed: on ? 1 : 0.5, ramp: false } : { freeze: on ? 0 : Math.min(0.4, s.out / 3) });
                return (
                  <button key={key} type="button" onClick={toggle} className={`rounded-md border px-2 py-1 ${on ? "border-fire/50 bg-fire/10 text-ink" : "border-line text-muted"}`}>{label}</button>
                );
              })}
            </div>
            <div className="flex gap-1">
              <Button size="sm" variant="ghost" aria-label="Omhoog" onClick={() => move(i, -1)}><ArrowUp className="size-3.5" /></Button>
              <Button size="sm" variant="ghost" aria-label="Omlaag" onClick={() => move(i, 1)}><ArrowDown className="size-3.5" /></Button>
              <Button size="sm" variant="ghost" aria-label="Verwijderen" onClick={() => setShots((all) => all.filter((_, k) => k !== i))}><Trash2 className="size-3.5" /></Button>
            </div>
          </div>
        ))}
      </div>
      <Button variant="fire" className="w-full" loading={saving} disabled={!shots.length} icon={<RefreshCw className="size-4" />} onClick={save}>Opnieuw renderen met deze aanpassingen</Button>
    </Card>
  );
}

function Result({ edit, onChange }: { edit: AutoEdit; onChange: (e: AutoEdit) => void }) {
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const busy = edit.status === "queued" || edit.status === "rendering";
  async function regenerate() {
    try {
      onChange(await api<AutoEdit>(`/api/edits/${edit.id}/regenerate`, { method: "POST" }));
      setEditing(false);
    } catch (e) {
      toast(errorText(e), "error");
    }
  }
  return (
    <div className="space-y-4">
      <Card className="p-4">
        <CardHeader
          title={edit.prompt}
          subtitle={`Stijl: ${edit.style_label}${edit.style_auto ? " (automatisch gekozen)" : ""} · ${edit.sources.map((s) => s.title).join(", ")}`}
        />
        {busy && (
          <div className="mt-4 space-y-2">
            <p className="flex items-center gap-2 text-sm"><Spinner /> Edit wordt gemaakt… {edit.stage ? <span className="text-muted">{edit.stage}</span> : null}</p>
            <ProgressBar value={edit.progress} />
          </div>
        )}
        {edit.status === "failed" && <p className="mt-4 rounded-lg border border-bad/30 bg-bad/10 p-3 text-sm text-bad">{edit.error}</p>}
        {edit.status === "ready" && edit.video_url && (
          <div className="mt-4 grid gap-4 sm:grid-cols-[minmax(0,320px)_1fr]">
            <div className="mx-auto aspect-[9/16] w-full max-w-[320px] overflow-hidden rounded-2xl border border-line bg-black">
              <video key={edit.video_url} src={edit.video_url} poster={edit.thumbnail_url ?? undefined} className="size-full" controls playsInline autoPlay muted loop />
            </div>
            <div className="space-y-3 text-sm">
              <p className="text-muted">{edit.result.duration?.toFixed(1)} s · {edit.result.shots} clips{edit.result.music ? ` · muziek: ${edit.result.music} (${edit.plan?.music?.bpm} BPM, cuts op de beat)` : " · origineel geluid"}</p>
              <div className="flex flex-wrap gap-1">{(edit.result.effects ?? []).map((e) => <Badge key={e}>{EFFECT_LABELS[e] ?? e}</Badge>)}</div>
              <div className="flex flex-wrap gap-2">
                <Button variant="fire" icon={<RefreshCw className="size-4" />} onClick={regenerate}>Opnieuw genereren</Button>
                <a href={edit.download_url ?? "#"} className="inline-flex h-10 items-center gap-2 rounded-lg border border-line bg-panel-2 px-4 text-sm font-medium hover:bg-panel-3"><Download className="size-4" /> Download</a>
                <Button icon={<SlidersHorizontal className="size-4" />} onClick={() => setEditing((v) => !v)}>Edit aanpassen</Button>
              </div>
            </div>
          </div>
        )}
        {edit.status === "failed" && <div className="mt-3"><Button icon={<RefreshCw className="size-4" />} onClick={regenerate}>Opnieuw proberen</Button></div>}
      </Card>
      {editing && edit.status === "ready" && edit.plan && <ShotEditor key={edit.version} edit={edit} onSaved={(e) => { setEditing(false); onChange(e); }} />}
    </div>
  );
}

function EditInner() {
  const router = useRouter();
  const params = useSearchParams();
  const currentId = params.get("id");
  const [prompt, setPrompt] = useState("");
  const [style, setStyle] = useState("auto");
  const [duration, setDuration] = useState(15);
  const [music, setMusic] = useState(true);
  const [picking, setPicking] = useState(false);
  const [picked, setPicked] = useState<number[]>([]);
  const [footageTitle, setFootageTitle] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { data: list, mutate: refreshList } = useApi<{ edits: AutoEdit[] }>("/api/edits", { refreshInterval: 5000 });
  const { data: sources, mutate: refreshSources } = useApi<Sources>("/api/edits/sources");
  const { data: current, mutate: setCurrent } = useApi<AutoEdit>(currentId ? `/api/edits/${currentId}` : null, {
    refreshInterval: (d) => (d && (d.status === "ready" || d.status === "failed") ? 0 : 1500),
  });
  useEffect(() => {
    if (current?.status === "ready") refreshList();
  }, [current?.status, refreshList]);

  async function create() {
    setCreating(true);
    setError(null);
    try {
      const e = await api<AutoEdit>("/api/edits", {
        method: "POST",
        json: { prompt, style, duration, music, video_ids: picking && picked.length ? picked : null },
      });
      router.replace(`/edit?id=${e.id}`);
      setCurrent(e, { revalidate: true });
      refreshList();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader title="Auto Edit" subtitle="Typ wat je wilt editen — de edit wordt lokaal gemaakt met FFmpeg van jouw eigen beeldmateriaal. Geen AI-kosten, geen transcriptie." />
      <Card className="space-y-4 p-4">
        <Field label="Wat wil je editen?">
          <Input value={prompt} placeholder="Maak een edit van Neymar" onChange={(e) => setPrompt(e.target.value)} onKeyDown={(e) => e.key === "Enter" && prompt.trim() && create()} />
        </Field>
        <div className="flex flex-wrap gap-1.5">
          {EXAMPLES.map((x) => <button key={x} type="button" onClick={() => setPrompt(x)} className="rounded-full border border-line px-3 py-1 text-xs text-ink-2 hover:border-line-2 hover:text-ink">{x}</button>)}
        </div>
        <div className="grid gap-3 sm:grid-cols-3">
          <Field label="Stijl" hint="Automatisch: de editor kiest zelf (of zeg het in je opdracht, bijv. 'cinematic')">
            <Select className="w-full" value={style} onChange={(e) => setStyle(e.target.value)}>{STYLES.map(([k, v]) => <option key={k} value={k}>{v}</option>)}</Select>
          </Field>
          <Field label="Duur">
            <Select className="w-full" value={duration} onChange={(e) => setDuration(Number(e.target.value))}>{[10, 15, 20, 30].map((d) => <option key={d} value={d}>{d} seconden</option>)}</Select>
          </Field>
          <Field label="Muziek" hint={sources?.music.length ? `${sources.music.length} nummer(s) beschikbaar` : "Nog geen muziek: dan origineel geluid"}>
            <Toggle checked={music} onChange={setMusic} label="Muziek gebruiken (cuts op de beat)" />
          </Field>
        </div>
        <div className="space-y-2">
          <Toggle checked={picking} onChange={setPicking} label="Kies zelf video's" hint="Anders zoekt de editor video's met de naam in de titel" />
          {picking && (
            <div className="grid max-h-52 gap-1 overflow-auto rounded-lg border border-line p-2 text-sm sm:grid-cols-2">
              {(sources?.videos ?? []).map((v) => (
                <label key={v.id} className="flex items-center gap-2">
                  <input type="checkbox" checked={picked.includes(v.id)} onChange={(e) => setPicked((p) => (e.target.checked ? [...p, v.id] : p.filter((x) => x !== v.id)))} />
                  <span className="truncate">{v.title}</span>
                  {v.duration ? <span className="text-xs text-muted">{formatDuration(v.duration)}</span> : null}
                </label>
              ))}
              {!sources?.videos.length && <p className="text-muted">Nog geen video&apos;s met beeld.</p>}
            </div>
          )}
        </div>
        {error && (
          <div className="rounded-lg border border-warn/40 bg-warn/10 p-3 text-sm">
            {error} <Link href="/videos" className="underline">Naar Video&apos;s</Link>
          </div>
        )}
        <Button variant="fire" className="w-full sm:w-auto" icon={<Wand2 className="size-4" />} loading={creating} disabled={!prompt.trim()} onClick={create}>Maak edit</Button>
      </Card>

      {current && <Result edit={current} onChange={(e) => { setCurrent(e, { revalidate: true }); refreshList(); }} />}

      <div className="grid gap-4 lg:grid-cols-2">
        <Card className="space-y-3 p-4">
          <p className="text-sm font-semibold">Beeldmateriaal toevoegen</p>
          <p className="text-xs text-muted">Video&apos;s die je hier toevoegt worden alleen voor Auto Edit gebruikt (er start geen clip-analyse, dus geen kosten). Zet de naam in de titel, bijvoorbeeld &quot;Neymar skills&quot;.</p>
          <div className="flex flex-wrap items-center gap-2">
            <Input className="max-w-xs" placeholder="Titel (bijv. Neymar skills)" value={footageTitle} onChange={(e) => setFootageTitle(e.target.value)} />
            <UploadButton label="Video kiezen" accept="video/*" path="/api/edits/footage" extra={{ title: footageTitle }} onDone={() => { setFootageTitle(""); refreshSources(); }} />
          </div>
          <p className="text-xs text-muted">{sources?.videos.length ?? 0} video&apos;s met beeld beschikbaar (ook alle video&apos;s uit de pagina Video&apos;s).</p>
        </Card>
        <Card className="space-y-3 p-4">
          <p className="flex items-center gap-2 text-sm font-semibold"><Music className="size-4" /> Muziek</p>
          <p className="text-xs text-muted">Gebruik alleen muziek die je mag gebruiken (eigen of rechtenvrij). Bestanden komen in de map data/music.</p>
          <div className="space-y-1 text-sm">
            {(sources?.music ?? []).map((m) => (
              <div key={m.name} className="flex items-center justify-between gap-2">
                <span className="truncate">{m.name}</span>
                <Button size="sm" variant="ghost" aria-label="Verwijderen" onClick={async () => { await api(`/api/edits/music/${encodeURIComponent(m.name)}`, { method: "DELETE" }); refreshSources(); }}><Trash2 className="size-3.5" /></Button>
              </div>
            ))}
          </div>
          <UploadButton label="Muziek toevoegen" accept="audio/*" path="/api/edits/music" onDone={() => refreshSources()} />
        </Card>
      </div>

      <Card className="p-4">
        <p className="mb-3 text-sm font-semibold">Eerdere edits</p>
        {list?.edits.length ? (
          <div className="grid gap-3 sm:grid-cols-3 lg:grid-cols-5">
            {list.edits.map((e) => (
              <button key={e.id} type="button" onClick={() => router.replace(`/edit?id=${e.id}`)} className="space-y-1 text-left">
                <div className="aspect-[9/16] overflow-hidden rounded-xl border border-line bg-panel-2">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  {e.thumbnail_url ? <img src={e.thumbnail_url} alt="" className="size-full object-cover" /> : <div className="flex size-full items-center justify-center text-xs text-muted">{e.status === "failed" ? "Mislukt" : "Bezig…"}</div>}
                </div>
                <p className="truncate text-xs">{e.prompt}</p>
                <p className="text-[11px] text-muted">{e.style_label} · {relativeTime(e.created_at)}</p>
              </button>
            ))}
          </div>
        ) : (
          <EmptyState icon={<Wand2 className="size-5" />} title="Nog geen edits" text="Typ hierboven een opdracht, bijvoorbeeld 'Maak een edit van Neymar'." />
        )}
      </Card>
    </div>
  );
}

export default function EditPage() {
  return (
    <Suspense fallback={<Spinner />}>
      <EditInner />
    </Suspense>
  );
}
