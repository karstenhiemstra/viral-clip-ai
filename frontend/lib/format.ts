const nf = new Intl.NumberFormat("nl-NL");

export function formatNumber(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return nf.format(n);
}

export function compactNumber(n: number | null | undefined): string {
  if (n === null || n === undefined) return "—";
  return new Intl.NumberFormat("nl-NL", { notation: "compact", maximumFractionDigits: 1 }).format(n);
}

export function formatTimestamp(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return "—";
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(sec).padStart(2, "0");
  return h ? `${String(h).padStart(2, "0")}:${mm}:${ss}` : `${mm}:${ss}`;
}

export function formatDuration(seconds: number | null | undefined): string {
  if (!seconds) return "—";
  if (seconds < 90) return `${seconds.toFixed(1).replace(".", ",")} sec`;
  const m = Math.round(seconds / 60);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)} u ${m % 60} min`;
}

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "nooit";
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  const rtf = new Intl.RelativeTimeFormat("nl-NL", { numeric: "auto" });
  const abs = Math.abs(diff);
  if (abs < 60) return "zojuist";
  if (abs < 3600) return rtf.format(-Math.round(diff / 60), "minute");
  if (abs < 86400) return rtf.format(-Math.round(diff / 3600), "hour");
  if (abs < 86400 * 30) return rtf.format(-Math.round(diff / 86400), "day");
  return new Date(iso).toLocaleDateString("nl-NL", { day: "numeric", month: "short", year: "numeric" });
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("nl-NL", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function money(usd: number | null | undefined): string {
  if (usd === null || usd === undefined) return "—";
  return usd < 0.01 && usd > 0 ? `< $0,01` : `$${usd.toFixed(2).replace(".", ",")}`;
}

export function pct(x: number | null | undefined, digits = 0): string {
  if (x === null || x === undefined) return "—";
  return `${(x * 100).toFixed(digits).replace(".", ",")}%`;
}

export const CATEGORY_LABELS: Record<string, string> = {
  humor: "Humor",
  story: "Verhaal",
  reaction: "Reactie",
  controversy: "Controversieel",
  reveal: "Onthulling",
  question: "Vraag",
  emotional: "Emotioneel",
  fail: "Fail",
  challenge: "Challenge",
  informative: "Informatief",
  awkward: "Awkward",
  wholesome: "Wholesome",
  other: "Overig",
};

export const FLAG_LABELS: Record<string, string> = {
  needs_context: "Heeft context nodig",
  inside_joke: "Inside joke",
  starts_mid_sentence: "Start midden in zin",
  ends_mid_sentence: "Eindigt midden in zin",
  weak_payoff: "Zwakke payoff",
  sponsor_or_ad: "Sponsor / reclame",
  intro_or_outro: "Intro / outro",
  mixes_topics: "Twee onderwerpen door elkaar",
  low_energy: "Weinig energie",
  repetitive: "Herhalend",
  sensitive: "Gevoelig onderwerp",
};

export const VIDEO_STATUS: Record<string, { label: string; tone: Tone }> = {
  discovered: { label: "Gevonden", tone: "neutral" },
  skipped: { label: "Overgeslagen", tone: "muted" },
  queued: { label: "In wachtrij", tone: "info" },
  awaiting_media: { label: "Bron nodig", tone: "warning" },
  awaiting_key: { label: "Wacht op OpenAI-key", tone: "warning" },
  analyzing: { label: "Analyseren", tone: "info" },
  analyzed: { label: "Geanalyseerd", tone: "success" },
  failed: { label: "Mislukt", tone: "danger" },
};

export const JOB_STATUS: Record<string, { label: string; tone: Tone }> = {
  queued: { label: "Wachtend", tone: "neutral" },
  running: { label: "Bezig", tone: "info" },
  waiting: { label: "Wacht op input", tone: "warning" },
  completed: { label: "Klaar", tone: "success" },
  failed: { label: "Mislukt", tone: "danger" },
  cancelled: { label: "Geannuleerd", tone: "muted" },
};

export const CLIP_STATUS: Record<string, { label: string; tone: Tone }> = {
  awaiting_media: { label: "Wacht op bronvideo", tone: "warning" },
  pending_render: { label: "Wacht op render", tone: "neutral" },
  rendering: { label: "Renderen…", tone: "info" },
  ready: { label: "Klaar", tone: "success" },
  failed: { label: "Render mislukt", tone: "danger" },
};

export const JOB_TYPES: Record<string, string> = {
  scan_creator: "Kanaalscan",
  analyze_video: "Analyse",
  render_clip: "Render",
  import_media: "Bron importeren",
  train_model: "Leermodel",
};

export type Tone = "neutral" | "muted" | "info" | "success" | "warning" | "danger" | "fire";

export function scoreTone(score: number): "hot" | "good" | "moderate" | "low" {
  if (score >= 85) return "hot";
  if (score >= 70) return "good";
  if (score >= 55) return "moderate";
  return "low";
}

export const DIMENSION_LABELS: Record<string, string> = {
  hook: "Hook",
  hook_strength: "Hook Strength",
  curiosity: "Curiosity",
  emotion: "Emotion",
  surprise: "Surprise",
  humor: "Humor",
  shareability: "Shareability",
  comment_potential: "Comment Potential",
  retention: "Retention",
  context: "Context",
  payoff: "Payoff",
  rewatch: "Rewatch",
};

export const STAGE_LABELS: Record<string, { label: string; hint: string }> = {
  stop: { label: "Stop", hint: "Stopt iemand met scrollen?" },
  hold: { label: "Hold", hint: "Blijft iemand kijken tot de payoff?" },
  engage: { label: "Engage", hint: "Wordt het gedeeld, becommentarieerd, opnieuw bekeken?" },
};

export const PRESET_LABELS: Record<string, string> = {
  dynamic: "Dynamic (TikTok-stijl)",
  bold_white: "Grote witte captions",
  minimal: "Minimalistisch",
  none: "Geen captions",
};

export const LAYOUT_LABELS: Record<string, string> = {
  auto: "Automatisch",
  face: "Volg spreker",
  center: "Midden crop",
  fit_blur: "Volledig beeld + blur",
  split: "Split screen (2 personen)",
};
