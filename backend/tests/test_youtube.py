import httpx
import pytest
import respx

from app.services.youtube import (
    API_BASE,
    ChannelInfo,
    Comment,
    QuotaExceeded,
    YouTubeClient,
    comment_hotspots,
    extract_comment_timestamps,
    parse_channel_input,
    parse_duration,
    parse_rss,
    parse_video_id,
    rank_channel_results,
)

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
 <title>Enzo Knol</title>
 <entry>
  <id>yt:video:AAAAAAAAAAA</id>
  <yt:videoId>AAAAAAAAAAA</yt:videoId>
  <yt:channelId>UCaaaaaaaaaaaaaaaaaaaaaa</yt:channelId>
  <title>Mijn nieuwe huis!</title>
  <link rel="alternate" href="https://www.youtube.com/watch?v=AAAAAAAAAAA"/>
  <published>2026-09-29T10:00:00+00:00</published>
  <media:group>
   <media:thumbnail url="https://i.ytimg.com/vi/AAAAAAAAAAA/hqdefault.jpg" width="480" height="360"/>
   <media:description>desc</media:description>
   <media:community><media:statistics views="123456"/></media:community>
  </media:group>
 </entry>
 <entry>
  <yt:videoId>BBBBBBBBBBB</yt:videoId>
  <title>Short!</title>
  <link rel="alternate" href="https://www.youtube.com/shorts/BBBBBBBBBBB"/>
  <published>2026-09-28T10:00:00+00:00</published>
 </entry>
</feed>"""


def test_parse_duration():
    assert parse_duration("PT15M33S") == 933
    assert parse_duration("PT1H2M3S") == 3723
    assert parse_duration("P1DT1S") == 86401
    assert parse_duration("PT45S") == 45
    assert parse_duration("") is None
    assert parse_duration("garbage") is None


@pytest.mark.parametrize(
    "text,expected",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?t=42", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ&list=x", "dQw4w9WgXcQ"),
        ("youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://example.com/watch?v=dQw4w9WgXcQ", None),
        ("not a url", None),
    ],
)
def test_parse_video_id(text, expected):
    assert parse_video_id(text) == expected


def test_parse_channel_input():
    assert parse_channel_input("@EnzoKnol").kind == "handle"
    assert parse_channel_input("https://www.youtube.com/@Bankzitters/videos").value == "@Bankzitters"
    ref = parse_channel_input("https://www.youtube.com/channel/UCaaaaaaaaaaaaaaaaaaaaaa")
    assert (ref.kind, ref.value) == ("id", "UCaaaaaaaaaaaaaaaaaaaaaa")
    assert parse_channel_input("https://youtube.com/user/enzoknol").kind == "username"
    assert parse_channel_input("Enzo Knol").kind == "query"


def test_comment_timestamps_and_hotspots():
    assert extract_comment_timestamps("3:42 😂 and 1:02:03") == [222.0, 3723.0]
    assert extract_comment_timestamps("at 12:30 lol", max_seconds=600) == []
    comments = [
        Comment("3:42 😂😂😂", likes=900),
        Comment("3:45 hahaha ik ga dood", likes=300),
        Comment("10:00 wat een moment", likes=5),
        Comment("0:00 intro 1:00 deel 1 2:00 deel 2 3:00 deel 3 4:00 einde", likes=2000),  # chapter list
        Comment("geen timestamp", likes=10000),
    ]
    spots = comment_hotspots(comments, duration=900)
    assert spots[0]["count"] >= 2
    assert 220 <= spots[0]["time"] <= 246
    assert spots[0]["strength"] == 1.0
    assert all(0 <= s["strength"] <= 1 for s in spots)


def test_parse_rss():
    videos = parse_rss(RSS)
    assert [v.video_id for v in videos] == ["AAAAAAAAAAA", "BBBBBBBBBBB"]
    assert videos[0].view_count == 123456
    assert videos[0].channel_title == "Enzo Knol"
    assert videos[0].published_at.year == 2026
    assert videos[1].is_short is True and videos[0].is_short is False


def test_rank_channel_results_prefers_exact_name_then_size():
    chans = [
        ChannelInfo("UC1", "Gio Fanpage", subscriber_count=1_000),
        ChannelInfo("UC2", "Gio", handle="@gio", subscriber_count=2_000_000),
        ChannelInfo("UC3", "Giorgio Cooking", subscriber_count=5_000_000),
    ]
    ranked = rank_channel_results("Gio", chans)
    assert ranked[0].channel_id == "UC2"


def _channel_item(cid: str, title: str, subs: str = "1000") -> dict:
    return {
        "id": cid,
        "snippet": {"title": title, "customUrl": f"@{title.lower().replace(' ', '')}", "thumbnails": {"high": {"url": "https://x/y.jpg"}}},
        "statistics": {"subscriberCount": subs, "videoCount": "10"},
        "contentDetails": {"relatedPlaylists": {"uploads": "UU" + cid[2:]}},
    }


@respx.mock
def test_resolve_channel_by_name_uses_cheap_handle_lookup_first():
    route = respx.get(f"{API_BASE}/channels")
    route.side_effect = [
        httpx.Response(200, json={"items": [_channel_item("UCenzo00000000000000000", "Enzo Knol", "2700000")]}),
        httpx.Response(200, json={"items": [_channel_item("UCother0000000000000000", "Enzo Fan", "300")]}),
    ]
    respx.get(f"{API_BASE}/search").mock(
        return_value=httpx.Response(200, json={"items": [{"id": {"channelId": "UCother0000000000000000"}}]})
    )
    yt = YouTubeClient("key", http=httpx.Client())
    results = yt.resolve_channel("EnzoKnol")
    assert results[0].title == "Enzo Knol"
    assert results[0].subscriber_count == 2_700_000
    assert results[0].uploads_playlist_id == "UUenzo00000000000000000"
    assert len(results) == 2


@respx.mock
def test_quota_exceeded_is_reported():
    respx.get(f"{API_BASE}/videos").mock(
        return_value=httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded"}], "message": "quota"}})
    )
    yt = YouTubeClient("key", http=httpx.Client())
    with pytest.raises(QuotaExceeded):
        yt.get_videos(["AAAAAAAAAAA"])


@respx.mock
def test_get_videos_parses_metadata():
    respx.get(f"{API_BASE}/videos").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "AAAAAAAAAAA",
                        "snippet": {"title": "Vlog", "channelId": "UCx", "publishedAt": "2026-09-29T10:00:00Z", "liveBroadcastContent": "none"},
                        "contentDetails": {"duration": "PT21M10S"},
                        "statistics": {"viewCount": "5000", "likeCount": "300", "commentCount": "40"},
                    },
                    {
                        "id": "BBBBBBBBBBB",
                        "snippet": {"title": "kort #shorts", "publishedAt": "2026-09-29T10:00:00Z"},
                        "contentDetails": {"duration": "PT1M50S"},
                        "statistics": {},
                    },
                ]
            },
        )
    )
    yt = YouTubeClient("key", http=httpx.Client())
    a, b = yt.get_videos(["AAAAAAAAAAA", "BBBBBBBBBBB"])
    assert a.duration_seconds == 1270 and a.view_count == 5000 and not a.is_short
    assert b.is_short
