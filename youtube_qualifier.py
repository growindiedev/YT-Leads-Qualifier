from __future__ import annotations

import os
import re
import sys
import json
import time
import logging
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import anthropic

load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def parse_duration(iso_duration: str) -> int:
    """Convert ISO 8601 duration (PT1H2M3S) to total seconds."""
    pattern = r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?"
    match = re.match(pattern, iso_duration)
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def find_channel_id_from_url(url: str) -> str | None:
    """Extract channel ID or handle from any YouTube URL format."""
    patterns = [
        r"youtube\.com/channel/([A-Za-z0-9_-]+)",
        r"youtube\.com/@([A-Za-z0-9_.-]+)",
        r"youtube\.com/c/([A-Za-z0-9_.-]+)",
        r"youtube\.com/user/([A-Za-z0-9_.-]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def resolve_channel_id(identifier: str, _url: str) -> str | None:
    """
    Given a raw identifier from a URL, resolve to a channel ID.
    Handles both raw channel IDs (UC...) and handles/custom names.
    """
    if identifier.startswith("UC"):
        return identifier

    # Try forHandle lookup first
    try:
        resp = youtube.channels().list(part="id", forHandle=identifier).execute()
        items = resp.get("items", [])
        if items:
            return items[0]["id"]
    except Exception:
        pass

    # Try search as fallback
    try:
        resp = youtube.search().list(
            part="snippet", q=identifier, type="channel", maxResults=1
        ).execute()
        items = resp.get("items", [])
        if items:
            return items[0]["snippet"]["channelId"]
    except Exception:
        pass

    return None


def format_videos_for_prompt(videos: list) -> str:
    lines = []
    for i, v in enumerate(videos, 1):
        lines.append(
            f"{i}. \"{v['title']}\"\n"
            f"   Duration: {v['duration_seconds']}s | Published: {v['published_at'].strftime('%Y-%m-%d')}\n"
            f"   Description: {v['description'][:200]}"
        )
    return "\n\n".join(lines)


STOP_WORDS = {
    "the", "and", "for", "inc", "llc", "ltd", "corp", "group",
    "solutions", "consulting", "services", "company", "co", "agency",
    "digital", "media", "marketing", "partners", "with", "from",
    "that", "this", "your", "our", "its", "has", "are", "was",
    "have", "been", "not", "but", "you", "all", "can", "her",
    "his", "they", "them", "their", "ventures", "global", "labs",
    "studio", "studios", "creative", "brand", "brands", "growth",
    "strategy", "strategies", "advisory", "advisors", "management",
    # Generic descriptor words that appear in many company names / descriptions
    "firm", "unknown", "team", "west", "east", "north", "south",
    "real", "true", "next", "best", "plus", "pros", "works",
    "house", "home", "zone", "core", "base", "peak", "edge",
    "open", "bold", "rise", "wave", "wire", "link", "flow",
}


def _name_match(text: str, person_name: str, company_name: str) -> bool:
    text_lower = text.lower()

    def meaningful_tokens(name: str) -> list:
        return [
            w for w in name.lower().split()
            if len(w) > 3 and w not in STOP_WORDS
        ]

    person_tokens  = meaningful_tokens(person_name)
    company_tokens = meaningful_tokens(company_name)

    # If we can't extract meaningful tokens, can't disqualify — let it through
    if not person_tokens and not company_tokens:
        return True

    # Person name match: ALL meaningful person tokens appear in channel text
    if person_tokens:
        if all(t in text_lower for t in person_tokens):
            return True

    # Company name match: at least 2 meaningful company tokens, or 1 if only 1 exists
    if company_tokens:
        matches = sum(1 for t in company_tokens if t in text_lower)
        threshold = min(2, len(company_tokens))
        if matches >= threshold:
            return True

    return False


# ---------------------------------------------------------------------------
# Channel discovery
# ---------------------------------------------------------------------------

def search_youtube_channels(query: str, max_results: int = 5) -> list:
    """Wrapper for YouTube search API, returns list of channel dicts."""
    try:
        resp = youtube.search().list(
            part="snippet", q=query, type="channel", maxResults=max_results
        ).execute()
        return resp.get("items", [])
    except HttpError as e:
        if e.resp.status == 403:
            raise
        logger.warning(f"YouTube search error: {e}")
        return []


def _fetch_with_retry(url: str, timeout: int = 10) -> requests.Response | None:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        )
    }
    for attempt in range(2):
        try:
            return requests.get(url, headers=headers, timeout=timeout)
        except requests.Timeout:
            if attempt == 0:
                time.sleep(3)
            else:
                return None
        except requests.RequestException as e:
            logger.warning(f"Request error: {e}")
            return None
    return None


def _discover_channel(person_name: str, company_name: str, website_url: str | None) -> str | None:
    """
    4-stage channel discovery:
      Stage 1: YouTube search by person name
      Stage 2: YouTube search by company name
      Stage 3: Scrape company website for YouTube links
      Stage 4: YouTube search by combined "person name + company name"

    Max quota cost: 300 units per person (3 API searches).
    Website scraping (Stage 3) is free but may fail silently.
    """
    # Stage 1: by person name
    try:
        results = search_youtube_channels(person_name)
        for item in results:
            snippet = item["snippet"]
            title = snippet.get("title", "")
            description = snippet.get("description", "")
            if _name_match(title + " " + description, person_name, company_name):
                return snippet["channelId"]
    except HttpError as e:
        if e.resp.status == 403:
            raise
    time.sleep(0.3)

    # Stage 2: by company name
    try:
        results = search_youtube_channels(company_name)
        for item in results:
            snippet = item["snippet"]
            title = snippet.get("title", "")
            description = snippet.get("description", "")
            if _name_match(title + " " + description, person_name, company_name):
                return snippet["channelId"]
    except HttpError as e:
        if e.resp.status == 403:
            raise
    time.sleep(0.3)

    # Stage 3: scrape website for YouTube link
    if website_url:
        resp = _fetch_with_retry(website_url)
        if resp and resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup.find_all("a", href=True):
                href = tag["href"]
                if any(p in href for p in ["youtube.com/channel/", "youtube.com/c/", "youtube.com/@", "youtube.com/user/"]):
                    identifier = find_channel_id_from_url(href)
                    if identifier:
                        channel_id = resolve_channel_id(identifier, href)
                        if channel_id:
                            return channel_id

    # Stage 4: combined person + company search
    combined_query = f"{person_name} {company_name}"
    try:
        results = search_youtube_channels(combined_query, max_results=3)
        for item in results:
            snippet = item["snippet"]
            title = snippet.get("title", "")
            desc  = snippet.get("description", "")
            if _name_match(title + " " + desc, person_name, company_name):
                return snippet["channelId"]
    except HttpError as e:
        if e.resp.status == 403:
            raise
    time.sleep(0.3)

    return None


# ---------------------------------------------------------------------------
# Channel data fetching
# ---------------------------------------------------------------------------

def get_channel_videos(channel_id: str, max_results: int = 10) -> tuple[list, dict]:
    """
    Returns (videos, channel_info) where:
      - videos is a list of video dicts
      - channel_info contains channel_url, channel_name, upload_count
    """
    # Get channel details + uploads playlist
    ch_resp = youtube.channels().list(
        part="contentDetails,snippet,statistics",
        id=channel_id
    ).execute()

    items = ch_resp.get("items", [])
    if not items:
        return [], {}

    ch = items[0]
    uploads_playlist_id = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    channel_name = ch["snippet"]["title"]
    channel_url = f"https://www.youtube.com/channel/{channel_id}"
    upload_count = int(ch["statistics"].get("videoCount", 0))

    channel_info = {
        "channel_url": channel_url,
        "channel_name": channel_name,
        "upload_count": upload_count,
    }

    # Fetch recent videos from uploads playlist
    pl_resp = youtube.playlistItems().list(
        part="snippet,contentDetails",
        playlistId=uploads_playlist_id,
        maxResults=max_results
    ).execute()

    playlist_items = pl_resp.get("items", [])
    if not playlist_items:
        return [], channel_info

    video_ids = [item["contentDetails"]["videoId"] for item in playlist_items]

    # Fetch video details (duration etc.)
    vid_resp = youtube.videos().list(
        part="contentDetails,snippet",
        id=",".join(video_ids)
    ).execute()

    video_details = {v["id"]: v for v in vid_resp.get("items", [])}

    videos = []
    for item in playlist_items:
        vid_id = item["contentDetails"]["videoId"]
        detail = video_details.get(vid_id, {})
        snippet = detail.get("snippet", item.get("snippet", {}))
        content = detail.get("contentDetails", {})

        published_raw = snippet.get("publishedAt", "")
        try:
            published_at = datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
        except Exception:
            published_at = datetime.now(timezone.utc)

        duration_iso = content.get("duration", "PT0S")
        thumbnails = snippet.get("thumbnails", {})
        thumbnail_url = (
            thumbnails.get("maxres", {}).get("url")
            or thumbnails.get("high", {}).get("url")
            or ""
        )

        videos.append({
            "title": snippet.get("title", ""),
            "description": snippet.get("description", "")[:500],
            "published_at": published_at,
            "duration_seconds": parse_duration(duration_iso),
            "thumbnail_url": thumbnail_url,
            "video_url": f"https://www.youtube.com/watch?v={vid_id}",
        })

    return videos, channel_info


# ---------------------------------------------------------------------------
# Stage 1 — deterministic conditions
# ---------------------------------------------------------------------------

def _run_stage_1(videos: list, channel_info: dict) -> dict | None:
    """Returns a result dict if a condition is triggered, else None."""
    now = datetime.now(timezone.utc)

    # Condition B — Dead Channel (no uploads in 60+ days)
    most_recent = videos[0]["published_at"]
    days_since = (now - most_recent).days
    if days_since > 60:
        return {
            "condition": "B",
            "reasoning": f"Last upload was {days_since} days ago (>{60} day threshold)",
            "stage": 1,
            **channel_info,
            "last_upload_date": most_recent.date().isoformat(),
            "upload_count": channel_info.get("upload_count", 0),
        }

    # Condition C — Inconsistent Poster (60+ day gap between recent uploads)
    if len(videos) >= 2:
        gap = (videos[0]["published_at"] - videos[1]["published_at"]).days
        if gap > 60:
            return {
                "condition": "C",
                "reasoning": f"Gap of {gap} days between most recent two uploads",
                "stage": 1,
                **channel_info,
                "last_upload_date": most_recent.date().isoformat(),
                "upload_count": channel_info.get("upload_count", 0),
            }

    if len(videos) >= 3:
        gap2 = (videos[1]["published_at"] - videos[2]["published_at"]).days
        if gap2 > 60:
            return {
                "condition": "C",
                "reasoning": f"Gap of {gap2} days between uploads 2 and 3",
                "stage": 1,
                **channel_info,
                "last_upload_date": most_recent.date().isoformat(),
                "upload_count": channel_info.get("upload_count", 0),
            }

    # Condition E — Shorts Only (all videos <= 60 seconds)
    long_form = [v for v in videos if v["duration_seconds"] > 60]
    if len(long_form) == 0:
        return {
            "condition": "E",
            "reasoning": "All recent videos are Shorts (<=60 seconds)",
            "stage": 1,
            **channel_info,
            "last_upload_date": most_recent.date().isoformat(),
            "upload_count": channel_info.get("upload_count", 0),
        }

    return None  # proceed to Stage 2


# ---------------------------------------------------------------------------
# Stage 2 — Claude judgment
# ---------------------------------------------------------------------------

def _run_stage_2(videos: list, channel_info: dict, person_name: str, company_name: str) -> dict:
    most_recent = videos[0]["published_at"]

    prompt = f"""You are evaluating a YouTube channel for a B2B content agency.
Classify this channel into one of: D, F, or FAIL.

CHANNEL DATA:
Person: {person_name}
Company: {company_name}

LAST 5 VIDEOS:
{format_videos_for_prompt(videos[:5])}

RESPOND WITH ONLY A JSON OBJECT:
{{
  "condition": "D" or "F" or "FAIL",
  "reasoning": "one sentence explanation"
}}

CONDITION D (PASS) — exclusively raw, unedited podcast/webinar/interview content:
- Titles suggest episode format (Ep., #123, "with [guest]", "interview", "webinar")
- No editing, motion graphics, or production value
- No direct-to-camera scripted content from the founder

CONDITION F (PASS) — channel posts regularly but content is unrelated to their business/offer:
- Personal vlogs, hobby content, or lifestyle videos with no connection to their professional offer
- Motivational or generic content not tied to their industry or service
- No videos that would attract their target B2B audience
- They have a channel but it does nothing to sell or support their business

FAIL — channel already has strong business-relevant content:
- Direct-to-camera scripted content from the founder about their industry/offer
- Educational or authority-building content relevant to their business
- Produced and edited — custom thumbnails, branded graphics
- SEO-optimised titles targeting their B2B audience

If genuinely unclear between D and F, default to D."""

    try:
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE).strip()
        parsed = json.loads(raw)
        condition = parsed.get("condition", "D")
        reasoning = parsed.get("reasoning", "")
    except json.JSONDecodeError:
        logger.warning("Claude returned invalid JSON — defaulting to Condition D")
        condition = "D"
        reasoning = "Claude response could not be parsed; defaulting to safe pass"
    except Exception as e:
        logger.warning(f"Claude API error: {e} — defaulting to Condition D")
        condition = "D"
        reasoning = f"Claude API error; defaulting to safe pass"

    return {
        "condition": condition,
        "reasoning": reasoning,
        "stage": 2,
        **channel_info,
        "last_upload_date": most_recent.date().isoformat(),
        "upload_count": channel_info.get("upload_count", 0),
    }


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def qualify_youtube(person_name: str, company_name: str, website_url: str = None, no_claude: bool = False) -> dict:
    """
    Qualify a YouTube channel for a given person/company.

    Returns a dict with keys:
        condition, channel_url, channel_name, last_upload_date,
        upload_count, reasoning, stage
    """
    base = {
        "condition": "A",
        "channel_url": None,
        "channel_name": None,
        "last_upload_date": None,
        "upload_count": 0,
        "reasoning": "",
        "stage": 1,
    }

    # --- Step 1: Discover channel ---
    try:
        channel_id = _discover_channel(person_name, company_name, website_url)
    except HttpError as e:
        if e.resp.status == 403:
            return {**base, "condition": "ERROR", "reasoning": "YouTube API quota exceeded"}
        return {**base, "condition": "ERROR", "reasoning": f"YouTube API error: {e}"}

    if not channel_id:
        return {**base, "reasoning": "No channel found after exhaustive search"}

    # --- Step 2: Fetch channel data ---
    try:
        videos, channel_info = get_channel_videos(channel_id)
    except HttpError as e:
        if e.resp.status == 403:
            return {**base, "condition": "ERROR", "reasoning": "YouTube API quota exceeded"}
        return {**base, "condition": "ERROR", "reasoning": f"YouTube API error: {e}"}

    if not videos:
        # Channel found but no videos — treat as dead
        return {
            **base,
            "condition": "B",
            "reasoning": "Channel found but has no videos",
            **channel_info,
        }

    # --- Step 3: Stage 1 logic ---
    stage1_result = _run_stage_1(videos, channel_info)
    if stage1_result:
        return {**base, **stage1_result}

    # --- Step 4: Stage 2 Claude judgment ---
    if no_claude:
        # Output raw data for external judgment (e.g. Claude Code skill)
        most_recent = videos[0]["published_at"]
        return {
            **base,
            "condition": "STAGE2_NEEDED",
            "stage": 2,
            **channel_info,
            "last_upload_date": most_recent.date().isoformat(),
            "upload_count": channel_info.get("upload_count", 0),
            "videos": [
                {**v, "published_at": v["published_at"].isoformat()}
                for v in videos[:5]
            ],
        }
    return {**base, **_run_stage_2(videos, channel_info, person_name, company_name)}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--test-name-match" in sys.argv:
        assert _name_match("Mike McCalley Revenue Strategy", "Mike McCalley", "The Vertical Solution")
        assert _name_match("StraDGy 360 Business Channel", "Zoe Fairfax", "StraDGy 360")
        assert not _name_match("The Best Marketing Tips Channel", "John Smith", "Solutions Inc")
        assert _name_match("Anything at all", "Bo Li", "IQ Co")
        print("All _name_match tests passed.")
        sys.exit(0)

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    no_claude = "--no-claude" in sys.argv

    person = args[0] if len(args) > 0 else "Test Person"
    company = args[1] if len(args) > 1 else "Test Company"
    website = args[2] if len(args) > 2 else None

    result = qualify_youtube(person, company, website, no_claude=no_claude)

    if result.get("condition") == "STAGE2_NEEDED":
        print(json.dumps(result))
        sys.exit(0)

    print("\nRESULT:")
    print(f"Condition:    {result['condition']}")
    print(f"Channel:      {result['channel_name']}")
    print(f"Channel URL:  {result['channel_url']}")
    print(f"Last Upload:  {result['last_upload_date']}")
    print(f"Upload Count: {result['upload_count']}")
    print(f"Resolved By:  Stage {result['stage']}")
    print(f"Reasoning:    {result['reasoning']}")
