export type Priority = "low" | "normal" | "high";
export type Rating = "viral" | "good" | "bad" | "reject";

export interface Creator {
  id: number;
  name: string;
  youtube_channel_id: string;
  handle: string | null;
  channel_url: string;
  thumbnail_url: string | null;
  subscriber_count: number | null;
  video_count: number | null;
  priority: Priority;
  language: string;
  clip_min_seconds: number | null;
  clip_max_seconds: number | null;
  max_clips_per_video: number | null;
  min_video_minutes: number | null;
  scan_enabled: boolean;
  auto_analyze: boolean;
  last_scanned_at: string | null;
  last_scan_status: string | null;
  last_scan_error: string | null;
  last_scan_new_videos: number;
  last_video_published_at: string | null;
  last_video_title: string | null;
  pending_videos?: number;
  new_videos?: number;
  analyzing_videos?: number;
  analyzed_videos?: number;
  skipped_videos?: number;
  clip_count?: number;
  best_score?: number | null;
  scan_job_status?: string | null;
}

export interface ChannelResult {
  channel_id: string;
  title: string;
  handle: string | null;
  description: string | null;
  thumbnail_url: string | null;
  subscriber_count: number | null;
  video_count: number | null;
  already_added?: boolean;
}

export interface Hotspot {
  time: number;
  weight: number;
  strength: number;
  count: number;
  sample: string;
}

export interface CandidateLog {
  id: string;
  start: number;
  end: number;
  sources: string[];
  category: string | null;
  strength: number;
  signal: number;
  description: string;
  reason: string;
  viral_score?: number;
  final_start?: number;
  final_end?: number;
  selected: boolean;
  verdict?: string | null;
  why?: string;
  note?: string;
}

export interface AnalysisRun {
  id: number;
  status: string;
  provider: string | null;
  models: Record<string, string>;
  started_at: string | null;
  finished_at: string | null;
  input_tokens: number;
  output_tokens: number;
  estimated_cost_usd: number;
  error: string | null;
  stage_log: { stage: string; seconds?: number; items?: string[]; [k: string]: unknown }[];
  candidates: CandidateLog[];
}

export interface Video {
  id: number;
  youtube_video_id: string | null;
  youtube_url: string | null;
  creator_id: number | null;
  creator_name: string | null;
  title: string;
  published_at: string | null;
  duration_seconds: number | null;
  view_count: number | null;
  like_count: number | null;
  comment_count: number | null;
  thumbnail_url: string | null;
  is_short: boolean;
  source: string;
  status: string;
  skip_reason: string | null;
  prescore: number | null;
  prescore_details: Record<string, number>;
  crowd_hotspots: Hotspot[];
  has_media: boolean;
  media_origin: string | null;
  media_meta: Record<string, unknown>;
  has_transcript: boolean;
  transcript_source: string | null;
  discovered_at: string | null;
  analyzed_at: string | null;
  clip_count?: number;
  best_score?: number | null;
  job_status?: string;
  job_progress?: number;
  job_message?: string | null;
  description?: string | null;
  transcript_words?: number;
  latest_run?: AnalysisRun | null;
}

export interface Performance {
  id: number;
  platform: string;
  post_url: string | null;
  views: number | null;
  likes: number | null;
  comments: number | null;
  shares: number | null;
  saves: number | null;
  watch_time_seconds: number | null;
  avg_watch_seconds: number | null;
  avg_percentage_watched: number | null;
  completion_rate: number | null;
  followers_gained: number | null;
  recorded_at: string | null;
}

/** One caption of a clip, in clip time (seconds). */
export interface CaptionCue {
  start: number;
  end: number;
  text: string;
}

export interface ClipCaptions {
  custom: boolean;
  captions: CaptionCue[];
  duration: number;
  caption_preset: string;
}

export interface Clip {
  id: number;
  video_id: number;
  creator_id: number | null;
  creator_name: string | null;
  creator_thumbnail: string | null;
  video_title: string | null;
  video_thumbnail: string | null;
  youtube_url: string | null;
  youtube_embed_url: string | null;
  rank: number;
  start_time: number;
  end_time: number;
  duration: number;
  title: string | null;
  hook_text: string | null;
  explanation: string | null;
  category: string | null;
  flags: string[];
  viral_score: number;
  potential_label: string;
  scores: Record<string, number>;
  stage_scores: Record<string, number>;
  status: string;
  caption_preset: string | null;
  captions_custom?: boolean;
  /** Spoken language = caption language (captions are never translated). */
  caption_language?: { caption_language: string | null; detected_language: string | null; translation_applied: boolean; confidence?: number; languages?: Record<string, number>; mixed?: boolean } | null;
  layout: string | null;
  video_url: string | null;
  thumbnail_url: string | null;
  download_url: string | null;
  rating: Rating | null;
  published: boolean;
  published_url: string | null;
  created_at: string | null;
  render_error: string | null;
  // detail
  transcript_text?: string;
  words?: [number, number, string][];
  segments?: [number, number][];
  emphasis_words?: string[];
  signals?: Record<string, unknown>;
  score_breakdown?: Record<string, unknown>;
  render_meta?: Record<string, unknown>;
  sources?: string[];
  feedback?: { rating: string; note: string | null; created_at: string | null }[];
  performance?: Performance[];
}

export interface Job {
  id: number;
  type: string;
  status: string;
  title: string | null;
  priority: number;
  progress: number;
  stage: string | null;
  message: string | null;
  error: string | null;
  attempts: number;
  creator_id: number | null;
  video_id: number | null;
  clip_id: number | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  creator_name: string | null;
  video_title: string | null;
  video_thumbnail: string | null;
}

export interface Usage {
  day: string;
  providers: Record<string, { units: number; input_tokens: number; output_tokens: number; cost_usd: number }>;
  youtube_quota_used: number;
  youtube_quota_limit: number;
  cost_today_usd: number;
  cost_total_usd: number;
}

export interface Dashboard {
  new_potential_viral_clips: number;
  new_clips_24h: number;
  unrated_high_potential: number;
  creators: number;
  videos_analyzed_24h: number;
  videos_awaiting_media: number;
  queue: { queued: number; running: number; waiting: number };
  running_jobs: { id: number; title: string | null; progress: number; message: string | null; type: string }[];
  top_clips: Clip[];
  today_by_creator: Clip[];
  usage: Usage;
  warnings: { level: "error" | "warning"; text: string }[];
  setup: { key: string; done: boolean; label: string; hint: string; href: string }[];
}

export interface Analytics {
  ratings: Record<string, number>;
  categories: {
    category: string;
    clips: number;
    avg_score: number | null;
    rated: number;
    positive_rate: number | null;
    published: number;
    avg_views: number | null;
    avg_completion: number | null;
  }[];
  calibration: { bucket: string; clips: number; positive_rate: number | null; avg_views: number | null }[];
  performance_points: {
    clip_id: number;
    title: string | null;
    creator: string | null;
    category: string | null;
    viral_score: number;
    views: number | null;
    completion_rate: number | null;
    likes: number | null;
    shares: number | null;
  }[];
  top_published: Analytics["performance_points"];
  rated_count: number;
  min_samples: number;
  model: {
    version: number;
    n_samples: number;
    metrics: Record<string, number | null>;
    insights: { feature: string; effect: number; samples: number; text: string }[];
    created_at: string | null;
  } | null;
  costs: { day: string; provider: string; cost_usd: number; units: number }[];
}

export interface SettingsPayload {
  settings: {
    scoring: {
      weights: Record<string, number>;
      stage_weights: Record<string, number>;
      signal_blend: number;
      min_viral_score: number;
      personalization_strength: number;
    };
    clips: {
      min_seconds: number;
      max_seconds: number;
      target_seconds: number;
      max_per_video: number;
      remove_silences: boolean;
      silence_min_gap: number;
      caption_preset: string;
      layout: string;
      add_hook_title: boolean;
    };
    discovery: {
      auto_scan: boolean;
      scan_interval_minutes: number;
      period: string;
      period_start: string | null;
      period_end: string | null;
      max_videos_per_scan: number;
      min_video_minutes: number;
      max_video_minutes: number;
      min_views: number;
      exclude_shorts: boolean;
      exclude_live: boolean;
      fetch_comments: boolean;
      title_exclude_keywords: string[];
    };
    pipeline: {
      auto_analyze: boolean;
      auto_render: boolean;
      candidate_count: number;
      detail_batch_size: number;
      scene_detection: boolean;
      use_embeddings: boolean;
      use_vision: boolean;
      vision_top_n: number;
    };
    ai: {
      llm_provider: string;
      quality: "budget" | "balanced" | "best";
      model_fast: string;
      model_smart: string;
      vision_model: string;
      transcriber: string;
      whisper_model: string;
      faster_whisper_model: string;
      output_language: string;
    };
  };
  secrets: Record<string, { configured: boolean; source: string | null; masked: string; status: "connected" | "error" | "untested" | "missing"; message: string | null; checked_at: string | null }>;
  system: {
    ffmpeg: boolean;
    face_detector: string;
    llm: { provider: string; models: Record<string, string>; error?: string };
    transcriber: string | null;
    storage: string;
    database: string;
    auth_enabled: boolean;
    timezone: string;
    cost_estimate: {
      provider: string;
      active: boolean;
      quality: string;
      whisper_api: boolean;
      rows: ({ minutes: number; transcription: number } & Record<string, unknown>)[];
    };
  };
  meta: {
    dimensions: { key: string; label: string }[];
    stages: Record<string, string[]>;
    default_weights: Record<string, number>;
    default_stage_weights: Record<string, number>;
    caption_presets: string[];
    layouts: string[];
    secret_names: string[];
  };
}
