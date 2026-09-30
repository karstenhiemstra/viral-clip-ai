"use client";

import { ExternalLink, Plus, RefreshCw, Search, SlidersHorizontal, Trash2, Users } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { ScoreBadge } from "@/components/clips";
import { errorText, useToast } from "@/components/toast";
import { Badge, Button, Card, EmptyState, ErrorNote, Field, Input, Modal, PageHeader, Select, Spinner, StatusDot, Toggle } from "@/components/ui";
import { api, useApi } from "@/lib/api";
import { compactNumber, relativeTime, type Tone } from "@/lib/format";
import type { ChannelResult, Creator, Priority } from "@/lib/types";

const PRIORITY: Record<Priority, { label: string; tone: Tone }> = {
  high: { label: "High", tone: "fire" },
  normal: { label: "Normal", tone: "neutral" },
  low: { label: "Low", tone: "muted" },
};

function scanState(c: Creator): { tone: Tone; label: string } {
  if (c.scan_job_status === "running") return { tone: "info", label: "Scant nu…" };
  if (c.scan_job_status === "queued") return { tone: "info", label: "Scan gepland" };
  if (!c.last_scanned_at) return { tone: "muted", label: "Nog niet gescand" };
  if (c.last_scan_status === "error") return { tone: "danger", label: "Fout bij scan" };
  if (c.last_scan_status === "warning") return { tone: "warning", label: "Scan met waarschuwing" };
  return { tone: "success", label: "OK" };
}

function Avatar({ src, name, size = 36 }: { src: string | null; name: string; size?: number }) {
  return src ? (
    // eslint-disable-next-line @next/next/no-img-element
    <img src={src} alt="" width={size} height={size} className="shrink-0 rounded-full object-cover" style={{ width: size, height: size }} />
  ) : (
    <span className="flex shrink-0 items-center justify-center rounded-full bg-panel-3 text-xs font-bold text-ink-2" style={{ width: size, height: size }}>
      {name.slice(0, 1).toUpperCase()}
    </span>
  );
}

function AddCreatorModal({ open, onClose, onAdded }: { open: boolean; onClose: () => void; onAdded: () => void }) {
  const toast = useToast();
  const [q, setQ] = useState("");
  const [results, setResults] = useState<ChannelResult[] | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [searching, setSearching] = useState(false);
  const [adding, setAdding] = useState<string | null>(null);
  const [priority, setPriority] = useState<Priority>("normal");
  const [language, setLanguage] = useState("nl");

  async function search(e?: React.FormEvent) {
    e?.preventDefault();
    if (!q.trim()) return;
    setSearching(true);
    setError(null);
    try {
      const res = await api<{ results: ChannelResult[]; note?: string }>(`/api/creators/search?q=${encodeURIComponent(q)}&language=${language}`);
      setResults(res.results);
      setNote(res.note ?? null);
    } catch (err) {
      setError(err);
      setResults(null);
    } finally {
      setSearching(false);
    }
  }

  async function add(ch: ChannelResult) {
    setAdding(ch.channel_id);
    try {
      await api("/api/creators", { method: "POST", json: { channel_id: ch.channel_id, priority, language } });
      toast(`${ch.title} toegevoegd — eerste scan is gestart`);
      onAdded();
      setResults((r) => r?.map((x) => (x.channel_id === ch.channel_id ? { ...x, already_added: true } : x)) ?? null);
    } catch (err) {
      toast(errorText(err), "error");
    } finally {
      setAdding(null);
    }
  }

  return (
    <Modal open={open} onClose={onClose} title="Creator toevoegen">
      <form onSubmit={search} className="flex gap-2">
        <Input autoFocus placeholder="Enzo Knol, @Bankzitters of kanaal-URL" value={q} onChange={(e) => setQ(e.target.value)} />
        <Button type="submit" variant="fire" loading={searching} icon={<Search className="size-4" />}>
          Zoek
        </Button>
      </form>
      <div className="mt-3 grid grid-cols-2 gap-3">
        <Field label="Prioriteit">
          <Select value={priority} onChange={(e) => setPriority(e.target.value as Priority)} className="w-full">
            <option value="high">High</option>
            <option value="normal">Normal</option>
            <option value="low">Low</option>
          </Select>
        </Field>
        <Field label="Taal">
          <Select value={language} onChange={(e) => setLanguage(e.target.value)} className="w-full">
            <option value="nl">Nederlands</option>
            <option value="en">Engels</option>
            <option value="de">Duits</option>
          </Select>
        </Field>
      </div>
      <div className="mt-4 space-y-2">
        <ErrorNote error={error} />
        {note && <p className="text-xs text-warn">{note}</p>}
        {results?.length === 0 && <p className="py-6 text-center text-sm text-muted">Geen kanalen gevonden.</p>}
        {results?.map((ch) => (
          <div key={ch.channel_id} className="flex items-center gap-3 rounded-xl border border-line bg-panel-2 p-3">
            <Avatar src={ch.thumbnail_url} name={ch.title} size={44} />
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-semibold">{ch.title}</p>
              <p className="truncate text-xs text-muted">
                {ch.handle ?? ch.channel_id}
                {ch.subscriber_count != null && ` · ${compactNumber(ch.subscriber_count)} subscribers`}
                {ch.video_count != null && ` · ${compactNumber(ch.video_count)} video's`}
              </p>
            </div>
            {ch.already_added ? (
              <Badge tone="success">Toegevoegd</Badge>
            ) : (
              <Button size="sm" variant="primary" loading={adding === ch.channel_id} onClick={() => add(ch)}>
                Kies
              </Button>
            )}
          </div>
        ))}
        {!results && !error && (
          <p className="pt-2 text-xs leading-relaxed text-muted">
            Tip: een @handle of kanaal-URL kost 1 YouTube-quota-unit, zoeken op naam 100 units (dagelijks budget: 10.000).
          </p>
        )}
      </div>
    </Modal>
  );
}

function CreatorSettingsModal({ creator, onClose, onSaved }: { creator: Creator | null; onClose: () => void; onSaved: () => void }) {
  const toast = useToast();
  const [form, setForm] = useState<Partial<Creator>>({});
  const [saving, setSaving] = useState(false);
  const c = creator ? { ...creator, ...form } : null;
  if (!c) return null;
  const num = (v: string) => (v === "" ? null : Number(v));

  async function save() {
    if (!creator) return;
    setSaving(true);
    try {
      await api(`/api/creators/${creator.id}`, { method: "PATCH", json: form });
      toast("Instellingen opgeslagen");
      setForm({});
      onSaved();
      onClose();
    } catch (err) {
      toast(errorText(err), "error");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal open={!!creator} onClose={() => { setForm({}); onClose(); }} title={`Instellingen · ${c.name}`}>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Prioriteit" hint="Bepaalt de volgorde in de analysewachtrij.">
          <Select value={c.priority} onChange={(e) => setForm({ ...form, priority: e.target.value as Priority })} className="w-full">
            <option value="high">High</option>
            <option value="normal">Normal</option>
            <option value="low">Low</option>
          </Select>
        </Field>
        <Field label="Taal">
          <Select value={c.language} onChange={(e) => setForm({ ...form, language: e.target.value })} className="w-full">
            <option value="nl">Nederlands</option>
            <option value="en">Engels</option>
            <option value="de">Duits</option>
          </Select>
        </Field>
        <Field label="Clipduur min (sec)" hint="Leeg = globale instelling">
          <Input type="number" min={3} value={c.clip_min_seconds ?? ""} onChange={(e) => setForm({ ...form, clip_min_seconds: num(e.target.value) })} />
        </Field>
        <Field label="Clipduur max (sec)">
          <Input type="number" min={5} value={c.clip_max_seconds ?? ""} onChange={(e) => setForm({ ...form, clip_max_seconds: num(e.target.value) })} />
        </Field>
        <Field label="Max clips per video">
          <Input type="number" min={1} max={30} value={c.max_clips_per_video ?? ""} onChange={(e) => setForm({ ...form, max_clips_per_video: num(e.target.value) })} />
        </Field>
        <Field label="Minimale videolengte (min)">
          <Input type="number" min={0} value={c.min_video_minutes ?? ""} onChange={(e) => setForm({ ...form, min_video_minutes: num(e.target.value) })} />
        </Field>
      </div>
      <div className="mt-4 space-y-1 border-t border-line pt-4">
        <Toggle checked={c.scan_enabled} onChange={(v) => setForm({ ...form, scan_enabled: v })} label="Automatisch scannen" hint="Nieuwe uploads worden periodiek gecontroleerd (interval in Settings)." />
        <Toggle checked={c.auto_analyze} onChange={(v) => setForm({ ...form, auto_analyze: v })} label="Nieuwe video's automatisch analyseren" />
      </div>
      <div className="mt-5 flex justify-end gap-2">
        <Button onClick={onClose}>Annuleren</Button>
        <Button variant="fire" loading={saving} onClick={save}>Opslaan</Button>
      </div>
    </Modal>
  );
}

export default function CreatorsPage() {
  const toast = useToast();
  const { data, mutate, isLoading } = useApi<Creator[]>("/api/creators", { refreshInterval: 8000 });
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<Creator | null>(null);

  async function scan(c: Creator) {
    try {
      await api(`/api/creators/${c.id}/scan`, { method: "POST" });
      toast(`Scan van ${c.name} gestart`);
      mutate();
    } catch (e) {
      toast(errorText(e), "error");
    }
  }
  async function scanAll() {
    try {
      const r = await api<{ queued: number }>("/api/creators/scan-all", { method: "POST" });
      toast(`${r.queued} scans gestart`);
      mutate();
    } catch (e) {
      toast(errorText(e), "error");
    }
  }
  async function remove(c: Creator) {
    if (!confirm(`${c.name} verwijderen? Gevonden video's en clips blijven bewaard.`)) return;
    await api(`/api/creators/${c.id}`, { method: "DELETE" });
    toast(`${c.name} verwijderd`);
    mutate();
  }

  return (
    <div>
      <PageHeader
        title="Creators"
        subtitle="Kanalen die automatisch op nieuwe uploads worden gecontroleerd."
        actions={
          <>
            {!!data?.length && <Button icon={<RefreshCw className="size-4" />} onClick={scanAll}>Alles scannen</Button>}
            <Button variant="fire" icon={<Plus className="size-4" />} onClick={() => setAdding(true)}>Creator toevoegen</Button>
          </>
        }
      />
      <Card>
        {isLoading && !data ? (
          <div className="flex justify-center py-16"><Spinner /></div>
        ) : !data?.length ? (
          <EmptyState
            icon={<Users className="size-5" />}
            title="Nog geen creators"
            text="Voeg bijvoorbeeld Enzo Knol, Bankzitters, Hanwe, Gio of StukTV toe. ViralClip vindt het kanaal en controleert nieuwe uploads automatisch."
            action={<Button variant="fire" icon={<Plus className="size-4" />} onClick={() => setAdding(true)}>Creator toevoegen</Button>}
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[860px] text-sm">
              <thead>
                <tr className="border-b border-line text-left text-[11px] tracking-wide text-muted uppercase">
                  <th className="px-5 py-3 font-medium">Creator</th>
                  <th className="px-3 py-3 font-medium">Laatste video</th>
                  <th className="px-3 py-3 text-right font-medium">Nieuwe video&apos;s</th>
                  <th className="px-3 py-3 font-medium">Laatste scan</th>
                  <th className="px-3 py-3 font-medium">Prioriteit</th>
                  <th className="px-3 py-3 text-right font-medium">Clips</th>
                  <th className="px-5 py-3" />
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {data.map((c) => {
                  const st = scanState(c);
                  return (
                    <tr key={c.id} className="transition hover:bg-panel-2/60">
                      <td className="px-5 py-3">
                        <div className="flex items-center gap-3">
                          <Avatar src={c.thumbnail_url} name={c.name} />
                          <div className="min-w-0">
                            <Link href={`/videos?creator_id=${c.id}`} className="block truncate font-semibold hover:text-fire">{c.name}</Link>
                            <a href={c.channel_url} target="_blank" rel="noreferrer" className="flex items-center gap-1 text-xs text-muted hover:text-ink">
                              {c.handle ?? "YouTube-kanaal"} {c.subscriber_count != null && `· ${compactNumber(c.subscriber_count)} subs`}
                              <ExternalLink className="size-3" />
                            </a>
                          </div>
                        </div>
                      </td>
                      <td className="max-w-[220px] px-3 py-3">
                        <p className="truncate text-ink-2">{c.last_video_title ?? "—"}</p>
                        <p className="text-xs text-muted">{c.last_video_published_at ? relativeTime(c.last_video_published_at) : ""}</p>
                      </td>
                      <td className="px-3 py-3 text-right tabular-nums">
                        <span className="font-semibold">{c.last_scan_new_videos}</span>
                        {!!c.pending_videos && <span className="block text-xs text-muted">{c.pending_videos} in behandeling</span>}
                      </td>
                      <td className="px-3 py-3">
                        <div className="flex items-center gap-2" title={c.last_scan_error ?? st.label}>
                          <StatusDot tone={st.tone} />
                          <span className="text-ink-2">{relativeTime(c.last_scanned_at)}</span>
                        </div>
                        {st.tone !== "success" && <p className="mt-0.5 text-xs text-muted">{st.label}</p>}
                      </td>
                      <td className="px-3 py-3"><Badge tone={PRIORITY[c.priority].tone}>{PRIORITY[c.priority].label}</Badge></td>
                      <td className="px-3 py-3 text-right">
                        <div className="flex items-center justify-end gap-2">
                          <span className="tabular-nums text-ink-2">{c.clip_count ?? 0}</span>
                          {c.best_score != null && <ScoreBadge score={c.best_score} />}
                        </div>
                      </td>
                      <td className="px-5 py-3">
                        <div className="flex justify-end gap-1">
                          <Button size="sm" variant="ghost" title="Nu scannen" onClick={() => scan(c)}><RefreshCw className="size-3.5" /></Button>
                          <Button size="sm" variant="ghost" title="Instellingen" onClick={() => setEditing(c)}><SlidersHorizontal className="size-3.5" /></Button>
                          <Button size="sm" variant="ghost" title="Verwijderen" onClick={() => remove(c)}><Trash2 className="size-3.5" /></Button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <AddCreatorModal open={adding} onClose={() => setAdding(false)} onAdded={() => mutate()} />
      <CreatorSettingsModal creator={editing} onClose={() => setEditing(null)} onSaved={() => mutate()} />
    </div>
  );
}
