"""A faithful stand-in for the YouTube Data API v3 + channel RSS feed + oEmbed.

It speaks the documented wire format (same JSON shapes, paging tokens, error bodies), so tests exercise
the real ``YouTubeClient`` HTTP code: query parameters, parsing, paging, quota errors.

Use it in tests through ``respx`` (``stub.install(respx_mock)``) or as a real HTTP server
(``stub.serve(port)``) for end-to-end runs of the whole app.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

import httpx

API_KEY = "test-youtube-key"


def iso_duration(seconds: int) -> str:
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return "PT" + (f"{h}H" if h else "") + (f"{m}M" if m else "") + (f"{s}S" if s or not (h or m) else "")


def z(dt: datetime) -> str:
    return dt.replace(tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class StubChannel:
    id: str
    title: str
    handle: str
    subscribers: int = 100_000
    country: str = "NL"


@dataclass
class StubVideo:
    id: str
    channel_id: str
    title: str
    published: datetime
    duration: int = 900
    views: int = 50_000
    likes: int = 2_000
    comments: int = 300
    live: str = "none"
    description: str = ""
    top_comments: list[tuple[str, int]] = field(default_factory=list)


class YouTubeStub:
    def __init__(self, channels: list[StubChannel], videos: list[StubVideo], api_key: str = API_KEY,
                 asset_base: str | None = None, thumbnail: bytes | None = None):
        self.channels = {c.id: c for c in channels}
        self.videos = {v.id: v for v in videos}
        self.api_key = api_key
        self.calls: list[str] = []
        # Thumbnails: fake hosts by default; ``serve`` can host a real image for UI runs.
        self.asset_base = (asset_base or "https://i.ytimg.example").rstrip("/")
        self.thumbnail = thumbnail

    # --- helpers ---------------------------------------------------------------------------------
    def _uploads(self, channel_id: str) -> list[StubVideo]:
        return sorted((v for v in self.videos.values() if v.channel_id == channel_id), key=lambda v: v.published, reverse=True)

    def _channel_json(self, c: StubChannel) -> dict:
        thumb = f"{self.asset_base}/ch/{c.id}.jpg"
        return {
            "kind": "youtube#channel",
            "etag": "x",
            "id": c.id,
            "snippet": {
                "title": c.title,
                "description": f"Het officiële kanaal van {c.title}",
                "customUrl": c.handle.lower(),
                "publishedAt": "2012-01-01T00:00:00Z",
                "thumbnails": {k: {"url": thumb, "width": w, "height": w} for k, w in (("default", 88), ("medium", 240), ("high", 800))},
                "country": c.country,
            },
            "contentDetails": {"relatedPlaylists": {"likes": "", "uploads": "UU" + c.id[2:]}},
            "statistics": {
                "viewCount": str(c.subscribers * 150),
                "subscriberCount": str(c.subscribers),
                "hiddenSubscriberCount": False,
                "videoCount": str(len(self._uploads(c.id))),
            },
        }

    def _video_json(self, v: StubVideo) -> dict:
        ch = self.channels[v.channel_id]
        thumb = f"{self.asset_base}/vi/{v.id}"
        return {
            "kind": "youtube#video",
            "etag": "x",
            "id": v.id,
            "snippet": {
                "publishedAt": z(v.published),
                "channelId": v.channel_id,
                "title": v.title,
                "description": v.description,
                "thumbnails": {k: {"url": f"{thumb}/{k}.jpg"} for k in ("default", "medium", "high", "maxres")},
                "channelTitle": ch.title,
                "tags": ["vlog"],
                "liveBroadcastContent": v.live,
                "defaultAudioLanguage": "nl",
            },
            "contentDetails": {"duration": iso_duration(v.duration), "dimension": "2d", "definition": "hd"},
            "statistics": {"viewCount": str(v.views), "likeCount": str(v.likes), "favoriteCount": "0", "commentCount": str(v.comments)},
        }

    @staticmethod
    def _error(status: int, reason: str, message: str) -> tuple[int, str, bytes]:
        body = {"error": {"code": status, "message": message, "errors": [{"message": message, "domain": "global", "reason": reason}]}}
        return status, "application/json", json.dumps(body).encode()

    @staticmethod
    def _ok(obj: dict) -> tuple[int, str, bytes]:
        return 200, "application/json", json.dumps(obj).encode()

    # --- routing ---------------------------------------------------------------------------------
    def dispatch(self, path: str, params: dict[str, str]) -> tuple[int, str, bytes]:
        self.calls.append(path.rsplit("/", 1)[-1])
        if path.endswith("/feeds/videos.xml"):
            return self._rss(params.get("channel_id", ""))
        if path.endswith("/oembed"):
            vid = parse_qs(urlparse(params.get("url", "")).query).get("v", [""])[0]
            v = self.videos.get(vid)
            if not v:
                return 404, "text/plain", b"Not Found"
            return self._ok({"title": v.title, "author_name": self.channels[v.channel_id].title, "thumbnail_url": f"{self.asset_base}/vi/{vid}/hq.jpg"})
        if "key" not in params:
            return self._error(403, "forbidden", "Method doesn't allow unregistered callers (callers without established identity). Please use API Key or other form of API consumer identity to call this API.")
        if params["key"] != self.api_key:
            return self._error(400, "badRequest", "API key not valid. Please pass a valid API key.")
        endpoint = path.rsplit("/", 1)[-1]
        if endpoint == "channels":
            if "id" in params:
                items = [self._channel_json(self.channels[i]) for i in params["id"].split(",") if i in self.channels]
            elif "forHandle" in params:
                h = "@" + params["forHandle"].lstrip("@").lower()
                items = [self._channel_json(c) for c in self.channels.values() if c.handle.lower() == h]
            else:
                items = []
            return self._ok({"kind": "youtube#channelListResponse", "items": items, "pageInfo": {"totalResults": len(items)}})
        if endpoint == "search":
            q = params.get("q", "").lower()
            hits = [c for c in self.channels.values() if any(tok in c.title.lower() for tok in q.split())]
            items = [{"kind": "youtube#searchResult", "id": {"kind": "youtube#channel", "channelId": c.id},
                      "snippet": {"channelId": c.id, "title": c.title}} for c in hits]
            return self._ok({"kind": "youtube#searchListResponse", "items": items[: int(params.get("maxResults", 5))]})
        if endpoint == "playlistItems":
            pl = params.get("playlistId", "")
            if not pl.startswith("UU") or "UC" + pl[2:] not in self.channels:
                return self._error(404, "playlistNotFound", "The playlist identified with the request's playlistId parameter cannot be found.")
            uploads = self._uploads("UC" + pl[2:])
            offset = int(params.get("pageToken") or 0)
            size = int(params.get("maxResults", 5))
            page = uploads[offset : offset + size]
            body = {
                "kind": "youtube#playlistItemListResponse",
                "items": [{"kind": "youtube#playlistItem", "id": f"pi-{v.id}",
                           "contentDetails": {"videoId": v.id, "videoPublishedAt": z(v.published)}} for v in page],
                "pageInfo": {"totalResults": len(uploads), "resultsPerPage": size},
            }
            if offset + size < len(uploads):
                body["nextPageToken"] = str(offset + size)
            return self._ok(body)
        if endpoint == "videos":
            items = [self._video_json(self.videos[i]) for i in params.get("id", "").split(",") if i in self.videos]
            return self._ok({"kind": "youtube#videoListResponse", "items": items})
        if endpoint == "commentThreads":
            v = self.videos.get(params.get("videoId", ""))
            if v is None:
                return self._error(404, "videoNotFound", "The video identified by the videoId parameter could not be found.")
            items = [{"kind": "youtube#commentThread", "snippet": {"topLevelComment": {"snippet": {
                "textDisplay": t, "textOriginal": t, "likeCount": likes}}}} for t, likes in v.top_comments]
            return self._ok({"kind": "youtube#commentThreadListResponse", "items": items})
        return self._error(404, "notFound", "Not Found")

    def _rss(self, channel_id: str) -> tuple[int, str, bytes]:
        c = self.channels.get(channel_id)
        if c is None:
            return 404, "text/html", b"Not Found"
        entries = []
        for v in self._uploads(channel_id)[:15]:
            entries.append(
                f"""<entry><id>yt:video:{v.id}</id><yt:videoId>{v.id}</yt:videoId><yt:channelId>{c.id}</yt:channelId>
<title>{escape(v.title)}</title><link rel="alternate" href="https://www.youtube.com/watch?v={v.id}"/>
<published>{z(v.published)}</published><media:group><media:title>{escape(v.title)}</media:title>
<media:thumbnail url="{self.asset_base}/vi/{v.id}/hqdefault.jpg" width="480" height="360"/>
<media:description>{escape(v.description)}</media:description>
<media:community><media:statistics views="{v.views}"/></media:community></media:group></entry>"""
            )
        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
<title>{escape(c.title)}</title>{''.join(entries)}</feed>"""
        return 200, "application/atom+xml", xml.encode()

    # --- adapters --------------------------------------------------------------------------------
    def httpx_handler(self, request: httpx.Request) -> httpx.Response:
        params = {k: v for k, v in request.url.params.items()}
        status, ctype, body = self.dispatch(request.url.path, params)
        return httpx.Response(status, content=body, headers={"content-type": ctype})

    def client(self, api_key: str | None = API_KEY):
        """A real YouTubeClient whose HTTP traffic is served by this stub."""
        from app.services.youtube import YouTubeClient

        transport = httpx.MockTransport(self.httpx_handler)
        return YouTubeClient(api_key, http=httpx.Client(transport=transport),
                             api_base="https://www.googleapis.com/youtube/v3", web_base="https://www.youtube.com")

    def serve(self, port: int, host: str = "127.0.0.1") -> ThreadingHTTPServer:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                u = urlparse(self.path)
                params = {k: v[0] for k, v in parse_qs(u.query).items()}
                if stub.thumbnail and u.path.startswith(("/ch/", "/vi/")):
                    status, ctype, body = 200, "image/jpeg", stub.thumbnail
                else:
                    status, ctype, body = stub.dispatch(u.path, params)
                self.send_response(status)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer((host, port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server
