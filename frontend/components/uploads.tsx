"use client";

import { FileText, Link2, Upload } from "lucide-react";
import { useRef, useState } from "react";

import { errorText, useToast } from "@/components/toast";
import { Button, cx, Field, Input, Modal, ProgressBar } from "@/components/ui";
import { api, uploadFile } from "@/lib/api";
import type { Video } from "@/lib/types";

export const VIDEO_ACCEPT = ".mp4,.mkv,.mov,.webm,.m4v,.avi,.mp3,.m4a,.wav,.aac,.ogg,.opus,.flac";

export function UploadMediaButton({ video, onDone, size = "sm" }: { video: Video; onDone?: () => void; size?: "sm" | "md" }) {
  const toast = useToast();
  const input = useRef<HTMLInputElement>(null);
  const [progress, setProgress] = useState<number | null>(null);
  async function onFile(file: File | undefined) {
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    setProgress(0);
    try {
      await uploadFile(`/api/videos/${video.id}/media`, form, setProgress);
      toast("Bronvideo geüpload — analyse wordt gestart");
      onDone?.();
    } catch (e) {
      toast(errorText(e), "error");
    } finally {
      setProgress(null);
      if (input.current) input.current.value = "";
    }
  }
  return (
    <>
      <input ref={input} type="file" accept={VIDEO_ACCEPT} className="hidden" onChange={(e) => onFile(e.target.files?.[0])} />
      {progress !== null ? (
        <div className="flex w-36 items-center gap-2 text-xs text-muted">
          <ProgressBar value={progress * 100} /> {Math.round(progress * 100)}%
        </div>
      ) : (
        <Button size={size} icon={<Upload className="size-3.5" />} onClick={() => input.current?.click()}>
          {video.has_media ? "Vervang bron" : "Upload bron"}
        </Button>
      )}
    </>
  );
}

export function UploadTranscriptButton({ video, onDone }: { video: Video; onDone?: () => void }) {
  const toast = useToast();
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  async function onFile(file: File | undefined) {
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    setBusy(true);
    try {
      await uploadFile(`/api/videos/${video.id}/transcript`, form);
      toast("Transcript geïmporteerd — analyse wordt gestart");
      onDone?.();
    } catch (e) {
      toast(errorText(e), "error");
    } finally {
      setBusy(false);
      if (input.current) input.current.value = "";
    }
  }
  return (
    <>
      <input ref={input} type="file" accept=".srt,.vtt" className="hidden" onChange={(e) => onFile(e.target.files?.[0])} />
      <Button size="sm" loading={busy} icon={<FileText className="size-3.5" />} onClick={() => input.current?.click()}>
        Transcript (SRT/VTT)
      </Button>
    </>
  );
}

export function AddVideoModal({ open, onClose, onAdded }: { open: boolean; onClose: () => void; onAdded: (v: Video) => void }) {
  const toast = useToast();
  const [tab, setTab] = useState<"url" | "upload">("url");
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [linkUrl, setLinkUrl] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);

  async function submitUrl(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const v = await api<Video>("/api/videos", { method: "POST", json: { url, analyze: true } });
      toast("Video toegevoegd aan de analysewachtrij");
      setUrl("");
      onAdded(v);
      onClose();
    } catch (err) {
      toast(errorText(err), "error");
    } finally {
      setBusy(false);
    }
  }

  async function submitUpload(e: React.FormEvent) {
    e.preventDefault();
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    if (title) form.append("title", title);
    if (linkUrl) form.append("youtube_url", linkUrl);
    setProgress(0);
    try {
      const v = await uploadFile<Video>("/api/videos/upload", form, setProgress);
      toast("Video geüpload — analyse is gestart");
      setFile(null);
      setTitle("");
      setLinkUrl("");
      onAdded(v);
      onClose();
    } catch (err) {
      toast(errorText(err), "error");
    } finally {
      setProgress(null);
    }
  }

  return (
    <Modal open={open} onClose={onClose} title="Video toevoegen">
      <div className="mb-4 grid grid-cols-2 gap-1 rounded-lg border border-line bg-panel-2 p-1">
        {(["url", "upload"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={cx("flex h-8 items-center justify-center gap-1.5 rounded-md text-xs font-medium transition", tab === t ? "bg-panel-3 text-ink" : "text-muted hover:text-ink")}
          >
            {t === "url" ? <Link2 className="size-3.5" /> : <Upload className="size-3.5" />}
            {t === "url" ? "YouTube-URL" : "Bestand uploaden"}
          </button>
        ))}
      </div>
      {tab === "url" ? (
        <form onSubmit={submitUrl} className="space-y-4">
          <Field label="YouTube-URL" hint="Metadata en comments komen via de officiële YouTube API. Voor analyse is daarna het bronbestand of een transcript nodig.">
            <Input autoFocus required placeholder="https://www.youtube.com/watch?v=…" value={url} onChange={(e) => setUrl(e.target.value)} />
          </Field>
          <div className="flex justify-end">
            <Button type="submit" variant="fire" loading={busy}>Toevoegen & analyseren</Button>
          </div>
        </form>
      ) : (
        <form onSubmit={submitUpload} className="space-y-4">
          <Field label="Videobestand" hint="Alleen materiaal waarvoor je toestemming hebt: eigen content of bestanden die de creator aanlevert (bijv. via een clipping-programma).">
            <input
              type="file"
              required
              accept={VIDEO_ACCEPT}
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              className="block w-full text-sm text-ink-2 file:mr-3 file:rounded-lg file:border-0 file:bg-panel-3 file:px-3 file:py-2 file:text-sm file:text-ink"
            />
          </Field>
          <Field label="Titel (optioneel)">
            <Input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="Wordt anders de bestandsnaam" />
          </Field>
          <Field label="Bijbehorende YouTube-URL (optioneel)" hint="Koppelt metadata, creator en comment-hotspots aan deze upload.">
            <Input value={linkUrl} onChange={(e) => setLinkUrl(e.target.value)} placeholder="https://www.youtube.com/watch?v=…" />
          </Field>
          {progress !== null && (
            <div className="space-y-1">
              <ProgressBar value={progress * 100} />
              <p className="text-xs text-muted">{Math.round(progress * 100)}% geüpload{progress >= 1 ? " — verwerken…" : ""}</p>
            </div>
          )}
          <div className="flex justify-end">
            <Button type="submit" variant="fire" loading={progress !== null} disabled={!file}>Uploaden & analyseren</Button>
          </div>
        </form>
      )}
    </Modal>
  );
}
