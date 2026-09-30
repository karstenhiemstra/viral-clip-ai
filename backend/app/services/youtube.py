"""YouTube discovery using only official, documented interfaces.

* YouTube Data API v3 (API key) - channel search/resolve, video metadata, top comments.
* Public channel RSS feed (``/feeds/videos.xml``) - new uploads at zero quota cost.
* oEmbed - basic metadata for a single video without an API key.

Quota costs (default daily quota 10,000 units): search.list = 100, every other call we use = 1.
We therefore only use search when adding a creator by name; scanning uses RSS + videos.list.
"""

from __future__ import annotations

import logging
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from app.services.usage import record_usage

log = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/youtube/v3"
RSS_URL = "https://www.youtube.com/feeds/videos.xml"
OEMBED_URL = "https://www.youtube.com/oembed"

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)
_TIMESTAMP_RE = re.compile(r"(?<![\d:])(?:(\d{1,2}):)?([0-5]?\d):([0-5]\d)(?![\d:])")


class YouTubeError(Exception):
    pass


class QuotaExceeded(YouTubeError):
    pass


class MissingApiKey(YouTubeError):
    pass


@dataclass
class ChannelInfo:
    channel_id: str
    title: str
    handle: str | None = None
    description: str | None = None
    thumbnail_url: str | None = None
    subscriber_count: int | None = None
    video_count: int | None = None
    view_count: int | None = None
    uploads_playlist_id: str | None = None
    country: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class VideoInfo:
    video_id: str
    title: str
    channel_id: str | None = None
    channel_title: str | None = None
    description: str | None = None
    published_at: datetime | None = None
    duration_seconds: float | None = None
    view_count: int | None = None
    like_count: int | None = None
    comment_count: int | None = None
    thumbnail_url: str | None = None
    tags: list[str] = field(default_factory=list)
    language: str | None = None
    live_status: str | None = None  # none | live | upcoming
    is_short: bool = False


@dataclass
class Comment:
    text: str
    likes: int = 0


@dataclass
class ChannelRef:
    kind: str  # id | handle | username | custom | query
    value: str


# --- parsing helpers ---------------------------------------------------------------------------


def parse_duration(value: str | None) -> float | None:
    """ISO-8601 duration (``PT1H2M3S``) -> seconds."""
    if not value:
        return None
    m = _DURATION_RE.match(value.strip())
    if not m:
        return None
    d = {k: float(v) if v else 0.0 for k, v in m.groupdict().items()}
    return d["days"] * 86400 + d["hours"] * 3600 + d["minutes"] * 60 + d["seconds"]


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def parse_video_id(text: str) -> str | None:
    """Accepts watch/shorts/live/embed/youtu.be URLs or a bare 11-char id."""
    text = (text or "").strip()
    if _VIDEO_ID_RE.match(text):
        return text
    try:
        u = urlparse(text if "://" in text else f"https://{text}")
    except ValueError:
        return None
    host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.").removeprefix("music.")
    if host == "youtu.be":
        cand = u.path.strip("/").split("/")[0]
        return cand if _VIDEO_ID_RE.match(cand) else None
    if host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        qs = parse_qs(u.query)
        if "v" in qs and _VIDEO_ID_RE.match(qs["v"][0]):
            return qs["v"][0]
        parts = [p for p in u.path.split("/") if p]
        if len(parts) >= 2 and parts[0] in ("shorts", "live", "embed", "v", "e"):
            return parts[1] if _VIDEO_ID_RE.match(parts[1]) else None
    return None


def parse_channel_input(text: str) -> ChannelRef:
    """Understands channel URLs, @handles, channel ids and falls back to a free-text search query."""
    text = (text or "").strip()
    if _CHANNEL_ID_RE.match(text):
        return ChannelRef("id", text)
    if text.startswith("@") and " " not in text:
        return ChannelRef("handle", text)
    if "youtube.com" in text or "youtu.be" in text:
        u = urlparse(text if "://" in text else f"https://{text}")
        parts = [p for p in u.path.split("/") if p]
        if parts:
            if parts[0].startswith("@"):
                return ChannelRef("handle", parts[0])
            if parts[0] == "channel" and len(parts) > 1 and _CHANNEL_ID_RE.match(parts[1]):
                return ChannelRef("id", parts[1])
            if parts[0] == "user" and len(parts) > 1:
                return ChannelRef("username", parts[1])
            if parts[0] == "c" and len(parts) > 1:
                return ChannelRef("custom", parts[1])
    return ChannelRef("query", text)


def extract_comment_timestamps(text: str, max_seconds: float | None = None) -> list[float]:
    out: list[float] = []
    for h, m, s in _TIMESTAMP_RE.findall(text or ""):
        seconds = int(h or 0) * 3600 + int(m) * 60 + int(s)
        if max_seconds is not None and seconds > max_seconds:
            continue
        out.append(float(seconds))
    return out


def comment_hotspots(
    comments: list[Comment], duration: float | None, *, cluster_seconds: float = 8.0, top: int = 15
) -> list[dict[str, Any]]:
    """Turn "3:42 😂😂" style comments into weighted moments the audience already found memorable.

    Chapter lists (many timestamps in one comment) are down-weighted so they do not dominate.
    """
    points: list[tuple[float, float, str]] = []
    for c in comments:
        stamps = extract_comment_timestamps(c.text, duration)
        if not stamps:
            continue
        per = (1.0 + math.log1p(max(0, c.likes))) / len(stamps)
        if len(stamps) > 3:
            per *= 0.25
        for t in stamps:
            points.append((t, per, c.text[:160]))
    if not points:
        return []
    points.sort()
    clusters: list[dict[str, Any]] = []
    for t, w, sample in points:
        if clusters and t - clusters[-1]["_last"] <= cluster_seconds:
            cl = clusters[-1]
            cl["_wt"] += t * w
            cl["weight"] += w
            cl["count"] += 1
            cl["_last"] = t
            if w > cl["_best"]:
                cl["_best"], cl["sample"] = w, sample
        else:
            clusters.append({"_wt": t * w, "weight": w, "count": 1, "_last": t, "_best": w, "sample": sample})
    out = []
    max_w = max(c["weight"] for c in clusters)
    for c in clusters:
        out.append(
            {
                "time": round(c["_wt"] / c["weight"], 1),
                "weight": round(c["weight"], 3),
                "strength": round(c["weight"] / max_w, 3),
                "count": c["count"],
                "sample": c["sample"],
            }
        )
    out.sort(key=lambda c: c["weight"], reverse=True)
    return out[:top]


# --- API client --------------------------------------------------------------------------------


class YouTubeClient:
    def __init__(self, api_key: str | None, http: httpx.Client | None = None):
        self.api_key = api_key or ""
        self.http = http or httpx.Client(timeout=20.0, headers={"User-Agent": "ViralClipAI/0.1"})

    @property
    def has_key(self) -> bool:
        return bool(self.api_key)

    def _get(self, endpoint: str, params: dict[str, Any], cost: int) -> dict[str, Any]:
        if not self.api_key:
            raise MissingApiKey("YOUTUBE_API_KEY is niet ingesteld")
        params = {k: v for k, v in params.items() if v is not None}
        params["key"] = self.api_key
        try:
            resp = self.http.get(f"{API_BASE}/{endpoint}", params=params)
        except httpx.HTTPError as e:
            raise YouTubeError(f"YouTube API niet bereikbaar: {e}") from e
        record_usage("youtube", endpoint, units=cost)
        if resp.status_code == 200:
            return resp.json()
        reason = ""
        try:
            err = resp.json().get("error", {})
            reason = (err.get("errors") or [{}])[0].get("reason", "") or err.get("status", "")
            message = err.get("message", resp.text)
        except ValueError:
            message = resp.text
        if resp.status_code == 403 and reason in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
            raise QuotaExceeded("YouTube API quota is op voor vandaag (reset om middernacht Pacific Time)")
        if resp.status_code == 403 and reason == "commentsDisabled":
            return {"items": []}
        raise YouTubeError(f"YouTube API fout {resp.status_code} ({reason}): {message}")

    # channels ------------------------------------------------------------------

    @staticmethod
    def _channel_from_item(item: dict[str, Any]) -> ChannelInfo:
        sn = item.get("snippet", {})
        st = item.get("statistics", {})
        cd = item.get("contentDetails", {})
        thumbs = sn.get("thumbnails", {})
        thumb = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url")
        hidden = st.get("hiddenSubscriberCount", False)
        return ChannelInfo(
            channel_id=item["id"] if isinstance(item.get("id"), str) else item.get("id", {}).get("channelId"),
            title=sn.get("title", ""),
            handle=sn.get("customUrl"),
            description=sn.get("description"),
            thumbnail_url=thumb,
            subscriber_count=None if hidden or "subscriberCount" not in st else int(st["subscriberCount"]),
            video_count=int(st["videoCount"]) if "videoCount" in st else None,
            view_count=int(st["viewCount"]) if "viewCount" in st else None,
            uploads_playlist_id=(cd.get("relatedPlaylists") or {}).get("uploads"),
            country=sn.get("country"),
        )

    def get_channels(self, ids: list[str]) -> list[ChannelInfo]:
        out: list[ChannelInfo] = []
        for i in range(0, len(ids), 50):
            data = self._get(
                "channels",
                {"part": "snippet,statistics,contentDetails", "id": ",".join(ids[i : i + 50]), "maxResults": 50},
                cost=1,
            )
            out.extend(self._channel_from_item(it) for it in data.get("items", []))
        return out

    def get_channel_by_handle(self, handle: str) -> ChannelInfo | None:
        data = self._get(
            "channels", {"part": "snippet,statistics,contentDetails", "forHandle": handle.lstrip("@")}, cost=1
        )
        items = data.get("items", [])
        return self._channel_from_item(items[0]) if items else None

    def get_channel_by_username(self, username: str) -> ChannelInfo | None:
        data = self._get("channels", {"part": "snippet,statistics,contentDetails", "forUsername": username}, cost=1)
        items = data.get("items", [])
        return self._channel_from_item(items[0]) if items else None

    def search_channels(self, query: str, max_results: int = 8, language: str | None = None) -> list[ChannelInfo]:
        data = self._get(
            "search",
            {
                "part": "snippet",
                "type": "channel",
                "q": query,
                "maxResults": max(1, min(max_results, 25)),
                "relevanceLanguage": language,
            },
            cost=100,
        )
        ids = [it["id"]["channelId"] for it in data.get("items", []) if it.get("id", {}).get("channelId")]
        if not ids:
            return []
        details = {c.channel_id: c for c in self.get_channels(ids)}
        results = [details[i] for i in ids if i in details]
        return rank_channel_results(query, results)

    def resolve_channel(self, text: str, language: str | None = None) -> list[ChannelInfo]:
        ref = parse_channel_input(text)
        if ref.kind == "id":
            return self.get_channels([ref.value])
        if ref.kind == "handle":
            ch = self.get_channel_by_handle(ref.value)
            return [ch] if ch else self.search_channels(ref.value.lstrip("@"), language=language)
        if ref.kind == "username":
            ch = self.get_channel_by_username(ref.value)
            return [ch] if ch else self.search_channels(ref.value, language=language)
        if ref.kind == "custom":
            ch = self.get_channel_by_handle(ref.value)
            return [ch] if ch else self.search_channels(ref.value, language=language)
        # Free text: a single word might be a handle; try that first because it costs 1 unit instead of 100.
        results: list[ChannelInfo] = []
        compact = re.sub(r"\s+", "", ref.value)
        if compact and re.match(r"^[A-Za-z0-9._-]{3,30}$", compact):
            ch = self.get_channel_by_handle(compact)
            if ch:
                results.append(ch)
        for ch in self.search_channels(ref.value, language=language):
            if all(ch.channel_id != r.channel_id for r in results):
                results.append(ch)
        return rank_channel_results(ref.value, results)

    # videos --------------------------------------------------------------------

    def list_upload_ids(self, uploads_playlist_id: str, max_items: int = 50) -> list[str]:
        ids: list[str] = []
        page_token = None
        while len(ids) < max_items:
            data = self._get(
                "playlistItems",
                {
                    "part": "contentDetails",
                    "playlistId": uploads_playlist_id,
                    "maxResults": min(50, max_items - len(ids)),
                    "pageToken": page_token,
                },
                cost=1,
            )
            for it in data.get("items", []):
                vid = it.get("contentDetails", {}).get("videoId")
                if vid:
                    ids.append(vid)
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return ids

    def get_videos(self, ids: list[str]) -> list[VideoInfo]:
        out: list[VideoInfo] = []
        for i in range(0, len(ids), 50):
            data = self._get(
                "videos",
                {"part": "snippet,contentDetails,statistics", "id": ",".join(ids[i : i + 50]), "maxResults": 50},
                cost=1,
            )
            for it in data.get("items", []):
                out.append(self._video_from_item(it))
        return out

    @staticmethod
    def _video_from_item(it: dict[str, Any]) -> VideoInfo:
        sn = it.get("snippet", {})
        st = it.get("statistics", {})
        cd = it.get("contentDetails", {})
        thumbs = sn.get("thumbnails", {})
        thumb = (
            thumbs.get("maxres") or thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}
        ).get("url")
        duration = parse_duration(cd.get("duration"))
        title = sn.get("title", "")
        desc = sn.get("description") or ""
        tags = sn.get("tags") or []
        shorts_tag = "#shorts" in (title + " " + desc).lower() or any(t.lower() == "shorts" for t in tags)
        is_short = duration is not None and (duration <= 60 or (duration <= 180 and shorts_tag))
        return VideoInfo(
            video_id=it["id"],
            title=title,
            channel_id=sn.get("channelId"),
            channel_title=sn.get("channelTitle"),
            description=desc,
            published_at=parse_datetime(sn.get("publishedAt")),
            duration_seconds=duration,
            view_count=int(st["viewCount"]) if "viewCount" in st else None,
            like_count=int(st["likeCount"]) if "likeCount" in st else None,
            comment_count=int(st["commentCount"]) if "commentCount" in st else None,
            thumbnail_url=thumb,
            tags=tags[:30],
            language=sn.get("defaultAudioLanguage") or sn.get("defaultLanguage"),
            live_status=sn.get("liveBroadcastContent") or "none",
            is_short=is_short,
        )

    def get_top_comments(self, video_id: str, max_results: int = 100) -> list[Comment]:
        data = self._get(
            "commentThreads",
            {
                "part": "snippet",
                "videoId": video_id,
                "order": "relevance",
                "maxResults": max(1, min(max_results, 100)),
                "textFormat": "plainText",
            },
            cost=1,
        )
        out = []
        for it in data.get("items", []):
            top = it.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
            out.append(Comment(text=top.get("textOriginal") or top.get("textDisplay") or "", likes=int(top.get("likeCount", 0))))
        return out

    # key-less endpoints -----------------------------------------------------------

    def fetch_rss(self, channel_id: str) -> list[VideoInfo]:
        """Latest ~15 uploads of a channel. Free (no quota) and needs no API key."""
        try:
            resp = self.http.get(RSS_URL, params={"channel_id": channel_id})
        except httpx.HTTPError as e:
            raise YouTubeError(f"RSS feed niet bereikbaar: {e}") from e
        if resp.status_code != 200:
            raise YouTubeError(f"RSS feed gaf status {resp.status_code}")
        return parse_rss(resp.text)

    def fetch_oembed(self, video_id: str) -> VideoInfo | None:
        try:
            resp = self.http.get(
                OEMBED_URL, params={"url": f"https://www.youtube.com/watch?v={video_id}", "format": "json"}
            )
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        data = resp.json()
        return VideoInfo(
            video_id=video_id,
            title=data.get("title", ""),
            channel_title=data.get("author_name"),
            thumbnail_url=data.get("thumbnail_url"),
        )


_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


def parse_rss(xml_text: str) -> list[VideoInfo]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise YouTubeError(f"Ongeldige RSS feed: {e}") from e
    channel_title = root.findtext("atom:title", default=None, namespaces=_NS)
    out: list[VideoInfo] = []
    for entry in root.findall("atom:entry", _NS):
        vid = entry.findtext("yt:videoId", default="", namespaces=_NS)
        if not vid:
            continue
        link = entry.find("atom:link", _NS)
        href = link.get("href", "") if link is not None else ""
        group = entry.find("media:group", _NS)
        views = None
        thumb = None
        desc = None
        if group is not None:
            stats = group.find("media:community/media:statistics", _NS)
            if stats is not None and stats.get("views", "").isdigit():
                views = int(stats.get("views"))
            th = group.find("media:thumbnail", _NS)
            thumb = th.get("url") if th is not None else None
            desc = group.findtext("media:description", default=None, namespaces=_NS)
        out.append(
            VideoInfo(
                video_id=vid,
                title=entry.findtext("atom:title", default="", namespaces=_NS),
                channel_id=entry.findtext("yt:channelId", default=None, namespaces=_NS),
                channel_title=channel_title,
                description=desc,
                published_at=parse_datetime(entry.findtext("atom:published", default=None, namespaces=_NS)),
                view_count=views,
                thumbnail_url=thumb,
                is_short="/shorts/" in href,
            )
        )
    return out


def rank_channel_results(query: str, channels: list[ChannelInfo]) -> list[ChannelInfo]:
    """Order search hits by name similarity first, then by audience size (a query like "Gio" is
    ambiguous; the big Dutch creator should float to the top)."""
    q = re.sub(r"[^a-z0-9]", "", query.lower())

    def key(c: ChannelInfo) -> tuple[float, float]:
        name = re.sub(r"[^a-z0-9]", "", (c.title or "").lower())
        handle = re.sub(r"[^a-z0-9]", "", (c.handle or "").lower())
        if q and (name == q or handle == q):
            sim = 2.0
        elif q and (q in name or q in handle):
            sim = 1.0
        else:
            sim = 0.0
        subs = math.log10((c.subscriber_count or 0) + 1)
        return (sim, subs)

    return sorted(channels, key=key, reverse=True)
