"""Static configuration loaded from environment variables (.env).

Only infrastructure and secrets live here. Everything a user may want to tweak from
the dashboard (weights, clip duration, caption preset, ...) lives in the runtime
settings store (``app.services.settings_store``) and falls back to these defaults.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BACKEND_DIR.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(REPO_DIR / ".env"), str(BACKEND_DIR / ".env")),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- core -----------------------------------------------------------------
    app_env: str = "development"
    database_url: str = f"sqlite:///{REPO_DIR / 'data' / 'viralclip.db'}"
    data_dir: Path = REPO_DIR / "data"
    # Secret used to encrypt API keys that are entered via the Settings page.
    app_secret_key: str = ""
    # Optional bearer token that every API request must carry (set in production).
    api_auth_token: str = ""
    cors_origins: str = "http://localhost:3000"
    app_timezone: str = "Europe/Amsterdam"

    # --- storage --------------------------------------------------------------
    storage_backend: str = "local"  # local | s3
    s3_endpoint_url: str = ""
    s3_bucket: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_region: str = "auto"
    s3_public_base_url: str = ""

    # --- external APIs --------------------------------------------------------
    youtube_api_key: str = ""
    # Override only for testing / corporate proxies.
    youtube_api_base: str = "https://www.googleapis.com/youtube/v3"
    youtube_web_base: str = "https://www.youtube.com"
    openai_api_key: str = ""
    openai_base_url: str = ""
    anthropic_api_key: str = ""

    # --- AI defaults (overridable from Settings page) -------------------------
    llm_provider: str = "auto"  # auto | openai | anthropic | heuristic
    llm_quality: str = "balanced"  # budget | balanced | best
    llm_model_fast: str = ""  # empty -> model from the quality preset
    llm_model_smart: str = ""
    transcriber: str = "auto"  # auto | openai | faster_whisper | none
    whisper_model: str = "whisper-1"
    faster_whisper_model: str = "small"
    embedding_model: str = "text-embedding-3-small"
    # Captions are a transcription of what is said, in the spoken language. Translation is not offered:
    # keep this false (true only logs a warning; captions still stay in the spoken language).
    translate_captions: bool = False

    # --- worker -----------------------------------------------------------------
    worker_poll_seconds: float = 2.0
    worker_id: str = ""
    job_stale_minutes: int = 30
    inbox_scan_seconds: int = 60
    # Largest accepted upload / imported source file.
    max_upload_gb: float = 20.0

    # --- tools ------------------------------------------------------------------
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    fonts_dir: Path = BACKEND_DIR / "assets" / "fonts"
    face_model_path: Path = Field(default_factory=lambda: REPO_DIR / "data" / "models" / "face_detection_yunet.onnx")
    # Downloaded once on first use; set empty to disable (then OpenCV's bundled Haar cascades are used).
    face_model_url: str = (
        "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
    )

    @field_validator("database_url", mode="before")
    @classmethod
    def _default_db(cls, v: object) -> object:
        # An empty DATABASE_URL= line in .env means "use the local SQLite default".
        return v or f"sqlite:///{REPO_DIR / 'data' / 'viralclip.db'}"

    @field_validator("data_dir", mode="before")
    @classmethod
    def _default_data_dir(cls, v: object) -> object:
        return v or REPO_DIR / "data"

    @property
    def storage_dir(self) -> Path:
        return self.data_dir / "storage"

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def music_dir(self) -> Path:
        """Music for the Auto Edit: files the user put here (or uploaded on the Auto Edit page)."""
        return self.data_dir / "music"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.storage_dir, self.inbox_dir, self.tmp_dir, self.music_dir, self.face_model_path.parent):
            d.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s


OPENAI_DEFAULT_BASE_URL = "https://api.openai.com/v1"


def openai_base_url() -> str:
    """The OpenAI API URL to use - always explicit, never None.

    When the OpenAI SDK gets ``base_url=None`` it reads the ``OPENAI_BASE_URL`` environment variable
    itself. Docker passes the empty ``OPENAI_BASE_URL=`` line of .env as an empty string, which the SDK
    then uses as the URL: every call fails with "Connection error." without leaving the container."""
    url = (get_settings().openai_base_url or "").strip().strip("\"'").strip()
    if not url:
        return OPENAI_DEFAULT_BASE_URL
    if "://" not in url:
        url = "https://" + url
    return url.rstrip("/")
