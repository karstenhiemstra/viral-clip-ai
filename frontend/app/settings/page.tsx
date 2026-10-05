"use client";

import { Check, CircleAlert, KeyRound, RotateCcw, Save, Zap } from "lucide-react";
import { type ReactNode, useState } from "react";

import { errorText, useToast } from "@/components/toast";
import { Badge, Button, Card, CardHeader, cx, Field, Input, PageHeader, Select, Spinner, Toggle } from "@/components/ui";
import { api, useApi } from "@/lib/api";
import { DIMENSION_LABELS, LAYOUT_LABELS, PRESET_LABELS, presetValue, STAGE_LABELS } from "@/lib/format";
import type { SettingsPayload } from "@/lib/types";

type Settings = SettingsPayload["settings"];
type Section = keyof Settings;

const SECRET_INFO: Record<string, { label: string; hint: ReactNode }> = {
  youtube_api_key: {
    label: "YouTube Data API key (gratis, nodig)",
    hint: <>console.cloud.google.com → project maken → &quot;YouTube Data API v3&quot; inschakelen → Credentials → Create credentials → API key. Gratis quota: 10.000 units/dag.</>,
  },
  openai_api_key: {
    label: "OpenAI API key (aanbevolen, betaald)",
    hint: "platform.openai.com → API keys → Create new secret key, en zet tegoed op je account (Billing). Eén key doet transcriptie én de AI-analyse.",
  },
  anthropic_api_key: {
    label: "Anthropic API key (optioneel)",
    hint: "Alleen nodig als je Claude wilt i.p.v. OpenAI (console.anthropic.com). Let op: Anthropic kan niet transcriberen.",
  },
};

const QUALITY_INFO: Record<string, { label: string; hint: string }> = {
  budget: { label: "Budget", hint: "Goedkoopste model voor beide passes" },
  balanced: { label: "Gebalanceerd (aanbevolen)", hint: "Goedkoop model leest alles, slim model beoordeelt alleen de ~30 kandidaten" },
  best: { label: "Beste kwaliteit", hint: "Slim model denkt langer na over de kandidaten" },
};

function usd(range: unknown) {
  const [lo, hi] = (range as [number, number]) ?? [0, 0];
  const f = (v: number) => `$${v.toFixed(2).replace(".", ",")}`;
  return Math.abs(hi - lo) < 0.005 ? f(lo) : `${f(lo)}–${f(hi)}`;
}

function CostTable({ est }: { est: SettingsPayload["system"]["cost_estimate"] }) {
  const qualities = ["budget", "balanced", "best"];
  return (
    <div className="border-t border-line p-5">
      <div className="mb-2 text-sm font-medium text-ink">Geschatte AI-kosten per video ({est.provider === "anthropic" ? "Anthropic" : "OpenAI"})</div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[520px] text-sm">
          <thead>
            <tr className="text-left text-xs text-muted">
              <th className="py-1.5 pr-3 font-medium">Videolengte</th>
              {qualities.map((q) => (
                <th key={q} className={`py-1.5 pr-3 font-medium ${q === est.quality ? "text-ink" : ""}`}>
                  {QUALITY_INFO[q].label.replace(" (aanbevolen)", "")}{q === est.quality ? " • actief" : ""}
                </th>
              ))}
              <th className="py-1.5 font-medium">waarvan transcriptie</th>
            </tr>
          </thead>
          <tbody className="tabular-nums">
            {est.rows.map((r) => (
              <tr key={r.minutes} className="border-t border-line">
                <td className="py-1.5 pr-3 text-muted">{r.minutes} min</td>
                {qualities.map((q) => (
                  <td key={q} className={`py-1.5 pr-3 ${q === est.quality ? "font-semibold text-ink" : "text-muted"}`}>{usd(r[q])}</td>
                ))}
                <td className="py-1.5 text-muted">{usd([r.transcription, r.transcription])}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mt-2 text-xs text-muted">
        Schatting op basis van gemeten tokengebruik van deze app en de publieke prijslijst (sept. 2026). De bandbreedte komt door
        verborgen &quot;denk&quot;-tokens. Transcriptie telt alleen als je een video-/audiobestand aanlevert zonder ondertitels
        {est.whisper_api ? " (OpenAI Whisper: $0,006 per minuut)" : " (lokaal: gratis)"}. Een .srt uploaden maakt transcriptie gratis.
        De echte kosten per analyse zie je op de videopagina.
      </p>
    </div>
  );
}

const KEY_STATUS: Record<string, { tone: "success" | "danger" | "muted" | "warning"; label: string }> = {
  connected: { tone: "success", label: "Verbonden" },
  error: { tone: "danger", label: "Niet verbonden" },
  untested: { tone: "warning", label: "Nog niet getest" },
  missing: { tone: "muted", label: "Niet ingesteld" },
};

function SecretRow({ name, status, onSaved }: { name: string; status: SettingsPayload["secrets"][string]; onSaved: (p: SettingsPayload) => void }) {
  const toast = useToast();
  const info = SECRET_INFO[name];
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState<"save" | "test" | null>(null);
  const st = KEY_STATUS[status.status] ?? KEY_STATUS.missing;
  async function save(v: string | null) {
    setBusy("save");
    try {
      const p = await api<SettingsPayload>("/api/settings/secrets", { method: "POST", json: { name, value: v } });
      onSaved(p);
      setValue("");
      const after = p.secrets[name];
      if (!v) toast("API key verwijderd");
      else if (after?.status === "connected") toast("Key opgeslagen en verbonden ✓");
      else toast(`Key opgeslagen, maar de verbinding werkt nog niet: ${after?.message ?? "onbekende fout"}`, "error");
    } catch (e) {
      toast(errorText(e), "error");
    } finally {
      setBusy(null);
    }
  }
  async function runTest() {
    setBusy("test");
    try {
      const r = await api<{ ok: boolean; message: string; settings: SettingsPayload }>(`/api/settings/secrets/${name}/test`, { method: "POST" });
      onSaved(r.settings);
      toast(r.ok ? "Verbinding werkt ✓" : r.message, r.ok ? "ok" : "error");
    } catch (e) {
      toast(errorText(e), "error");
    } finally {
      setBusy(null);
    }
  }
  return (
    <div className="space-y-2 px-5 py-4">
      <div className="flex flex-wrap items-center gap-2">
        <KeyRound className="size-4 text-muted" />
        <span className="text-sm font-medium">{info.label}</span>
        <Badge tone={st.tone}>
          {status.status === "connected" && <Check className="size-3" />} {st.label}
        </Badge>
        {status.configured && <span className="text-xs text-muted">{status.masked} · {status.source === "ui" ? "ingevuld in de app" : "uit .env"}</span>}
      </div>
      <p className="text-xs text-muted">{info.hint}</p>
      {status.status === "error" && status.message && (
        <p className="rounded-lg border border-bad/30 bg-bad/10 px-3 py-2 text-xs text-bad">{status.message}</p>
      )}
      <div className="flex flex-wrap gap-2">
        <Input type="password" autoComplete="off" placeholder={status.configured ? "Nieuwe key om te vervangen" : "Plak hier je API key"} value={value} onChange={(e) => setValue(e.target.value)} className="min-w-[240px] flex-1" />
        <Button variant="primary" disabled={!value} loading={busy === "save"} onClick={() => save(value)}>Opslaan</Button>
        {status.configured && <Button onClick={runTest} loading={busy === "test"}>Verbinding testen</Button>}
        {status.configured && status.source === "ui" && <Button variant="ghost" onClick={() => save(null)}>Wissen</Button>}
      </div>
    </div>
  );
}

function Slider({ label, value, min, max, step, onChange, hint, format }: { label: string; value: number; min: number; max: number; step: number; onChange: (v: number) => void; hint?: string; format?: (v: number) => string }) {
  return (
    <label className="block space-y-1.5">
      <span className="flex items-center justify-between text-xs">
        <span className="text-ink-2" title={hint}>{label}</span>
        <span className="font-semibold tabular-nums">{format ? format(value) : value}</span>
      </span>
      <input type="range" min={min} max={max} step={step} value={value} onChange={(e) => onChange(Number(e.target.value))} className="w-full" />
    </label>
  );
}

function PresetPreview({ preset, active, onClick }: { preset: string; active: boolean; onClick: () => void }) {
  const outline = "[text-shadow:0_1px_0_#000,0_-1px_0_#000,1px_0_0_#000,-1px_0_0_#000,1px_2px_1px_rgba(0,0,0,0.5)]";
  const text = {
    capcut: (
      <span className={cx("text-[11px] font-extrabold tracking-tight text-white", outline)}>
        IK HEB <span className="rounded-[2px] bg-[#28A7F0] px-[3px] py-px">ALLES</span>
      </span>
    ),
    none: <span className="text-[10px] text-white/60">geen captions</span>,
  }[preset];
  return (
    <button type="button" onClick={onClick} className={cx("space-y-2 rounded-xl border p-2 text-left transition", active ? "border-fire/60 bg-fire/5" : "border-line hover:border-line-2")}>
      <div className="relative flex aspect-[9/16] items-end justify-center overflow-hidden rounded-lg bg-gradient-to-br from-[#3a2f52] via-[#1e2a3d] to-[#122018] pb-[30%]">
        <div className="absolute top-[18%] left-1/2 size-10 -translate-x-1/2 rounded-full bg-[#d9b59b]/70" />
        {text}
      </div>
      <p className="text-[11px] font-medium text-ink-2">{PRESET_LABELS[preset]}</p>
    </button>
  );
}

export default function SettingsPage() {
  const { data, mutate } = useApi<SettingsPayload>("/api/settings");
  if (!data) return <div className="flex justify-center py-24"><Spinner /></div>;
  return <SettingsForm data={data} mutate={(p) => mutate(p, { revalidate: false })} />;
}

function SettingsForm({ data, mutate }: { data: SettingsPayload; mutate: (p: SettingsPayload) => void }) {
  const toast = useToast();
  const [draft, setDraft] = useState<Settings>(() => structuredClone(data.settings));
  const [dirty, setDirty] = useState<Set<Section>>(new Set());
  const [saving, setSaving] = useState<Section | null>(null);

  function update<S extends Section>(section: S, patch: Partial<Settings[S]>) {
    setDraft((d) => ({ ...d, [section]: { ...d[section], ...patch } }));
    setDirty((s) => new Set(s).add(section));
  }

  async function save(section: Section) {
    setSaving(section);
    try {
      const p = await api<SettingsPayload>("/api/settings", { method: "PATCH", json: { [section]: draft[section] } });
      mutate(p);
      setDraft((d) => ({ ...d, [section]: p.settings[section] }));
      setDirty((s) => { const n = new Set(s); n.delete(section); return n; });
      toast("Instellingen opgeslagen");
    } catch (e) {
      toast(errorText(e), "error");
    } finally {
      setSaving(null);
    }
  }
  async function reset(section: Section) {
    const p = await api<SettingsPayload>(`/api/settings/reset/${section}`, { method: "POST" });
    mutate(p);
    setDraft((d) => ({ ...d, [section]: p.settings[section] }));
    setDirty((s) => { const n = new Set(s); n.delete(section); return n; });
    toast("Standaardwaarden hersteld");
  }
  const actions = (section: Section) => (
    <div className="flex gap-2">
      <Button size="sm" variant="ghost" icon={<RotateCcw className="size-3.5" />} onClick={() => reset(section)}>Standaard</Button>
      <Button size="sm" variant={dirty.has(section) ? "fire" : "secondary"} disabled={!dirty.has(section)} loading={saving === section} icon={<Save className="size-3.5" />} onClick={() => save(section)}>
        Opslaan
      </Button>
    </div>
  );
  const sys = data.system;
  const s = draft;

  return (
    <div className="space-y-6">
      <PageHeader title="Settings" subtitle="API keys, AI-modellen, scoring, clipduur, captions en automatisering." />

      <Card>
        <CardHeader title="Systeemstatus" />
        <div className="grid gap-3 p-5 text-sm sm:grid-cols-2 lg:grid-cols-4">
          {[
            ["Clipselectie", sys.llm.provider === "heuristic" ? "Gratis basisanalyse (geen AI-key)" : `AI: ${sys.llm.provider} · ${sys.llm.models.fast ?? ""} / ${sys.llm.models.smart ?? ""}`, sys.llm.provider !== "heuristic" && sys.llm.provider !== "error"],
            ["Transcriptie", sys.transcriber === "openai_whisper" ? "OpenAI Whisper" : sys.transcriber === "faster_whisper" ? "Lokaal (faster-whisper)" : sys.transcriber ? sys.transcriber : "Vul OpenAI-key in of upload .srt", !!sys.transcriber && !sys.transcriber.startsWith("error")],
            ["Video-bewerking", sys.ffmpeg ? "Klaar (ffmpeg)" : "ffmpeg ontbreekt", sys.ffmpeg],
            ["Gezicht volgen", sys.face_detector === "yunet" ? "Klaar (nauwkeurig model)" : sys.face_detector === "haar" ? "Klaar (standaardmodel)" : "Niet beschikbaar", sys.face_detector !== "none"],
          ].map(([label, value, ok]) => (
            <div key={label as string} className="rounded-xl border border-line bg-panel-2 p-3">
              <p className="flex items-center gap-1.5 text-xs text-muted">
                {ok ? <Check className="size-3 text-ok" /> : <CircleAlert className="size-3 text-warn" />} {label as string}
              </p>
              <p className="mt-1 truncate text-sm font-medium" title={value as string}>{value as string}</p>
            </div>
          ))}
        </div>
        {sys.llm.error && <p className="px-5 pb-4 text-xs text-bad">{sys.llm.error}</p>}
        <p className="px-5 pb-4 text-[11px] text-muted">Database: {sys.database} · Opslag: {sys.storage} · Tijdzone: {sys.timezone} · API-auth: {sys.auth_enabled ? "aan" : "uit"}</p>
      </Card>

      <Card>
        <CardHeader title="API keys" subtitle="Plak een key en klik Opslaan: de app test hem meteen. Keys worden versleuteld opgeslagen en nooit teruggestuurd naar de browser." />
        <div className="divide-y divide-line">
          {Object.keys(SECRET_INFO).map((name) => (
            <SecretRow key={name} name={name} status={data.secrets[name]} onSaved={(p) => mutate(p)} />
          ))}
        </div>
      </Card>

      <Card>
        <CardHeader
          title="AI & modellen"
          subtitle="Pass 1: goedkoop model leest het hele transcript. Pass 2: lokaal (gratis) de beste ~30 kiezen. Pass 3: slim model beoordeelt alleen die kandidaten."
          action={actions("ai")}
        />
        <div className="grid gap-4 p-5 sm:grid-cols-2 lg:grid-cols-3">
          <Field label="Kwaliteit / kosten" hint={QUALITY_INFO[s.ai.quality]?.hint}>
            <Select className="w-full" value={s.ai.quality} onChange={(e) => update("ai", { quality: e.target.value as SettingsPayload["settings"]["ai"]["quality"] })}>
              {Object.entries(QUALITY_INFO).map(([k, v]) => (
                <option key={k} value={k}>{v.label}</option>
              ))}
            </Select>
          </Field>
          <Field label="LLM-provider" hint="Automatisch = OpenAI als die key er is, anders Anthropic, anders de gratis basisanalyse.">
            <Select className="w-full" value={s.ai.llm_provider} onChange={(e) => update("ai", { llm_provider: e.target.value })}>
              <option value="auto">Automatisch</option>
              <option value="anthropic">Anthropic (Claude)</option>
              <option value="openai">OpenAI (of compatibel)</option>
              <option value="heuristic">Alleen gratis basisanalyse (geen AI)</option>
            </Select>
          </Field>
          <Field label="Snel model (pass 1)" hint="Leeg = volgt de kwaliteitskeuze (gpt-5-mini / claude-haiku-4-5)">
            <Input value={s.ai.model_fast} placeholder="standaard" onChange={(e) => update("ai", { model_fast: e.target.value })} />
          </Field>
          <Field label="Slim model (pass 3)" hint="Leeg = volgt de kwaliteitskeuze (gpt-5 / claude-sonnet-5-5; bij 'Beste' claude-opus-5-5)">
            <Input value={s.ai.model_smart} placeholder="standaard" onChange={(e) => update("ai", { model_smart: e.target.value })} />
          </Field>
          <Field label="Transcriptie">
            <Select className="w-full" value={s.ai.transcriber} onChange={(e) => update("ai", { transcriber: e.target.value })}>
              <option value="auto">Automatisch</option>
              <option value="openai">OpenAI Whisper API (~$0,006/min)</option>
              <option value="faster_whisper">Lokaal faster-whisper (gratis)</option>
              <option value="none">Uit (alleen SRT/VTT)</option>
            </Select>
          </Field>
          <Field label="Lokaal Whisper-model" hint="tiny/base/small/medium/large-v3 — groter = nauwkeuriger maar trager">
            <Input value={s.ai.faster_whisper_model} onChange={(e) => update("ai", { faster_whisper_model: e.target.value })} />
          </Field>
          <Field label="Taal van uitleg & titels">
            <Select className="w-full" value={s.ai.output_language} onChange={(e) => update("ai", { output_language: e.target.value })}>
              <option value="nl">Nederlands</option>
              <option value="en">Engels</option>
            </Select>
          </Field>
        </div>
        {data.system.llm.error && <div className="mx-5 mb-4 rounded-lg bg-bad/10 px-3 py-2 text-sm text-bad">{data.system.llm.error}</div>}
        {data.system.cost_estimate && <CostTable est={data.system.cost_estimate} />}
      </Card>

      <Card>
        <CardHeader title="Viral Score" subtitle="Score = gewogen funnel: Stop × Hold × Engage (meetkundig gemiddelde). Een zwakke hook kan niet worden goedgemaakt door deelbaarheid." action={actions("scoring")} />
        <div className="grid gap-6 p-5 lg:grid-cols-3">
          {Object.entries(data.meta.stages).map(([stage, dims]) => (
            <div key={stage} className="space-y-3 rounded-xl border border-line bg-panel-2 p-4">
              <Slider
                label={`${STAGE_LABELS[stage].label} — ${STAGE_LABELS[stage].hint}`}
                value={s.scoring.stage_weights[stage]}
                min={0} max={1} step={0.05}
                onChange={(v) => update("scoring", { stage_weights: { ...s.scoring.stage_weights, [stage]: v } })}
                format={(v) => v.toFixed(2)}
              />
              <div className="space-y-3 border-t border-line pt-3">
                {dims.map((d) => (
                  <Slider key={d} label={DIMENSION_LABELS[d] ?? d} value={s.scoring.weights[d]} min={0} max={3} step={0.1}
                    onChange={(v) => update("scoring", { weights: { ...s.scoring.weights, [d]: v } })} format={(v) => `×${v.toFixed(1)}`} />
                ))}
              </div>
            </div>
          ))}
        </div>
        <div className="grid gap-5 border-t border-line p-5 sm:grid-cols-3">
          <Slider label="Aandeel audio/tekst-signalen" hint="Hoe zwaar objectieve signalen (volume-pieken, comment-hotspots, hookwoorden) meewegen naast de AI" value={s.scoring.signal_blend} min={0} max={0.6} step={0.05} onChange={(v) => update("scoring", { signal_blend: v })} format={(v) => `${Math.round(v * 100)}%`} />
          <Slider label="Minimale Viral Score" value={s.scoring.min_viral_score} min={0} max={90} step={5} onChange={(v) => update("scoring", { min_viral_score: v })} />
          <Slider label="Invloed persoonlijk leermodel" hint="Hoeveel je eigen feedback/TikTok-resultaten de score mogen bijsturen (max ±12 punten)" value={s.scoring.personalization_strength} min={0} max={1} step={0.1} onChange={(v) => update("scoring", { personalization_strength: v })} format={(v) => `${Math.round(v * 100)}%`} />
        </div>
      </Card>

      <Card>
        <CardHeader title="Clips & captions" subtitle="Zo kort mogelijk, maar lang genoeg voor hook én payoff." action={actions("clips")} />
        <div className="grid gap-4 p-5 sm:grid-cols-2 lg:grid-cols-4">
          <Field label="Minimale duur (sec)" hint="Clips duren altijd 10–15 seconden"><Input type="number" min={10} max={15} value={s.clips.min_seconds} onChange={(e) => update("clips", { min_seconds: Number(e.target.value) })} /></Field>
          <Field label="Maximale duur (sec)"><Input type="number" min={10} max={15} value={s.clips.max_seconds} onChange={(e) => update("clips", { max_seconds: Number(e.target.value) })} /></Field>
          <Field label="Doelduur (sec)"><Input type="number" min={10} max={15} value={s.clips.target_seconds} onChange={(e) => update("clips", { target_seconds: Number(e.target.value) })} /></Field>
          <Field label="Max clips per video"><Input type="number" min={1} max={30} value={s.clips.max_per_video} onChange={(e) => update("clips", { max_per_video: Number(e.target.value) })} /></Field>
        </div>
        <div className="grid max-w-3xl grid-cols-2 gap-3 px-5 sm:grid-cols-4">
          {data.meta.caption_presets.map((p) => (
            <PresetPreview key={p} preset={p} active={presetValue(s.clips.caption_preset) === p} onClick={() => update("clips", { caption_preset: p })} />
          ))}
        </div>
        <div className="grid gap-4 p-5 sm:grid-cols-2">
          <Field label="9:16 reframing">
            <Select className="w-full" value={s.clips.layout} onChange={(e) => update("clips", { layout: e.target.value })}>
              {data.meta.layouts.map((l) => <option key={l} value={l}>{LAYOUT_LABELS[l] ?? l}</option>)}
            </Select>
          </Field>
          <Field label="Stiltes langer dan (sec) inkorten">
            <Input type="number" step={0.1} min={0.2} max={3} value={s.clips.silence_min_gap} onChange={(e) => update("clips", { silence_min_gap: Number(e.target.value) })} />
          </Field>
          <Toggle checked={s.clips.remove_silences} onChange={(v) => update("clips", { remove_silences: v })} label="Dode lucht verwijderen (jump cuts)" hint="Korte clips met hoger tempo houden kijkers langer vast." />
          <Toggle checked={s.clips.add_hook_title} onChange={(v) => update("clips", { add_hook_title: v })} label="Hook-titel in beeld (eerste 3 sec)" hint="Brandt de AI-titel bovenin de eerste seconden in." />
        </div>
      </Card>

      <Card>
        <CardHeader title="Automatische discovery" subtitle="Welke nieuwe video's automatisch worden geanalyseerd." action={actions("discovery")} />
        <div className="grid gap-4 p-5 sm:grid-cols-2 lg:grid-cols-4">
          <Field label="Periode">
            <Select className="w-full" value={s.discovery.period} onChange={(e) => update("discovery", { period: e.target.value })}>
              <option value="today">Vandaag</option>
              <option value="24h">Afgelopen 24 uur</option>
              <option value="7d">Afgelopen 7 dagen</option>
              <option value="30d">Afgelopen 30 dagen</option>
              <option value="custom">Aangepaste periode</option>
              <option value="all">Alles</option>
            </Select>
          </Field>
          {s.discovery.period === "custom" && (
            <>
              <Field label="Van"><Input type="date" value={s.discovery.period_start ?? ""} onChange={(e) => update("discovery", { period_start: e.target.value || null })} /></Field>
              <Field label="Tot"><Input type="date" value={s.discovery.period_end ?? ""} onChange={(e) => update("discovery", { period_end: e.target.value || null })} /></Field>
            </>
          )}
          <Field label="Aantal video's per scan">
            <Select className="w-full" value={String(s.discovery.max_videos_per_scan)} onChange={(e) => update("discovery", { max_videos_per_scan: Number(e.target.value) })}>
              {[5, 10, 25, 50].map((n) => <option key={n} value={n}>{n}</option>)}
              <option value="0">Alles</option>
            </Select>
          </Field>
          <Field label="Minimale videolengte (min)">
            <Select className="w-full" value={String(s.discovery.min_video_minutes)} onChange={(e) => update("discovery", { min_video_minutes: Number(e.target.value) })}>
              {[0, 3, 5, 10, 20, 30].map((n) => <option key={n} value={n}>{n === 0 ? "Geen" : `${n} minuten`}</option>)}
            </Select>
          </Field>
          <Field label="Scan-interval">
            <Select className="w-full" value={String(s.discovery.scan_interval_minutes)} onChange={(e) => update("discovery", { scan_interval_minutes: Number(e.target.value) })}>
              {[30, 60, 120, 240, 720, 1440].map((n) => <option key={n} value={n}>Elke {n < 60 ? `${n} min` : `${n / 60} uur`}</option>)}
            </Select>
          </Field>
          <Field label="Minimaal aantal views"><Input type="number" min={0} value={s.discovery.min_views} onChange={(e) => update("discovery", { min_views: Number(e.target.value) })} /></Field>
          <Field label="Maximale lengte (min, 0 = geen)"><Input type="number" min={0} value={s.discovery.max_video_minutes} onChange={(e) => update("discovery", { max_video_minutes: Number(e.target.value) })} /></Field>
          <Field label="Titels uitsluiten (komma's)">
            <Input value={s.discovery.title_exclude_keywords.join(", ")} placeholder="trailer, livestream" onChange={(e) => update("discovery", { title_exclude_keywords: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) })} />
          </Field>
        </div>
        <div className="grid gap-x-8 gap-y-1 border-t border-line p-5 sm:grid-cols-2">
          <Toggle checked={s.discovery.auto_scan} onChange={(v) => update("discovery", { auto_scan: v })} label="Automatisch scannen" hint="Worker controleert creators volgens het interval." />
          <Toggle checked={s.discovery.fetch_comments} onChange={(v) => update("discovery", { fetch_comments: v })} label="Comment-hotspots ophalen" hint="Tijdstempels in YouTube-comments (1 quota-unit per video)." />
          <Toggle checked={s.discovery.exclude_shorts} onChange={(v) => update("discovery", { exclude_shorts: v })} label="Shorts overslaan" />
          <Toggle checked={s.discovery.exclude_live} onChange={(v) => update("discovery", { exclude_live: v })} label="Livestreams/premières overslaan" />
        </div>
      </Card>

      <Card>
        <CardHeader title="Pipeline & kosten" subtitle="Meer kandidaten = betere recall maar meer tokens in pass 2." action={actions("pipeline")} />
        <div className="grid gap-5 p-5 sm:grid-cols-2">
          <Slider label="Kandidaten voor gedetailleerde analyse" value={s.pipeline.candidate_count} min={5} max={60} step={1} onChange={(v) => update("pipeline", { candidate_count: v })} />
          <Slider label="Kandidaten per AI-batch" value={s.pipeline.detail_batch_size} min={1} max={15} step={1} onChange={(v) => update("pipeline", { detail_batch_size: v })} />
          <Toggle checked={s.pipeline.auto_analyze} onChange={(v) => update("pipeline", { auto_analyze: v })} label="Nieuwe video's automatisch analyseren" />
          <Toggle checked={s.pipeline.auto_render} onChange={(v) => update("pipeline", { auto_render: v })} label="Top clips automatisch renderen" />
          <Toggle checked={s.pipeline.scene_detection} onChange={(v) => update("pipeline", { scene_detection: v })} label="Camerawissels detecteren" hint="Gratis (ffmpeg); helpt reframing en scoring." />
          <Toggle checked={s.pipeline.use_embeddings} onChange={(v) => update("pipeline", { use_embeddings: v })} label="Embeddings voor duplicate-detectie" hint="Alleen OpenAI; anders lokale TF-IDF." />
          <Toggle checked={s.pipeline.use_vision} onChange={(v) => update("pipeline", { use_vision: v })} label="Vision-analyse (beta)" hint="Laat een vision-model frames van alleen de beste kandidaten beoordelen." />
          {s.pipeline.use_vision && (
            <Slider label="Vision voor top N kandidaten" value={s.pipeline.vision_top_n} min={1} max={20} step={1} onChange={(v) => update("pipeline", { vision_top_n: v })} />
          )}
        </div>
        <p className="flex items-center gap-1.5 border-t border-line px-5 py-3 text-[11px] text-muted"><Zap className="size-3" /> Kosten per video worden per analyse bijgehouden; zie de video-detailpagina en Analytics.</p>
      </Card>
    </div>
  );
}
