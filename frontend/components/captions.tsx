"use client";

import { Crosshair, Eye, Merge, Play, Plus, RefreshCw, RotateCcw, Save, Scissors, Trash2, X } from "lucide-react";
import { type RefObject, useEffect, useRef, useState } from "react";

import { errorText, useToast } from "@/components/toast";
import { Button, Card, CardHeader, cx, Input, Spinner } from "@/components/ui";
import { api } from "@/lib/api";
import type { CaptionCue, Clip, ClipCaptions } from "@/lib/types";

type Row = CaptionCue & { key: number };

const MIN_CUE = 0.1;
const MAX_CHARS = 200;
const round2 = (t: number) => Math.round(t * 100) / 100;

/** 00:01.8 */
export function cueTime(t: number): string {
  if (!Number.isFinite(t)) return "--:--.-";
  const s = Math.max(0, t);
  const m = Math.floor(s / 60);
  return `${String(m).padStart(2, "0")}:${(s - m * 60).toFixed(1).padStart(4, "0")}`;
}

/** Split a caption at the cursor (snapped to the nearest space) or in the middle; time is shared by text length. */
export function splitCue(cue: CaptionCue, cursor?: number): [CaptionCue, CaptionCue] | null {
  const text = cue.text;
  let at = -1;
  if (cursor !== undefined && cursor > 0 && cursor < text.length) {
    for (let d = 0; d < text.length; d++) {
      if (text[cursor - d] === " ") { at = cursor - d; break; }
      if (text[cursor + d] === " ") { at = cursor + d; break; }
    }
  }
  let left = at > 0 ? text.slice(0, at).trim() : "";
  let right = at > 0 ? text.slice(at).trim() : "";
  if (!left || !right) {
    const words = text.trim().split(/\s+/).filter(Boolean);
    if (words.length < 2) return null;
    const mid = Math.ceil(words.length / 2);
    left = words.slice(0, mid).join(" ");
    right = words.slice(mid).join(" ");
  }
  const t = round2(cue.start + ((cue.end - cue.start) * left.length) / (left.length + right.length));
  return [{ start: cue.start, end: t, text: left }, { start: t, end: cue.end, text: right }];
}

export function mergeCues(a: CaptionCue, b: CaptionCue): CaptionCue {
  return { start: Math.min(a.start, b.start), end: Math.max(a.end, b.end), text: `${a.text.trim()} ${b.text.trim()}`.trim() };
}

/** A new empty caption at moment t (in the first free spot from t on, else after the last caption). */
export function newCueAt(cues: CaptionCue[], t: number, duration: number): CaptionCue | null {
  const sorted = [...cues].sort((a, b) => a.start - b.start);
  const fit = (from: number) => {
    let start = Math.min(Math.max(0, from), duration);
    for (const c of sorted) if (c.start <= start && c.end > start) start = c.end;
    const next = sorted.find((c) => c.start >= start);
    const end = Math.min(duration, start + 1.5, next ? next.start : duration);
    return end - start >= 0.2 ? { start: round2(start), end: round2(end), text: "" } : null;
  };
  return fit(t) ?? fit(sorted.length ? sorted[sorted.length - 1].end : 0);
}

/** Same rules as the server: text present, 0 <= start < end <= duration, no overlap with the previous one. */
export function cueErrors(cues: CaptionCue[], duration: number): (string | null)[] {
  return cues.map((c, i) => {
    if (!c.text.trim()) return "Lege caption: typ tekst of verwijder hem";
    if (c.text.trim().length > MAX_CHARS) return `Te lang (max. ${MAX_CHARS} tekens): splits hem op`;
    if (!Number.isFinite(c.start) || !Number.isFinite(c.end)) return "Vul een geldige start- en eindtijd in";
    if (c.start < 0) return "De starttijd kan niet negatief zijn";
    if (c.end > duration + 0.05) return `De eindtijd ligt na het einde van de clip (${cueTime(duration)})`;
    if (c.end - c.start < MIN_CUE) return "De eindtijd moet na de starttijd liggen";
    const prev = cues[i - 1];
    if (prev && prev.end - c.start > 0.15) return "Overlapt met de vorige caption: laat die eerder eindigen";
    if (prev && c.start < prev.start) return "Begint vóór de vorige caption: pas de tijden aan";
    return null;
  });
}

const plain = (rows: Row[]): CaptionCue[] => rows.map(({ start, end, text }) => ({ start, end, text }));

export function CaptionEditor({
  clip,
  videoRef,
  onPreview,
  onRendered,
  onClose,
}: {
  clip: Clip;
  videoRef: RefObject<HTMLVideoElement | null>;
  onPreview: (url: string) => void;
  onRendered: () => void;
  onClose: () => void;
}) {
  const toast = useToast();
  const nextKey = useRef(1);
  const cursor = useRef<Record<number, number>>({});
  const [rows, setRows] = useState<Row[] | null>(null);
  const [savedJson, setSavedJson] = useState("");
  const [meta, setMeta] = useState<{ duration: number; preset: string; custom: boolean }>({ duration: clip.duration, preset: "capcut", custom: false });
  const [busy, setBusy] = useState<"" | "preview" | "save" | "render" | "reset">("");
  const [now, setNow] = useState(0);
  const [focusKey, setFocusKey] = useState<number | null>(null);

  function load(data: ClipCaptions) {
    const loaded = data.captions.map((c) => ({ ...c, key: nextKey.current++ }));
    setRows(loaded);
    setSavedJson(JSON.stringify(plain(loaded)));
    setMeta({ duration: data.duration, preset: data.caption_preset, custom: data.custom });
  }

  useEffect(() => {
    api<ClipCaptions>(`/api/clips/${clip.id}/captions`).then(load).catch((e) => toast(errorText(e), "error"));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clip.id]);

  useEffect(() => {
    const id = window.setInterval(() => setNow(videoRef.current?.currentTime ?? 0), 200);
    return () => window.clearInterval(id);
  }, [videoRef]);

  if (!rows) return <Card className="flex justify-center p-6"><Spinner /></Card>;

  const duration = meta.duration;
  const errors = cueErrors(rows, duration);
  const valid = errors.every((e) => e === null);
  const dirty = JSON.stringify(plain(rows)) !== savedJson;
  const payload = () => ({ captions: plain(rows).sort((a, b) => a.start - b.start) });

  const update = (key: number, patch: Partial<CaptionCue>) => setRows(rows.map((r) => (r.key === key ? { ...r, ...patch } : r)));
  const remove = (key: number) => setRows(rows.filter((r) => r.key !== key));
  const at = () => round2(videoRef.current?.currentTime ?? 0);

  function seek(t: number) {
    const v = videoRef.current;
    if (!v) return;
    v.currentTime = t + 0.01;
    v.play().catch(() => undefined);
  }

  function split(i: number) {
    const parts = splitCue(rows![i], cursor.current[rows![i].key]);
    if (!parts) return toast("Een caption van één woord kun je niet splitsen", "error");
    const [a, b] = parts.map((p) => ({ ...p, key: nextKey.current++ }));
    setRows([...rows!.slice(0, i), a, b, ...rows!.slice(i + 1)]);
  }

  function mergeNext(i: number) {
    const merged = { ...mergeCues(rows![i], rows![i + 1]), key: nextKey.current++ };
    setRows([...rows!.slice(0, i), merged, ...rows!.slice(i + 2)]);
  }

  function add() {
    const cue = newCueAt(rows!, at(), duration);
    if (!cue) return toast("Geen ruimte meer: maak eerst een andere caption korter", "error");
    const row = { ...cue, key: nextKey.current++ };
    setRows([...rows!, row].sort((a, b) => a.start - b.start));
    setFocusKey(row.key);
  }

  async function run(kind: typeof busy, fn: () => Promise<void>) {
    setBusy(kind);
    try {
      await fn();
    } catch (e) {
      toast(errorText(e), "error");
    } finally {
      setBusy("");
    }
  }

  const preview = () =>
    run("preview", async () => {
      const r = await api<{ preview_url: string }>(`/api/clips/${clip.id}/captions/preview`, { method: "POST", json: payload() });
      onPreview(r.preview_url);
      toast("Preview klaar: speel de video af om je captions te zien");
    });

  const save = (render: boolean) =>
    run(render ? "render" : "save", async () => {
      load(await api<ClipCaptions>(`/api/clips/${clip.id}/captions`, { method: "PUT", json: payload() }));
      if (render) {
        await api(`/api/clips/${clip.id}/render`, { method: "POST" });
        onRendered();
        toast("Captions opgeslagen — de clip wordt nu opnieuw gerenderd");
      } else {
        toast("Captions opgeslagen (de download verandert na ‘Opslaan & clip renderen’)");
      }
    });

  const reset = () => {
    if (!window.confirm("Al je aanpassingen weggooien en de automatische captions terugzetten?")) return;
    run("reset", async () => {
      load(await api<ClipCaptions>(`/api/clips/${clip.id}/captions`, { method: "PUT", json: { captions: null } }));
      toast("Automatische captions teruggezet");
    });
  };

  const close = () => {
    if (dirty && !window.confirm("Je hebt wijzigingen die nog niet zijn opgeslagen. Toch sluiten?")) return;
    onClose();
  };

  return (
    <Card>
      <CardHeader
        title="Captions aanpassen"
        subtitle={meta.custom ? "Je eigen captions · stijl, kleur en positie blijven gelijk" : "Automatische captions · stijl, kleur en positie blijven gelijk"}
        action={<Button size="sm" variant="ghost" onClick={close} aria-label="Sluiten"><X className="size-4" /></Button>}
      />
      <div className="space-y-3 p-4">
        {meta.preset === "none" && (
          <p className="rounded-lg border border-warn/30 bg-warn/10 p-3 text-xs text-warn">
            Captions staan uit (stijl “Geen captions”). Kies hierboven bij Captions een stijl om ze in de clip te zien.
          </p>
        )}
        <div className="max-h-[560px] space-y-2 overflow-y-auto pr-1">
          {rows.length === 0 && <p className="py-4 text-center text-xs text-muted">Geen captions. Voeg er een toe met de knop hieronder.</p>}
          {rows.map((r, i) => {
            const active = now >= r.start && now < r.end;
            return (
              <div key={r.key} className={cx("space-y-2 rounded-xl border p-3", active ? "border-fire/60 bg-fire/5" : "border-line bg-panel-2", errors[i] && "border-bad/50")}>
                <div className="flex items-center gap-1">
                  <Button size="sm" variant="ghost" className="px-2" title="Afspelen vanaf hier" onClick={() => seek(r.start)}><Play className="size-3.5" /></Button>
                  <span className="font-mono text-xs tabular-nums text-ink-2">{cueTime(r.start)} → {cueTime(r.end)}</span>
                  <div className="ml-auto flex gap-0.5">
                    <Button size="sm" variant="ghost" className="px-2" title="Splitsen (bij de cursor, anders in het midden)" onClick={() => split(i)}><Scissors className="size-3.5" /></Button>
                    {i + 1 < rows.length && (
                      <Button size="sm" variant="ghost" className="px-2" title="Samenvoegen met de volgende caption" onClick={() => mergeNext(i)}><Merge className="size-3.5" /></Button>
                    )}
                    <Button size="sm" variant="ghost" className="px-2 hover:text-bad" title="Verwijderen" onClick={() => remove(r.key)}><Trash2 className="size-3.5" /></Button>
                  </div>
                </div>
                <textarea
                  rows={2}
                  value={r.text}
                  autoFocus={focusKey === r.key}
                  placeholder="Typ de tekst van deze caption"
                  onChange={(e) => update(r.key, { text: e.target.value })}
                  onSelect={(e) => { cursor.current[r.key] = e.currentTarget.selectionStart; }}
                  className="w-full resize-y rounded-lg border border-line bg-panel px-3 py-2 text-sm text-ink outline-none transition placeholder:text-muted focus:border-fire/60 focus:ring-2 focus:ring-fire/20"
                />
                <div className="grid grid-cols-2 gap-2 text-[11px] text-muted">
                  {(["start", "end"] as const).map((k) => (
                    <label key={k} className="space-y-1">
                      <span>{k === "start" ? "Verschijnt (s)" : "Verdwijnt (s)"}</span>
                      <div className="flex gap-1">
                        <Input
                          type="number"
                          step="0.1"
                          min={0}
                          max={duration}
                          className="h-8 px-2 text-xs"
                          value={Number.isFinite(r[k]) ? r[k] : ""}
                          onChange={(e) => update(r.key, { [k]: e.target.value === "" ? Number.NaN : round2(Number(e.target.value)) })}
                        />
                        <Button size="sm" variant="secondary" className="px-2" title="Zet op het huidige moment in de video" onClick={() => update(r.key, { [k]: at() })}>
                          <Crosshair className="size-3.5" />
                        </Button>
                      </div>
                    </label>
                  ))}
                </div>
                {errors[i] && <p className="text-[11px] text-bad">{errors[i]}</p>}
              </div>
            );
          })}
        </div>
        <Button className="w-full" variant="secondary" icon={<Plus className="size-4" />} onClick={add}>Caption toevoegen (op het huidige moment)</Button>
        {!valid && <p className="text-[11px] text-bad">Los eerst de rode meldingen op.</p>}
        <div className="grid grid-cols-2 gap-2">
          <Button variant="secondary" loading={busy === "preview"} disabled={!valid || !!busy} icon={<Eye className="size-4" />} onClick={preview}>Preview</Button>
          <Button variant="secondary" loading={busy === "save"} disabled={!valid || !!busy || !dirty} icon={<Save className="size-4" />} onClick={() => save(false)}>Opslaan</Button>
        </div>
        <Button className="w-full" variant="fire" loading={busy === "render"} disabled={!valid || !!busy} icon={<RefreshCw className="size-4" />} onClick={() => save(true)}>
          Opslaan & clip renderen
        </Button>
        <div className="flex items-center justify-between gap-2">
          <p className="text-[11px] leading-snug text-muted">
            {busy === "preview" ? "Preview maken… (een paar seconden)" : "Preview maakt snel een voorbeeld zonder AI of face tracking opnieuw. De download verandert pas na ‘Opslaan & clip renderen’."}
          </p>
          {meta.custom && (
            <Button size="sm" variant="ghost" loading={busy === "reset"} disabled={!!busy} icon={<RotateCcw className="size-3.5" />} onClick={reset} title="Automatische captions terugzetten">
              Automatisch
            </Button>
          )}
        </div>
      </div>
    </Card>
  );
}
