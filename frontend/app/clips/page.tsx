"use client";

import { Scissors } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { ClipCard } from "@/components/clips";
import { Card, EmptyState, PageHeader, Select, Spinner } from "@/components/ui";
import { useApi } from "@/lib/api";
import type { Clip, Creator } from "@/lib/types";

function ClipsInner() {
  const router = useRouter();
  const params = useSearchParams();
  const get = (k: string, d = "") => params.get(k) ?? d;
  const qs = new URLSearchParams({ limit: "120", sort: get("sort", "score") });
  for (const k of ["creator_id", "video_id", "status", "rating"]) if (get(k)) qs.set(k, get(k));
  if (get("min_score")) qs.set("min_score", get("min_score"));
  if (get("since_hours")) qs.set("since_hours", get("since_hours"));
  if (get("rating") === "reject") qs.set("include_rejected", "true");
  const { data, isLoading } = useApi<{ items: Clip[]; total: number }>(`/api/clips?${qs}`, { refreshInterval: 10000 });
  const { data: creators } = useApi<Creator[]>("/api/creators");

  function setParam(key: string, value: string) {
    const next = new URLSearchParams(params.toString());
    if (value) next.set(key, value);
    else next.delete(key);
    router.replace(`/clips?${next}`);
  }

  return (
    <div>
      <PageHeader title="Clips" subtitle={data ? `${data.total} clips` : "Alle gegenereerde clips"} />
      <div className="mb-5 flex flex-wrap gap-2">
        <Select value={get("creator_id")} onChange={(e) => setParam("creator_id", e.target.value)}>
          <option value="">Alle creators</option>
          {creators?.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
        </Select>
        <Select value={get("min_score")} onChange={(e) => setParam("min_score", e.target.value)}>
          <option value="">Elke score</option>
          <option value="85">🔥 85+</option>
          <option value="70">70+</option>
          <option value="55">55+</option>
        </Select>
        <Select value={get("since_hours")} onChange={(e) => setParam("since_hours", e.target.value)}>
          <option value="">Altijd</option>
          <option value="24">Afgelopen 24 uur</option>
          <option value="168">Afgelopen 7 dagen</option>
          <option value="720">Afgelopen 30 dagen</option>
        </Select>
        <Select value={get("rating")} onChange={(e) => setParam("rating", e.target.value)}>
          <option value="">Alle beoordelingen</option>
          <option value="none">Nog niet beoordeeld</option>
          <option value="viral">🔥 Viral</option>
          <option value="good">👍 Good</option>
          <option value="bad">👎 Bad</option>
          <option value="reject">❌ Afgewezen</option>
        </Select>
        <Select value={get("status")} onChange={(e) => setParam("status", e.target.value)}>
          <option value="">Elke status</option>
          <option value="ready">Klaar om te downloaden</option>
          <option value="awaiting_media">Wacht op bronvideo</option>
          <option value="pending_render">Wacht op render</option>
          <option value="failed">Render mislukt</option>
        </Select>
        <Select value={get("sort", "score")} onChange={(e) => setParam("sort", e.target.value)}>
          <option value="score">Sorteer: Viral Score</option>
          <option value="date">Sorteer: Nieuwste</option>
        </Select>
      </div>
      {isLoading && !data ? (
        <div className="flex justify-center py-24"><Spinner /></div>
      ) : !data?.items.length ? (
        <Card><EmptyState icon={<Scissors className="size-5" />} title="Geen clips gevonden" text="Pas de filters aan of wacht tot de volgende analyse klaar is." /></Card>
      ) : (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4 xl:grid-cols-6">
          {data.items.map((c) => <ClipCard key={c.id} clip={c} />)}
        </div>
      )}
    </div>
  );
}

export default function ClipsPage() {
  return (
    <Suspense fallback={<div className="flex justify-center py-24"><Spinner /></div>}>
      <ClipsInner />
    </Suspense>
  );
}
