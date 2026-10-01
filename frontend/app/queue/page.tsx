"use client";

import { Hourglass, ListOrdered, RotateCcw, Trash2, X } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { errorText, useToast } from "@/components/toast";
import { Badge, Button, Card, cx, EmptyState, PageHeader, ProgressBar, Spinner } from "@/components/ui";
import { api, useApi } from "@/lib/api";
import { JOB_STATUS, JOB_TYPES, relativeTime } from "@/lib/format";
import type { Job } from "@/lib/types";

const FILTERS = [
  { key: "active", label: "Actief" },
  { key: "failed", label: "Mislukt" },
  { key: "completed", label: "Klaar" },
  { key: "all", label: "Alles" },
] as const;

export default function QueuePage() {
  const toast = useToast();
  const [filter, setFilter] = useState<(typeof FILTERS)[number]["key"]>("active");
  const { data, mutate, isLoading } = useApi<{ items: Job[]; counts: Record<string, number> }>(`/api/jobs?status=${filter}&limit=200`, { refreshInterval: 2000 });

  async function act(job: Job, action: "cancel" | "retry") {
    try {
      await api(`/api/jobs/${job.id}/${action}`, { method: "POST" });
      mutate();
    } catch (e) {
      toast(errorText(e), "error");
    }
  }
  async function clear() {
    const r = await api<{ deleted: number }>("/api/jobs/history", { method: "DELETE" });
    toast(`${r.deleted} afgeronde jobs opgeruimd`);
    mutate();
  }

  const counts = data?.counts ?? {};
  return (
    <div>
      <PageHeader
        title="Analysis Queue"
        subtitle="Scans, analyses en renders worden hier één voor één verwerkt door de worker."
        actions={<Button icon={<Trash2 className="size-4" />} onClick={clear}>Geschiedenis opruimen</Button>}
      />
      <div className="mb-4 flex flex-wrap items-center gap-2">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            onClick={() => setFilter(f.key)}
            className={cx("h-8 rounded-full border px-3 text-xs font-medium transition", filter === f.key ? "border-fire/40 bg-fire/10 text-fire" : "border-line text-muted hover:text-ink")}
          >
            {f.label}
          </button>
        ))}
        <span className="ml-auto text-xs text-muted">
          {counts.running ?? 0} bezig · {counts.queued ?? 0} wachtend · {counts.waiting ?? 0} wacht op input · {counts.failed ?? 0} mislukt
        </span>
      </div>
      <Card>
        {isLoading && !data ? (
          <div className="flex justify-center py-16"><Spinner /></div>
        ) : !data?.items.length ? (
          <EmptyState icon={<ListOrdered className="size-5" />} title="De wachtrij is leeg" text="Nieuwe video's van je creators komen hier automatisch langs." />
        ) : (
          <ol className="divide-y divide-line">
            {data.items.map((j, i) => {
              const st = JOB_STATUS[j.status] ?? { label: j.status, tone: "neutral" as const };
              return (
                <li key={j.id} className="flex flex-col gap-3 px-4 py-4 sm:flex-row sm:items-center sm:px-5">
                  <span className="hidden w-6 shrink-0 text-right text-xs text-muted tabular-nums sm:block">{i + 1}.</span>
                  {j.video_thumbnail ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img src={j.video_thumbnail} alt="" className="hidden aspect-video w-24 shrink-0 rounded-md object-cover sm:block" />
                  ) : (
                    <span className="hidden aspect-video w-24 shrink-0 rounded-md bg-panel-2 sm:block" />
                  )}
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center gap-2">
                      <Badge tone="muted">{JOB_TYPES[j.type] ?? j.type}</Badge>
                      <Badge tone={st.tone}>{j.status === "waiting" && <Hourglass className="size-3" />}{st.label}</Badge>
                      <span className="text-xs text-muted">{relativeTime(j.finished_at ?? j.started_at ?? j.created_at)}</span>
                    </div>
                    <p className="mt-1 truncate text-sm font-medium">
                      {j.video_id ? <Link href={`/videos/${j.video_id}`} className="hover:text-fire">{j.title}</Link> : j.title}
                    </p>
                    {j.status === "running" && <ProgressBar value={j.progress} className="mt-2 max-w-md" />}
                    {j.message && <p className={cx("mt-1 text-xs", j.status === "waiting" ? "text-warn" : "text-muted")}>{j.message}</p>}
                    {j.status === "failed" && j.error && (
                      <details className="mt-1 text-xs text-bad">
                        <summary className="cursor-pointer">{j.error.split("\n")[0].slice(0, 180)}</summary>
                        <pre className="mt-2 max-h-48 overflow-auto rounded bg-panel-2 p-2 whitespace-pre-wrap text-[11px] text-ink-2">{j.error}</pre>
                      </details>
                    )}
                  </div>
                  <div className="flex shrink-0 gap-1 sm:justify-end">
                    {j.status === "running" && <span className="mr-2 text-sm tabular-nums text-ink-2">{Math.round(j.progress)}%</span>}
                    {["queued", "waiting", "running"].includes(j.status) && (
                      <Button size="sm" variant="ghost" title="Annuleren" onClick={() => act(j, "cancel")}><X className="size-3.5" /></Button>
                    )}
                    {["failed", "cancelled", "completed"].includes(j.status) && (
                      <Button size="sm" variant="ghost" title="Opnieuw" onClick={() => act(j, "retry")}><RotateCcw className="size-3.5" /></Button>
                    )}
                  </div>
                </li>
              );
            })}
          </ol>
        )}
      </Card>
    </div>
  );
}
