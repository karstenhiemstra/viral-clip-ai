"""Import a source video from a share link (Google Drive, Dropbox, OneDrive or any direct file URL).

Creators' clipping programs usually hand out their raw files as a Drive/Dropbox link. This module
downloads such a link safely:

* YouTube (and other platform page) URLs are refused on purpose: downloading from YouTube breaks its
  Terms of Service, so ViralClip never does it.
* SSRF protection: only http(s), and every hop (including redirects) must resolve to a public IP.
* Size limit (``MAX_UPLOAD_GB``) and a check that the response is a media file, not a web page.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import uuid
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx

from app.config import get_settings

BLOCKED_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com", "googlevideo.com", "ytimg.com", "tiktok.com",
                 "instagram.com")
MEDIA_TYPES = {
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/x-matroska": ".mkv", "video/webm": ".webm",
    "video/x-msvideo": ".avi", "audio/mpeg": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/wav": ".wav",
    "audio/x-wav": ".wav", "audio/ogg": ".ogg", "audio/flac": ".flac", "audio/aac": ".aac",
}


class RemoteMediaError(ValueError):
    pass


def normalize_share_link(url: str) -> str:
    """Turn common share links into direct-download links."""
    u = urlparse(url.strip())
    host = (u.hostname or "").lower()
    if host.endswith("drive.google.com"):
        m = re.search(r"/file/d/([A-Za-z0-9_-]{10,})", u.path)
        file_id = m.group(1) if m else parse_qs(u.query).get("id", [None])[0]
        if file_id:
            return f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
    if host.endswith("dropbox.com"):
        q = parse_qs(u.query)
        q["dl"] = ["1"]
        return urlunparse(u._replace(query=urlencode({k: v[0] for k, v in q.items()})))
    if host in ("1drv.ms", "onedrive.live.com") and "download" not in u.query:
        sep = "&" if u.query else ""
        return urlunparse(u._replace(query=u.query + sep + "download=1"))
    return url.strip()


def _check_host(url: str) -> None:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise RemoteMediaError("Alleen http(s)-links worden ondersteund")
    host = u.hostname.lower()
    if any(host == b or host.endswith("." + b) for b in BLOCKED_HOSTS):
        raise RemoteMediaError(
            "Links naar YouTube/TikTok/Instagram worden niet gedownload (dat is in strijd met hun voorwaarden). "
            "Gebruik het originele bestand of een Drive/Dropbox-link van de creator."
        )
    try:
        infos = socket.getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        if os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"):
            return  # resolution happens at the (egress) proxy, which enforces its own policy
        raise RemoteMediaError(f"Host {host} niet gevonden") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise RemoteMediaError("Deze link wijst naar een intern netwerkadres en wordt geweigerd")


def _filename_ext(resp: httpx.Response, url: str) -> str:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
    name = m.group(1) if m else Path(urlparse(url).path).name
    ext = Path(name).suffix.lower()
    if ext:
        return ext
    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    return MEDIA_TYPES.get(ctype, ".mp4")


def download(url: str, progress: Callable[[float], None] | None = None, max_redirects: int = 6) -> Path:
    """Download a share link to a temporary file. Returns the path (caller moves/deletes it)."""
    settings = get_settings()
    limit = int(settings.max_upload_gb * 1024**3)
    url = normalize_share_link(url)
    client = httpx.Client(timeout=httpx.Timeout(30.0, read=120.0), follow_redirects=False,
                          headers={"User-Agent": "ViralClipAI/0.1"})
    try:
        for _ in range(max_redirects + 1):
            _check_host(url)
            with client.stream("GET", url) as resp:
                if resp.is_redirect:
                    url = str(resp.url.join(resp.headers.get("location", "")))
                    continue
                if resp.status_code != 200:
                    raise RemoteMediaError(f"Download mislukt: HTTP {resp.status_code}")
                ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if ctype.startswith("text/html"):
                    raise RemoteMediaError(
                        "De link geeft een webpagina in plaats van een videobestand. Zet de deelinstelling op "
                        "'Iedereen met de link' of gebruik een directe downloadlink."
                    )
                total = int(resp.headers.get("content-length") or 0)
                if total and total > limit:
                    raise RemoteMediaError(f"Bestand is groter dan {settings.max_upload_gb:g} GB")
                ext = _filename_ext(resp, url)
                dst = settings.tmp_dir / f"import-{uuid.uuid4().hex}{ext}"
                done = 0
                try:
                    with dst.open("wb") as out:
                        for chunk in resp.iter_bytes(1024 * 1024):
                            done += len(chunk)
                            if done > limit:
                                raise RemoteMediaError(f"Bestand is groter dan {settings.max_upload_gb:g} GB")
                            out.write(chunk)
                            if progress and total:
                                progress(done / total)
                except Exception:
                    dst.unlink(missing_ok=True)
                    raise
                if done == 0:
                    dst.unlink(missing_ok=True)
                    raise RemoteMediaError("De link gaf een leeg bestand terug")
                return dst
        raise RemoteMediaError("Te veel doorverwijzingen")
    except httpx.HTTPError as e:
        raise RemoteMediaError(f"Download mislukt: {e}") from e
    finally:
        client.close()
