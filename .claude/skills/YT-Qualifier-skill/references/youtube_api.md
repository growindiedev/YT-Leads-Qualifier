# YouTube Data API v3 — Reference for YouTube Qualifier

All endpoints, parameters, and response shapes needed to build the qualifier module.

---

## Authentication

All requests require an API key as a query parameter:
```
?key={YOUTUBE_API_KEY}
```

Get a key: Google Cloud Console → Enable "YouTube Data API v3" → Create credentials → API Key.

---

## Step 1 — Find the Channel

### Endpoint: Search
```
GET https://www.googleapis.com/youtube/v3/search
```

**Parameters:**
```
part=snippet
type=channel
q={search_query}
maxResults=5
key={YOUTUBE_API_KEY}
```

**Cost:** 100 units per call

**Response shape:**
```json
{
  "items": [
    {
      "id": { "channelId": "UCxxxxxx" },
      "snippet": {
        "channelId": "UCxxxxxx",
        "title": "Channel Name",
        "description": "Channel description text"
      }
    }
  ]
}
```

**Usage:**
```python
import requests

def search_channel(query: str, api_key: str) -> list:
    url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "part": "snippet",
        "type": "channel",
        "q": query,
        "maxResults": 5,
        "key": api_key
    }
    response = requests.get(url, params=params)
    data = response.json()
    return data.get("items", [])
```

**Validation after search:**
Do not blindly accept the first result. Check that the channel title or description contains at least one of:
- Founder first name
- Founder last name  
- Company name
- Niche keyword

If none match → reject result, try next query.

---

## Step 2 — Get Channel's Upload Playlist ID

Every channel has a default uploads playlist. Its ID is the channel ID with `UC` replaced by `UU`.

```python
def get_uploads_playlist_id(channel_id: str) -> str:
    return "UU" + channel_id[2:]
```

No API call needed. Pure string manipulation.

---

## Step 3 — Fetch Recent Videos from Uploads Playlist

### Endpoint: PlaylistItems
```
GET https://www.googleapis.com/youtube/v3/playlistItems
```

**Parameters:**
```
part=snippet,contentDetails
playlistId={uploads_playlist_id}
maxResults=10
key={YOUTUBE_API_KEY}
```

**Cost:** 1 unit per call

**Response shape:**
```json
{
  "items": [
    {
      "snippet": {
        "title": "Video Title",
        "publishedAt": "2024-11-15T14:30:00Z",
        "resourceId": { "videoId": "xxxxxxxxxxx" }
      },
      "contentDetails": {
        "videoId": "xxxxxxxxxxx",
        "videoPublishedAt": "2024-11-15T14:30:00Z"
      }
    }
  ]
}
```

Results are returned newest-first by default.

**Usage:**
```python
def get_recent_uploads(playlist_id: str, api_key: str, max_results: int = 10) -> list:
    url = "https://www.googleapis.com/youtube/v3/playlistItems"
    params = {
        "part": "snippet,contentDetails",
        "playlistId": playlist_id,
        "maxResults": max_results,
        "key": api_key
    }
    response = requests.get(url, params=params)
    data = response.json()
    return data.get("items", [])
```

---

## Step 4 — Get Video Duration and Thumbnail Details

### Endpoint: Videos.list
```
GET https://www.googleapis.com/youtube/v3/videos
```

**Parameters:**
```
part=contentDetails,snippet
id={comma_separated_video_ids}
key={YOUTUBE_API_KEY}
```

**Cost:** 1 unit per call (batch up to 50 video IDs)

**Response shape:**
```json
{
  "items": [
    {
      "id": "xxxxxxxxxxx",
      "snippet": {
        "title": "Video Title",
        "description": "Video description",
        "thumbnails": {
          "maxres": { "url": "https://i.ytimg.com/vi/xxxxxxxxxxx/maxresdefault.jpg" },
          "default": { "url": "https://i.ytimg.com/vi/xxxxxxxxxxx/default.jpg" }
        }
      },
      "contentDetails": {
        "duration": "PT12M34S"
      }
    }
  ]
}
```

**Usage:**
```python
def get_video_details(video_ids: list, api_key: str) -> list:
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "part": "contentDetails,snippet",
        "id": ",".join(video_ids),
        "key": api_key
    }
    response = requests.get(url, params=params)
    data = response.json()
    return data.get("items", [])
```

---

## Parsing Duration

YouTube returns duration in ISO 8601 format. Parse it to seconds:

```python
import re

def parse_duration_seconds(duration: str) -> int:
    """Convert ISO 8601 duration to total seconds.
    Examples: PT45S -> 45, PT1M30S -> 90, PT12M34S -> 754, PT1H2M3S -> 3723
    """
    pattern = r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?'
    match = re.match(pattern, duration)
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds
```

A video is a Short if `parse_duration_seconds(duration) < 60`.

---

## Detecting Custom Thumbnails

Auto-generated YouTube thumbnails follow this URL pattern:
```
https://i.ytimg.com/vi/{videoId}/default.jpg
https://i.ytimg.com/vi/{videoId}/hqdefault.jpg
```

Custom thumbnails typically appear as `maxresdefault.jpg` or have a non-standard URL structure. However, the most reliable signal is whether `maxres` key exists in the thumbnails object — custom thumbnails always have a maxres version, auto-generated ones often don't.

```python
def has_custom_thumbnail(video_item: dict) -> bool:
    thumbnails = video_item.get("snippet", {}).get("thumbnails", {})
    return "maxres" in thumbnails
```

---

## Quota Tracking

Build a simple counter:

```python
class QuotaTracker:
    def __init__(self, daily_limit: int = 10000):
        self.used = 0
        self.daily_limit = daily_limit
        self.warn_threshold = 8000

    def consume(self, units: int, operation: str):
        self.used += units
        if self.used >= self.warn_threshold:
            print(f"⚠️  Quota warning: {self.used}/{self.daily_limit} units used after {operation}")
        if self.used >= self.daily_limit:
            raise Exception(f"Daily YouTube API quota exhausted ({self.used} units)")

    def status(self) -> str:
        return f"Quota: {self.used}/{self.daily_limit} units used"
```

---

## Error Handling

Common API errors and how to handle them:

| Error | Code | Action |
|---|---|---|
| Invalid API key | 400 | Raise immediately, stop session |
| Quota exceeded | 403 `quotaExceeded` | Return `QUOTA_EXCEEDED` condition, stop session |
| Channel not found | 404 | Try next search query |
| Private/terminated channel | 404 on playlist | Treat as Condition A |
| Rate limited | 429 | Wait 5 seconds, retry once |

```python
def handle_api_error(response: requests.Response) -> None:
    if response.status_code == 403:
        error = response.json().get("error", {})
        if any(e.get("reason") == "quotaExceeded" for e in error.get("errors", [])):
            raise QuotaExceededException("YouTube API daily quota exhausted")
    response.raise_for_status()
```

---

## Full Units Budget Per Prospect

| Operation | Units |
|---|---|
| Search by founder name | 100 |
| Search by company name (if needed) | 100 |
| PlaylistItems fetch | 1 |
| Videos.list for details | 1 |
| **Total per prospect** | **~102–202** |

At 200 units per prospect: ~50 prospects before quota.
At 102 units (one search hit): ~98 prospects before quota.