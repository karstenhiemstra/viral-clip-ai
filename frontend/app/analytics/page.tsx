"use client";

import { Brain, ChartColumn, Lightbulb } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { ColumnChart, ScatterChart } from "@/components/charts";
import { errorText, useToast } from "@/components/toast";
import { Button, Card, CardHeader, EmptyState, PageHeader, Spinner } from "@/components/ui";
import { api, useApi } from "@/lib/api";
import { CATEGORY_LABELS, compactNumber, formatDate, money, pct } from "@/lib/format";
import type { Analytics } from "@/lib/types";

function Tile({ label, value, hint }: { label: string; value: React.ReactNode; hint?: React.ReactNode }) {
  return (
    <Card className="p-5">
      <p className="text-xs text-muted">{label}</p>
      <p className="mt-2 text-3xl font-semibold text-ink">{value}</p>
      {hint && <p className="mt-1 text-xs text-muted">{hint}</p>}
    </Card>
  );
}

export default function AnalyticsPage() {
  const toast = useToast();
  const { data, mutate } = useApi<Analytics>("/api/analytics", { refreshInterval: 30000 });
  const [training, setTraining] = useState(false);

  if (!data) return <div className="flex justify-center py-24"><Spinner /></div>;

  async function train() {
    setTraining(true);
    try {
      const r = await api<{ trained: boolean; message?: string; version?: number }>("/api/analytics/train", { method: "POST" });
      toast(r.trained ? `Persoonlijk model v${r.version} getraind` : r.message ?? "Nog te weinig data", r.trained ? "ok" : "error");
      mutate();
    } catch (e) {
      toast(errorText(e), "error");
    } finally {
      setTraining(false);
    }
  }

  const published = data.performance_points.length;
  const viewsList = data.performance_points.map((p) => p.views).filter((v): v is number => v != null);
  const avgViews = viewsList.length ? viewsList.reduce((a, b) => a + b, 0) / viewsList.length : null;
  const positiveRated = (data.ratings.viral ?? 0) + (data.ratings.good ?? 0);

  // cost per day (sum over providers)
  const byDay = new Map<string, number>();
  for (const c of data.costs) byDay.set(c.day, (byDay.get(c.day) ?? 0) + c.cost_usd);
  const costData = [...byDay.entries()].sort().map(([day, cost]) => ({
    label: new Date(day).toLocaleDateString("nl-NL", { day: "numeric", month: "short" }),
    value: cost,
    detail: data.costs.filter((c) => c.day === day && c.cost_usd > 0).map((c) => `${c.provider}: ${money(c.cost_usd)}`).join(" · "),
  }));
  const maxViews = Math.max(0, ...data.categories.map((c) => c.avg_views ?? 0));

  return (
    <div className="space-y-6">
      <PageHeader
        title="Analytics"
        subtitle="Welke soorten clips werken voor jou? Gebaseerd op je feedback en de TikTok-resultaten die je logt."
        actions={<Button variant="fire" icon={<Brain className="size-4" />} loading={training} onClick={train}>Leermodel trainen</Button>}
      />

      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <Tile label="Beoordeelde clips" value={data.rated_count} hint={`${positiveRated} als 🔥/👍 · ${data.ratings.reject ?? 0} afgewezen`} />
        <Tile label="Gepubliceerd met statistieken" value={published} />
        <Tile label="Gemiddelde views" value={compactNumber(avgViews)} hint="Laatste meting per clip" />
        <Tile
          label="Persoonlijk model"
          value={data.model ? `v${data.model.version}` : "—"}
          hint={data.model ? `${data.model.n_samples} clips · ${formatDate(data.model.created_at)}` : `Vanaf ${data.min_samples} beoordeelde clips`}
        />
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader title="Voorspelt de Viral Score jouw oordeel?" subtitle="Aandeel clips per scoregroep dat je als 🔥 Viral of 👍 Good beoordeelde" />
          <div className="p-5">
            {data.calibration.some((b) => b.positive_rate != null) ? (
              <ColumnChart
                ariaLabel="Aandeel positief beoordeelde clips per Viral Score-groep"
                data={data.calibration.map((b) => ({ label: b.bucket, value: b.positive_rate, detail: `${b.clips} clips` }))}
                format={(v) => pct(v)}
              />
            ) : (
              <EmptyState icon={<ChartColumn className="size-5" />} title="Nog geen beoordelingen" text="Geef clips 🔥/👍/👎 om te zien of hogere scores ook betere clips zijn." />
            )}
          </div>
        </Card>
        <Card>
          <CardHeader title="Viral Score vs. echte views" subtitle="Elke stip is een gepubliceerde clip (views op log-schaal)" />
          <div className="p-5">
            {data.performance_points.some((p) => p.views != null) ? (
              <ScatterChart
                ariaLabel="Viral Score tegen views per gepubliceerde clip"
                points={data.performance_points.filter((p) => p.views != null).map((p) => ({
                  x: p.viral_score, y: p.views!, title: p.title ?? `Clip ${p.clip_id}`, sub: p.creator ?? undefined, href: `/clips/${p.clip_id}`,
                }))}
                xLabel="Viral Score"
                yLabel="Views"
                formatY={(v) => compactNumber(v)}
              />
            ) : (
              <EmptyState icon={<ChartColumn className="size-5" />} title="Nog geen gepubliceerde clips" text="Log views en completion rate op de clip-pagina na het posten op TikTok." />
            )}
          </div>
        </Card>
      </div>

      <Card>
        <CardHeader title="Prestaties per type clip" subtitle="Humor, controverse, verhalen, reacties, …" />
        {data.categories.length === 0 ? (
          <EmptyState icon={<ChartColumn className="size-5" />} title="Nog geen clips" />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[720px] text-sm">
              <thead>
                <tr className="border-b border-line text-left text-[11px] tracking-wide text-muted uppercase">
                  <th className="px-5 py-2.5 font-medium">Categorie</th>
                  <th className="px-3 py-2.5 text-right font-medium">Clips</th>
                  <th className="px-3 py-2.5 text-right font-medium">Gem. score</th>
                  <th className="px-3 py-2.5 text-right font-medium">Positief beoordeeld</th>
                  <th className="px-3 py-2.5 font-medium">Gem. views</th>
                  <th className="px-5 py-2.5 text-right font-medium">Completion</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {data.categories.map((c) => (
                  <tr key={c.category}>
                    <td className="px-5 py-2.5 font-medium">
                      <Link href={`/clips`} className="hover:text-fire">{CATEGORY_LABELS[c.category] ?? c.category}</Link>
                    </td>
                    <td className="px-3 py-2.5 text-right tabular-nums text-ink-2">{c.clips}</td>
                    <td className="px-3 py-2.5 text-right tabular-nums text-ink-2">{c.avg_score ?? "—"}</td>
                    <td className="px-3 py-2.5 text-right tabular-nums text-ink-2">{c.rated ? `${pct(c.positive_rate)} (${c.rated})` : "—"}</td>
                    <td className="px-3 py-2.5">
                      {c.avg_views != null ? (
                        <div className="flex items-center gap-2">
                          <div className="h-2 w-28 overflow-hidden rounded-full bg-panel-3">
                            <div className="h-full rounded-full" style={{ width: `${(c.avg_views / (maxViews || 1)) * 100}%`, background: "#3987e5" }} />
                          </div>
                          <span className="tabular-nums text-ink-2">{compactNumber(c.avg_views)}</span>
                        </div>
                      ) : (
                        <span className="text-muted">—</span>
                      )}
                    </td>
                    <td className="px-5 py-2.5 text-right tabular-nums text-ink-2">{pct(c.avg_completion)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader title="Wat werkt voor jou" subtitle="Inzichten van het persoonlijke leermodel" action={<Lightbulb className="size-4 text-muted" />} />
          <div className="p-5">
            {data.model?.insights.length ? (
              <ul className="space-y-3">
                {data.model.insights.map((i) => (
                  <li key={i.feature} className="flex gap-3 text-sm">
                    <span className={`mt-1.5 size-2 shrink-0 rounded-full ${i.effect > 0 ? "bg-ok" : "bg-bad"}`} />
                    <span className="text-ink-2">{i.text} <span className="text-muted">(n={i.samples})</span></span>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-sm text-muted">
                {data.rated_count < data.min_samples
                  ? `Beoordeel nog ${data.min_samples - data.rated_count} clips (of log TikTok-statistieken) om het persoonlijke model te trainen.`
                  : "Train het model om inzichten te zien."}
              </p>
            )}
            {data.model && (
              <p className="mt-4 text-[11px] text-muted">
                Cross-validatie correlatie: {data.model.metrics.cv_correlation ?? "n.v.t. (<20 clips)"} · het model stuurt scores maximaal ±12 punten bij, afhankelijk van de betrouwbaarheid.
              </p>
            )}
          </div>
        </Card>
        <Card>
          <CardHeader title="AI-kosten per dag" subtitle="Laatste 30 dagen, alle providers samen" />
          <div className="p-5">
            {costData.length ? (
              <ColumnChart ariaLabel="AI-kosten per dag in dollars" data={costData} format={(v) => money(v)} capLabels={costData.length <= 10} height={200} />
            ) : (
              <p className="text-sm text-muted">Nog geen kosten gemaakt. In heuristische modus is analyse gratis.</p>
            )}
          </div>
        </Card>
      </div>
    </div>
  );
}
