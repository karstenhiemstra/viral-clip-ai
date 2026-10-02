"use client";

import { Ban, Download, Flame, Hourglass, Play, ThumbsDown, ThumbsUp, TriangleAlert } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { errorText, useToast } from "@/components/toast";
import { Badge, cx } from "@/components/ui";
import { api } from "@/lib/api";
import { CATEGORY_LABELS, CLIP_STATUS, formatDuration, scoreTone } from "@/lib/format";
import type { Clip, Rating } from "@/lib/types";

const TONE_CLASSES = {
  hot: "fire-gradient text-white shadow-[0_6px_20px_-6px_rgba(255,70,60,0.8)]",
  good: "bg-warn text-black",
  moderate: "bg-info/90 text-black",
  low: "bg-panel-3 text-ink-2",
};

export function ScoreBadge({ score, size = "sm", className }: { score: number; size?: "sm" | "lg"; className?: string }) {
  const tone = scoreTone(score);
  return (
    <span
      className={cx(
        "inline-flex items-center gap-1 rounded-full font-bold tabular-nums",
        size === "lg" ? "px-3.5 py-1.5 text-lg" : "px-2 py-0.5 text-xs",
        TONE_CLASSES[tone],
        className,
      )}
      title="Viral Score: voorspelde potentie (0-100), geen garantie"
    >
      {tone === "hot" && <Flame className={size === "lg" ? "size-4.5" : "size-3"} strokeWidth={2.5} />}
      {Math.round(score)}
    </span>
  );
}

export function ClipThumb({ clip, className }: { clip: Clip; className?: string }) {
  const [hover, setHover] = useState(false);
  const status = CLIP_STATUS[clip.status];
  return (
    <div
      className={cx("relative aspect-[9/16] overflow-hidden rounded-xl border border-line bg-panel-2", className)}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      {clip.video_url && hover ? (
        <video src={clip.video_url} className="absolute inset-0 size-full object-cover" autoPlay muted loop playsInline />
      ) : clip.thumbnail_url ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={clip.thumbnail_url} alt="" className="absolute inset-0 size-full object-cover" loading="lazy" />
      ) : clip.video_thumbnail ? (
        <>
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={clip.video_thumbnail} alt="" className="absolute inset-0 size-full scale-110 object-cover opacity-60 blur-md" loading="lazy" />
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img src={clip.video_thumbnail} alt="" className="absolute inset-x-0 top-1/2 w-full -translate-y-1/2 object-cover" loading="lazy" />
        </>
      ) : null}
      <div className="absolute inset-0 bg-gradient-to-b from-black/40 via-transparent to-transparent" />
      <div className="absolute top-2 left-2 flex items-center gap-1.5">
        <ScoreBadge score={clip.viral_score} />
      </div>
      {clip.status !== "ready" && status && (
        <div className="absolute bottom-2 left-2">
          <Badge tone={status.tone} className="bg-black/60 backdrop-blur">
            {clip.status === "awaiting_media" ? <Hourglass className="size-3" /> : clip.status === "failed" ? <TriangleAlert className="size-3" /> : null}
            {status.label}
          </Badge>
        </div>
      )}
      {clip.video_url && !hover && (
        <div className="absolute inset-0 flex items-center justify-center opacity-0 transition group-hover:opacity-100">
          <span className="flex size-11 items-center justify-center rounded-full bg-black/50 backdrop-blur">
            <Play className="size-5 fill-white text-white" />
          </span>
        </div>
      )}
    </div>
  );
}

export function ClipCard({ clip }: { clip: Clip }) {
  return (
    <div className="group relative">
      <Link href={`/clips/${clip.id}`} className="block space-y-2.5">
        <ClipThumb clip={clip} className="transition group-hover:border-line-2" />
        <div className="space-y-0.5 px-0.5">
          <p className="truncate text-[11px] font-semibold tracking-wide text-muted uppercase">{clip.creator_name ?? "Eigen upload"}</p>
          <p className="line-clamp-2 text-sm leading-snug font-semibold text-ink group-hover:text-white">{clip.title || clip.hook_text}</p>
          <p className="text-[11px] text-muted">
            {formatDuration(clip.duration)} · {CATEGORY_LABELS[clip.category ?? "other"] ?? clip.category}
          </p>
        </div>
      </Link>
      {clip.download_url && (
        <a
          href={clip.download_url}
          className="absolute top-2 right-2 flex size-8 items-center justify-center rounded-full bg-black/55 text-white opacity-0 backdrop-blur transition group-hover:opacity-100 hover:bg-black/75"
          title="Download clip"
        >
          <Download className="size-4" />
        </a>
      )}
    </div>
  );
}

export function ScoreBars({ scores, labels }: { scores: Record<string, number>; labels: Record<string, string> }) {
  const order = ["hook", "hook_strength", "curiosity", "emotion", "surprise", "humor", "shareability", "comment_potential", "retention", "context", "payoff", "rewatch"];
  return (
    <div className="grid gap-x-6 gap-y-2.5 sm:grid-cols-2">
      {order.filter((k) => k in scores).map((k) => {
        const v = Math.round(scores[k]);
        const tone = scoreTone(v);
        return (
          <div key={k}>
            <div className="mb-1 flex items-center justify-between text-xs">
              <span className="text-ink-2">{labels[k] ?? k}</span>
              <span className="font-semibold tabular-nums text-ink">{v}</span>
            </div>
            <div className="h-1.5 overflow-hidden rounded-full bg-panel-3">
              <div
                className={cx("h-full rounded-full", tone === "hot" ? "fire-gradient" : tone === "good" ? "bg-warn" : tone === "moderate" ? "bg-info" : "bg-muted")}
                style={{ width: `${v}%` }}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

const FEEDBACK: { rating: Rating; label: string; icon: typeof Flame; active: string }[] = [
  { rating: "viral", label: "Viral", icon: Flame, active: "fire-gradient text-white border-transparent" },
  { rating: "good", label: "Good", icon: ThumbsUp, active: "bg-ok/15 text-ok border-ok/40" },
  { rating: "bad", label: "Bad", icon: ThumbsDown, active: "bg-warn/15 text-warn border-warn/40" },
  { rating: "reject", label: "Reject", icon: Ban, active: "bg-bad/15 text-bad border-bad/40" },
];

export function FeedbackButtons({ clip, onChange, compact }: { clip: Clip; onChange?: (c: Clip) => void; compact?: boolean }) {
  const toast = useToast();
  const [busy, setBusy] = useState<Rating | null>(null);
  async function rate(rating: Rating) {
    setBusy(rating);
    try {
      const updated =
        clip.rating === rating
          ? await api<Clip>(`/api/clips/${clip.id}/feedback`, { method: "DELETE" })
          : await api<Clip>(`/api/clips/${clip.id}/feedback`, { method: "POST", json: { rating } });
      onChange?.(updated);
      if (clip.rating !== rating) toast(rating === "reject" ? "Clip afgewezen — het systeem leert hiervan" : "Feedback opgeslagen");
    } catch (e) {
      toast(errorText(e), "error");
    } finally {
      setBusy(null);
    }
  }
  return (
    <div className={cx("grid gap-2", compact ? "grid-cols-4" : "grid-cols-2 sm:grid-cols-4")}>
      {FEEDBACK.map(({ rating, label, icon: Icon, active }) => (
        <button
          key={rating}
          onClick={() => rate(rating)}
          disabled={busy !== null}
          className={cx(
            "flex h-10 items-center justify-center gap-1.5 rounded-lg border text-sm font-medium transition disabled:opacity-60",
            clip.rating === rating ? active : "border-line bg-panel-2 text-ink-2 hover:border-line-2 hover:text-ink",
          )}
        >
          <Icon className="size-4" />
          {!compact && label}
        </button>
      ))}
    </div>
  );
}

export function ClipPlayer({ clip, src, videoRef }: { clip: Clip; src?: string | null; videoRef?: React.Ref<HTMLVideoElement> }) {
  const url = src || clip.video_url;
  if (url) {
    return (
      <div className="mx-auto aspect-[9/16] w-full max-w-[360px] overflow-hidden rounded-2xl border border-line bg-black">
        <video ref={videoRef} key={url} src={url} poster={src ? undefined : (clip.thumbnail_url ?? undefined)} className="size-full" controls playsInline preload="metadata" />
      </div>
    );
  }
  if (clip.youtube_embed_url) {
    return (
      <div className="space-y-3">
        <div className="aspect-video w-full overflow-hidden rounded-2xl border border-line bg-black">
          <iframe
            src={clip.youtube_embed_url}
            className="size-full"
            title="Origineel fragment op YouTube"
            allow="accelerometer; encrypted-media; gyroscope; picture-in-picture"
            allowFullScreen
          />
        </div>
        <p className="text-xs leading-relaxed text-muted">
          Preview van het originele fragment via de officiële YouTube-player. Upload de bronvideo (of zet hem in de inbox-map) om automatisch een
          verticale 9:16-clip met captions te renderen.
        </p>
      </div>
    );
  }
  return <div className="aspect-[9/16] w-full max-w-[360px] rounded-2xl border border-line bg-panel-2" />;
}
