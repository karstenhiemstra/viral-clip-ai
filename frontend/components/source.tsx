"use client";

import { CloudDownload, Copy, FileText, FolderInput, TvMinimalPlay, Upload } from "lucide-react";
import { useRef, useState } from "react";

import { errorText, useToast } from "@/components/toast";
import { Button, cx, Input, Modal, ProgressBar } from "@/components/ui";
import { VIDEO_ACCEPT } from "@/components/uploads";
import { api, uploadFile } from "@/lib/api";
import type { Video } from "@/lib/types";

/** Every allowed way to hand ViralClip the source of a video, in one place. */
export function SourcePanel({ video, onDone, compact = false }: { video: Video; onDone?: () => void; compact?: boolean }) {
  const toast = useToast();
  const fileInput = useRef<HTMLInputElement>(null);
  const srtInput = useRef<HTMLInputElement>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [drag, setDrag] = useState(false);
  const [link, setLink] = useState("");
  const [busy, setBusy] = useState<"link" | "srt" | null>(null);
  const inboxName = video.youtube_video_id ? `${(video.title || "video").replace(/[\\/:*?"<>|[\]]+/g, "").slice(0, 60).trim()} [${video.youtube_video_id}].mp4` : null;

  async function uploadVideo(file: File | undefined) {
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    setProgress(0);
    try {
      await uploadFile(`/api/videos/${video.id}/media`, form, setProgress);
      toast("Bronvideo ontvangen — de analyse start automatisch");
      onDone?.();
    } catch (e) {
      toast(errorText(e), "error");
    } finally {
      setProgress(null);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  async function importLink(e: React.FormEvent) {
    e.preventDefault();
    setBusy("link");
    try {
      await api(`/api/videos/${video.id}/import-url`, { method: "POST", json: { url: link } });
      toast("Download gestart — daarna start de analyse automatisch");
      setLink("");
      onDone?.();
    } catch (err) {
      toast(errorText(err), "error");
    } finally {
      setBusy(null);
    }
  }

  async function uploadSrt(file: File | undefined) {
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    setBusy("srt");
    try {
      await uploadFile(`/api/videos/${video.id}/transcript`, form);
      toast("Ondertitels ontvangen — de analyse start nu al");
      onDone?.();
    } catch (e) {
      toast(errorText(e), "error");
    } finally {
      setBusy(null);
      if (srtInput.current) srtInput.current.value = "";
    }
  }

  return (
    <div className="space-y-4">
      {video.youtube_video_id && !compact && (
        <div className="flex items-start gap-3 rounded-xl border border-warn/30 bg-warn/5 p-4">
          <TvMinimalPlay className="mt-0.5 size-5 shrink-0 text-warn" />
          <div className="text-sm">
            <p className="font-semibold text-ink">Deze video is gevonden op YouTube. Lever de bronvideo hier aan om clips te genereren.</p>
            <p className="mt-1 text-xs text-muted">
              YouTube staat niet toe dat apps video&apos;s downloaden, daarom lever je het bestand zelf aan. Daarna gaat alles automatisch:
              transcriptie → AI-analyse → beste clips → 9:16 met captions.
            </p>
          </div>
        </div>
      )}

      {/* 1. upload */}
      <div
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => { e.preventDefault(); setDrag(false); uploadVideo(e.dataTransfer.files?.[0]); }}
        className={cx("rounded-xl border-2 border-dashed p-5 text-center transition", drag ? "border-fire bg-fire/5" : "border-line-2 bg-panel-2")}
      >
        <input ref={fileInput} type="file" accept={VIDEO_ACCEPT} className="hidden" onChange={(e) => uploadVideo(e.target.files?.[0])} />
        <Upload className="mx-auto size-6 text-fire" />
        <p className="mt-2 text-sm font-semibold">Sleep het videobestand hierheen</p>
        <p className="text-xs text-muted">MP4, MOV, MKV, WEBM of alleen audio (MP3/M4A/WAV)</p>
        {progress !== null ? (
          <div className="mx-auto mt-3 max-w-xs space-y-1">
            <ProgressBar value={progress * 100} />
            <p className="text-xs text-muted">{Math.round(progress * 100)}% geüpload{progress >= 1 ? " — verwerken…" : ""}</p>
          </div>
        ) : (
          <Button className="mt-3" variant="fire" icon={<Upload className="size-4" />} onClick={() => fileInput.current?.click()}>
            Bestand kiezen
          </Button>
        )}
      </div>

      {/* 2. share link */}
      <form onSubmit={importLink} className="rounded-xl border border-line bg-panel-2 p-4">
        <p className="flex items-center gap-2 text-sm font-semibold"><CloudDownload className="size-4 text-info" /> Of plak een deel-link</p>
        <p className="mt-0.5 text-xs text-muted">Google Drive (delen: &quot;Iedereen met de link&quot;), Dropbox of OneDrive. De app downloadt het bestand zelf.</p>
        <div className="mt-2 flex flex-wrap gap-2">
          <Input required type="url" placeholder="https://drive.google.com/file/d/…" value={link} onChange={(e) => setLink(e.target.value)} className="min-w-[220px] flex-1" />
          <Button type="submit" loading={busy === "link"} disabled={!link}>Importeren</Button>
        </div>
      </form>

      <div className="grid gap-3 sm:grid-cols-2">
        {/* 3. subtitles */}
        <div className="rounded-xl border border-line bg-panel-2 p-4">
          <p className="flex items-center gap-2 text-sm font-semibold"><FileText className="size-4 text-ok" /> Alleen ondertitels (.srt)?</p>
          <p className="mt-0.5 text-xs text-muted">Dan start de analyse al (en is transcriptie gratis). De clips worden gerenderd zodra de video er is.</p>
          <input ref={srtInput} type="file" accept=".srt,.vtt" className="hidden" onChange={(e) => uploadSrt(e.target.files?.[0])} />
          <Button size="sm" className="mt-2" loading={busy === "srt"} icon={<FileText className="size-3.5" />} onClick={() => srtInput.current?.click()}>
            .srt / .vtt kiezen
          </Button>
        </div>
        {/* 4. inbox */}
        <div className="rounded-xl border border-line bg-panel-2 p-4">
          <p className="flex items-center gap-2 text-sm font-semibold"><FolderInput className="size-4 text-muted" /> Of via de inbox-map</p>
          <p className="mt-0.5 text-xs text-muted">
            Zet het bestand in de map <code className="rounded bg-panel-3 px-1 text-ink-2">data/inbox</code> in je ViralClip-map{inboxName ? " met deze naam:" : "."}
          </p>
          {inboxName && (
            <button
              type="button"
              onClick={() => { navigator.clipboard?.writeText(inboxName); toast("Bestandsnaam gekopieerd"); }}
              className="mt-2 flex w-full items-center gap-2 truncate rounded-lg border border-line bg-panel-3 px-2.5 py-1.5 text-left font-mono text-[11px] text-ink-2 hover:border-line-2"
              title="Klik om te kopiëren"
            >
              <Copy className="size-3 shrink-0" /> <span className="truncate">{inboxName}</span>
            </button>
          )}
        </div>
      </div>

      {video.youtube_video_id && (
        <p className="text-xs text-muted">
          💡 Is het je eigen video? Download hem in <span className="text-ink-2">YouTube Studio → Content → ⋮ → Downloaden</span>, en de ondertitels via{" "}
          <span className="text-ink-2">Ondertiteling → ⋮ → Downloaden (.srt)</span>. Van een andere creator: vraag het bestand aan via hun clipping-programma of een gedeelde map.
        </p>
      )}
    </div>
  );
}

export function SourceButton({ video, onDone, size = "sm" }: { video: Video; onDone?: () => void; size?: "sm" | "md" }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button size={size} variant="fire" icon={<Upload className="size-3.5" />} onClick={() => setOpen(true)}>
        Bron aanleveren
      </Button>
      <Modal open={open} onClose={() => setOpen(false)} title={`Bronvideo · ${video.title || "video"}`}>
        <SourcePanel video={video} onDone={() => { setOpen(false); onDone?.(); }} />
      </Modal>
    </>
  );
}
