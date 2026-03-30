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
    # Generic service/industry descriptors
    "design", "online", "website", "technology", "technologies",
    "training", "coaching", "education", "learning", "development",
    "sales", "revenue", "business", "enterprise", "professional",
}


def _word_in_text(word: str, text: str) -> bool:
    """True if word appears as a whole word in text (case-insensitive, pre-lowered text)."""
    return bool(re.search(r"\b" + re.escape(word) + r"\b", text))


def _core_company_name(company_name: str) -> str:
    """
    Extract the core brand name from a long or descriptive company name.
    Returns the first meaningful word (> 3 chars, not a stop word).
    Falls back to the full company name if no such word is found.

    Examples:
      "Foloware Website design & Online lead generation" → "Foloware"
      "Corridor Strategy Group"                          → "Corridor"
      "LEVEL mpls"                                       → "LEVEL"
      "JW Design"                                        → "JW Design"  (no long non-stop word)
    """
    for word in company_name.split():
        clean = word.strip(".,&|/-").lower()
        if len(clean) > 3 and clean not in STOP_WORDS:
            return word.strip(".,&|/-")
    return company_name


def _name_match(text: str, person_name: str, company_name: str) -> bool:
    text_lower = text.lower()

    def meaningful_tokens(name: str, min_len: int = 4) -> list:
        return [
            w for w in name.lower().split()
            if len(w) > min_len and w not in STOP_WORDS
        ]

    person_tokens  = meaningful_tokens(person_name, min_len=4)
    company_tokens = meaningful_tokens(company_name, min_len=4)

    # Fallback: if filtering removed all tokens, relax min_len.
    # Short tokens (< 4 chars) are only accepted as whole words — the
    # previous substring check caused "jus" to match "just", etc.
    if not person_tokens:
        person_tokens = meaningful_tokens(person_name, min_len=2)
    if not company_tokens:
        company_tokens = meaningful_tokens(company_name, min_len=2)

    # If we can't extract meaningful tokens from BOTH sides, reject rather
    # than accepting anything. An empty match means we have no signal.
    if not person_tokens and not company_tokens:
        return False

    # Person name match — first + last name must appear as whole words.
    # Middle names / extra tokens are optional (handles "Michael John Smith"
    # where the channel says "Michael Smith").
    if person_tokens:
        if len(person_tokens) <= 2:
            match = all(_word_in_text(t, text_lower) for t in person_tokens)
        else:
            # 3+ tokens: require first and last; skip middle tokens
            match = (
                _word_in_text(person_tokens[0], text_lower)
                and _word_in_text(person_tokens[-1], text_lower)
            )
        if match:
            return True

    # Company name match: at least 2 meaningful tokens (or 1 if only 1 exists)
    # must appear as whole words.
    # Guard: if the only surviving tokens are very short (≤3 chars after
    # fallback), require the full company name bounded by non-alphanumeric chars
    # rather than trusting a single short token — prevents "IQ" matching "unique".
    if company_tokens:
        if all(len(t) <= 3 for t in company_tokens):
            pattern = r"(?<![a-zA-Z0-9])" + re.escape(company_name.lower()) + r"(?![a-zA-Z0-9])"
            return bool(re.search(pattern, text_lower))
        matches = sum(1 for t in company_tokens if _word_in_text(t, text_lower))
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


# Pages to check per website, in discovery order
WEBSITE_PAGES_TO_CHECK = ["", "/about", "/contact", "/team", "/about-us", "/contact-us"]


def _extract_youtube_channel_links(soup: BeautifulSoup) -> list:
    """
    Extract all YouTube channel/handle URLs from a BeautifulSoup object.
    Returns a deduplicated list of raw YouTube URLs.

    Includes:  youtube.com/channel/UC..., youtube.com/@handle, youtube.com/c/name
    Excludes:  watch?v=, /embed/, /playlist, youtu.be
    """
    CHANNEL_PATTERNS = [
        r"youtube\.com/channel/([A-Za-z0-9_-]{10,})",
        r"youtube\.com/@([A-Za-z0-9_.\-]{3,})",
        r"youtube\.com/c/([A-Za-z0-9_.\-]{3,})",
        r"youtube\.com/user/([A-Za-z0-9_.\-]{3,})",
    ]
    EXCLUDE_PATTERNS = ["watch?v=", "/embed/", "/playlist", "youtu.be"]

    found = set()
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if any(ex in href for ex in EXCLUDE_PATTERNS):
            continue
        if "youtube.com" not in href:
            continue
        for pattern in CHANNEL_PATTERNS:
            if re.search(pattern, href):
                found.add(href)
                break

    return list(found)


def _resolve_youtube_url_to_channel_id(url: str) -> str | None:
    """Convert any YouTube channel URL format to a channel ID (UC...)."""
    identifier = find_channel_id_from_url(url)
    if not identifier:
        return None
    return resolve_channel_id(identifier, url)


def _get_channel_subscriber_count(channel_id: str) -> "int | None":
    """Return subscriber count or None on failure. Costs 1 quota unit."""
    try:
        resp = youtube.channels().list(
            part="statistics", id=channel_id
        ).execute()
        items = resp.get("items", [])
        if not items:
            return None
        count_str = items[0]["statistics"].get("subscriberCount", "")
        return int(count_str) if count_str else None
    except Exception:
        return None


def _validate_channel_ownership(
    channel_id: str,
    person_name: str,
    company_name: str,
    company_website: str,
) -> bool:
    """
    Confirm a YouTube channel belongs to the given person/company.
    Calls channels.list(part="snippet") — 1 quota unit.
    Checks channel title, description, and customUrl against person/company name.
    Also checks if the company website domain appears in the channel description.
    """
    try:
        resp = youtube.channels().list(part="snippet", id=channel_id).execute()
        items = resp.get("items", [])
        if not items:
            return False
        snippet = items[0]["snippet"]
        text = (
            snippet.get("title", "") + " "
            + snippet.get("description", "") + " "
            + snippet.get("customUrl", "")
        )
        if _name_match(text, person_name, company_name):
            return True
        if company_website:
            from urllib.parse import urlparse
            raw = company_website if company_website.startswith("http") else "https://" + company_website
            domain = urlparse(raw).netloc.lstrip("www.")
            # Use boundary anchors so "acme.com" doesn't match "myacme.com"
            if domain:
                pattern = r"(?<![a-zA-Z0-9\-])" + re.escape(domain) + r"(?![a-zA-Z0-9])"
                if re.search(pattern, snippet.get("description", "").lower()):
                    return True
        return False
    except Exception:
        return False


def _find_channel_via_ddg(
    person_name: str,
    company_name: str,
    company_website: str,
) -> "str | None":
    """
    Search DuckDuckGo for a YouTube channel URL, then validate ownership.
    Free — no API key required. Costs at most 1 YouTube quota unit (channels.list
    for validation), vs 100 units for search.list.

    Tries three query variants in order, stopping at the first validated hit:
      1. person + company — most specific
      2. company + "youtube channel" — catches channels not mentioning the person
      3. site:youtube.com + company — direct site search
    Returns channel_id or None.
    """
    try:
        from ddgs import DDGS
    except ImportError:
        return None

    core_name = _core_company_name(company_name)
    queries = [
        f'"{person_name}" "{company_name}" youtube',
        f'"{company_name}" youtube channel',
        f'site:youtube.com "{company_name}"',
    ]
    # For long/descriptive company names add core-brand fallbacks
    if core_name != company_name:
        queries += [
            f'"{person_name}" "{core_name}" youtube',
            f'"{core_name}" youtube channel',
        ]

    # Track both raw identifiers (handles/UC ids from URLs) and resolved channel IDs.
    # Checking identifiers before resolution avoids repeated API calls for the same
    # handle across different queries.
    seen_identifiers: set = set()
    seen_channel_ids: set = set()

    for query in queries:
        try:
            with DDGS(verify=False) as ddgs:
                results = list(ddgs.text(query, max_results=5))
        except Exception:
            continue

        for r in results:
            url = r.get("href", "")
            if "youtube.com" not in url:
                continue
            if any(skip in url for skip in ("watch?v=", "/playlist", "/embed", "youtu.be")):
                continue
            identifier = find_channel_id_from_url(url)
            if not identifier or identifier in seen_identifiers:
                continue
            seen_identifiers.add(identifier)

            channel_id = resolve_channel_id(identifier, url)
            if not channel_id or channel_id in seen_channel_ids:
                continue
            seen_channel_ids.add(channel_id)

            # Skip channels too large to be a small B2B founder's channel
            sub_count = _get_channel_subscriber_count(channel_id)
            if sub_count is not None and sub_count > 200_000:
                logger.info(
                    f"  DDG: skipping channel {channel_id} "
                    f"({sub_count:,} subs, too large)"
                )
                continue

            # DDG search already filtered for relevance — trust it and accept the first hit
            # (strict validation rejected too many legitimate founder channels)
            logger.info(
                f"  DDG found channel {channel_id} for {person_name} / {company_name}"
            )
            return channel_id

    return None


def _get_channel_website(channel_id: str) -> str | None:
    """
    Attempt to retrieve the website URL listed on a YouTube channel's About page.
    Uses a best-effort HTTP scrape; returns None on failure or if not listed.
    """
    url = f"https://www.youtube.com/channel/{channel_id}/about"
    resp = _fetch_with_retry(url, timeout=8)
    if not resp or resp.status_code != 200:
        return None
    # YouTube About pages are JS-rendered; look for website in raw HTML metadata
    # The channel website is often embedded in og:url or structured data
    match = re.search(r'"url"\s*:\s*"(https?://(?!www\.youtube)[^"]+)"', resp.text)
    if match:
        candidate = match.group(1)
        # Filter out YouTube and Google URLs
        if "youtube.com" not in candidate and "google.com" not in candidate:
            return candidate
    return None


def _websites_match(url1: str, url2: str) -> bool:
    """
    Compare two website URLs to check if they refer to the same domain.
    Strips scheme, www prefix, trailing slashes, and paths.

    Examples:
        https://www.acme.com/about  vs  http://acme.com  → True
        https://acme.com           vs  betacorp.com     → False
        acme.com                   vs  https://www.acme.com → True
    """
    from urllib.parse import urlparse

    def normalise(url: str) -> str:
        if not url:
            return ""
        if not url.startswith("http"):
            url = "https://" + url
        parsed = urlparse(url)
        domain = parsed.netloc.lower().lstrip("www.")
        return domain.rstrip("/")

    d1 = normalise(url1)
    d2 = normalise(url2)
    return bool(d1 and d2 and d1 == d2)


def _scrape_website_for_channel(
    base_url: str,
    person_name: str,
    company_name: str,
) -> str | None:
    """
    Scrape up to 6 pages of a website looking for YouTube channel links.
    Returns a channel_id string if found, None otherwise.
    Stops as soon as one valid YouTube channel link is found.
    """
    from urllib.parse import urlparse

    if not base_url.startswith("http"):
        base_url = "https://" + base_url

    parsed = urlparse(base_url)
    base = f"{parsed.scheme}://{parsed.netloc}"

    found_links = []

    for page_path in WEBSITE_PAGES_TO_CHECK:
        url = base + page_path
        resp = _fetch_with_retry(url, timeout=6)
        if not resp or resp.status_code != 200:
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        links = _extract_youtube_channel_links(soup)
        found_links.extend(links)
        if found_links:
            break

    if not found_links:
        return None

    # Try to validate each link against person/company name
    for link in found_links:
        channel_id = _resolve_youtube_url_to_channel_id(link)
        if not channel_id:
            continue
        try:
            resp = youtube.channels().list(
                part="snippet", id=channel_id
            ).execute()
            items = resp.get("items", [])
            if not items:
                continue
            channel_title = items[0]["snippet"].get("title", "")
            channel_desc  = items[0]["snippet"].get("description", "")
            if _name_match(channel_title + " " + channel_desc, person_name, company_name):
                return channel_id
        except Exception:
            continue

    logger.info(
        f"  Found {len(found_links)} YouTube link(s) on {base_url} "
        f"but none passed name validation. Skipping."
    )
    return None


def _search_and_validate(
    query: str,
    person_name: str,
    company_name: str,
    company_website: str,
    require_cross_validation: bool = True,
    max_results: int = 5,
) -> dict | None:
    """
    Run a YouTube channel search and validate the top results.

    If require_cross_validation=True: candidate must pass name match AND
    (if channel has a listed website) website must match company_website.

    Returns {"channel_id": ..., "channel_url": ...} or None.
    Costs 100 quota units per call.
    """
    try:
        results = search_youtube_channels(query, max_results=max_results)
    except HttpError as e:
        if e.resp.status == 403:
            raise
        return None

    for item in results:
        snippet    = item["snippet"]
        channel_id = snippet["channelId"]
        title      = snippet.get("title", "")
        desc       = snippet.get("description", "")

        if not _name_match(title + " " + desc, person_name, company_name):
            continue

        if require_cross_validation:
            # Reject channels with >200K subscribers — established creators
            # that crowd out the real target in search results
            sub_count = _get_channel_subscriber_count(channel_id)
            if sub_count is not None and sub_count > 500_000:
                logger.info(
                    f"  Skipping channel {channel_id}: "
                    f"{sub_count:,} subs (>500K threshold)"
                )
                continue

            # Secondary check: if channel lists a website and it doesn't
            # match, it's a definitive mismatch — skip it.
            if company_website:
                channel_website = _get_channel_website(channel_id)
                if channel_website and not _websites_match(
                    channel_website, company_website
                ):
                    logger.info(
                        f"  Skipping channel {channel_id}: "
                        f"website mismatch ({channel_website} vs {company_website})"
                    )
                    continue

        channel_url = f"https://www.youtube.com/channel/{channel_id}"
        return {"channel_id": channel_id, "channel_url": channel_url}

    return None


def discover_channel_for_company(
    company_dict: dict,
    person_name: str,
    person_name_search: bool = True,
) -> dict | None:
    """
    Attempt to find a YouTube channel for one specific company.

    Returns a dict with:
        channel_id, channel_url, source, confidence
    Or None if no channel found.

    Discovery order:
        Stage 1 — Scrape company website (free)
        Stage 2 — YouTube search by company name + cross-validate
        Stage 3 — YouTube search by person name + cross-validate (skipped if person_name_search=False)
        Stage 4 — YouTube search combined (person + company) — no cross-validation (skipped if person_name_search=False)
    """
    company_name    = company_dict.get("company", "")
    company_website = company_dict.get("company_website") or ""

    # Stage 1 — Scrape company website for YouTube links (free)
    if company_website:
        channel_id = _scrape_website_for_channel(company_website, person_name, company_name)
        if channel_id:
            return {
                "channel_id":  channel_id,
                "channel_url": f"https://www.youtube.com/channel/{channel_id}",
                "source":      "website",
                "confidence":  "high",
            }

    time.sleep(0.3)

    # Stage 1.5 — DDG web search (free, costs at most 1 quota unit for validation)
    # Finds the channel URL via Google index before spending 100 units on search.list
    channel_id = _find_channel_via_ddg(person_name, company_name, company_website)
    if channel_id:
        return {
            "channel_id":  channel_id,
            "channel_url": f"https://www.youtube.com/channel/{channel_id}",
            "source":      "ddg_search",
            "confidence":  "high",
        }

    time.sleep(0.3)

    if not person_name_search:
        # Skip person-based searches if caller opted out
        if company_name:
            core_name = _core_company_name(company_name)
            search_names = [company_name]
            if core_name != company_name:
                search_names.append(core_name)
            for qname in search_names:
                candidate = _search_and_validate(
                    query=qname,
                    person_name=person_name,
                    company_name=company_name,
                    company_website=company_website,
                    require_cross_validation=True,
                )
                if candidate:
                    return {**candidate, "source": "search_company", "confidence": "high"}
                time.sleep(0.3)
        return None

    # Stage 2 — YouTube search: person + company combined (most specific).
    # User insight: the right channel almost always appears in the top results
    # for this query. Trying it first — before any single-term fallback — minimises
    # false positives while maximising recall.
    combined = f"{person_name} {company_name}".strip()
    if combined:
        candidate = _search_and_validate(
            query=combined,
            person_name=person_name,
            company_name=company_name,
            company_website=company_website,
            require_cross_validation=False,  # Relax validation — trust search results
            max_results=20,  # Search deeper
        )
        if candidate:
            return {**candidate, "source": "search_combined", "confidence": "high"}
        time.sleep(0.3)

    # Stage 3 — YouTube search: company name alone + cross-validate.
    # For long/descriptive names also try the core brand name.
    if company_name:
        core_name = _core_company_name(company_name)
        search_names = [company_name]
        if core_name != company_name:
            search_names.append(core_name)

        for qname in search_names:
            candidate = _search_and_validate(
                query=qname,
                person_name=person_name,
                company_name=company_name,
                company_website=company_website,
                require_cross_validation=False,  # Relax validation
                max_results=15,  # Search deeper
            )
            if candidate:
                return {**candidate, "source": "search_company", "confidence": "high"}
            time.sleep(0.3)

    # Stage 4 — YouTube search: person name alone + cross-validate.
    # Last resort — search deeper to find founder channels.
    if person_name:
        candidate = _search_and_validate(
            query=person_name,
            person_name=person_name,
            company_name=company_name,
            company_website=company_website,
            require_cross_validation=False,  # Relax validation for last-resort search
            max_results=20,  # Search deeper
        )
        if candidate:
            return {**candidate, "source": "search_person", "confidence": "high"}

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

    # Condition C — Inconsistent Poster: any 60+ day gap among the most recent
    # uploads signals the channel doesn't maintain a reliable cadence.
    # Check all consecutive pairs across the full fetched window.
    for idx in range(len(videos) - 1):
        gap = (videos[idx]["published_at"] - videos[idx + 1]["published_at"]).days
        if gap > 60:
            return {
                "condition": "C",
                "reasoning": (
                    f"Gap of {gap} days between uploads "
                    f"{idx + 1} and {idx + 2}"
                ),
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
# Multi-company qualification
# ---------------------------------------------------------------------------

def qualify_all_companies(
    active_companies: list,
    person_name: str,
    no_claude: bool = False,
    person_name_search: bool = True,
) -> list:
    """
    Run YouTube discovery and qualification for each active company.
    Returns a list of result dicts, one per company, in input order.
    """
    results = []

    for rank, company in enumerate(active_companies):
        company_name = company.get("company", "")
        print(
            f"    Checking YouTube for company [{rank+1}/{len(active_companies)}]: "
            f"{company_name}",
            file=sys.stderr,
        )

        try:
            discovery = discover_channel_for_company(
                company_dict=company,
                person_name=person_name,
                person_name_search=person_name_search,
            )
        except HttpError as e:
            if e.resp.status == 403:
                raise
            discovery = None

        if not discovery:
            result = {
                "condition":        "A",
                "channel_url":      None,
                "channel_name":     None,
                "last_upload_date": None,
                "upload_count":     0,
                "reasoning":        f"No channel found for {company_name}",
                "stage":            1,
                "company_name":     company_name,
                "company_rank":     rank,
                "discovery_source": "none",
            }
        else:
            channel_id = discovery["channel_id"]
            try:
                videos, channel_info = get_channel_videos(channel_id, max_results=20)
            except HttpError as e:
                if e.resp.status == 403:
                    raise
                results.append({
                    "condition":        "ERROR",
                    "channel_url":      discovery["channel_url"],
                    "channel_name":     None,
                    "last_upload_date": None,
                    "upload_count":     0,
                    "reasoning":        f"API error fetching channel: {e}",
                    "stage":            1,
                    "company_name":     company_name,
                    "company_rank":     rank,
                    "discovery_source": discovery["source"],
                })
                continue

            if not videos:
                cond_result = {
                    "condition":        "B",
                    "reasoning":        "Channel found but has no videos",
                    "stage":            1,
                    **channel_info,
                    "last_upload_date": None,
                }
            else:
                stage1 = _run_stage_1(videos, channel_info)
                if stage1:
                    cond_result = stage1
                elif no_claude:
                    most_recent = videos[0]["published_at"]
                    cond_result = {
                        "condition":        "STAGE2_NEEDED",
                        "stage":            2,
                        "last_upload_date": most_recent.date().isoformat(),
                        "upload_count":     channel_info.get("upload_count", 0),
                        "videos": [
                            {**v, "published_at": v["published_at"].isoformat()}
                            for v in videos[:5]
                        ],
                        **channel_info,
                    }
                else:
                    cond_result = _run_stage_2(
                        videos, channel_info, person_name, company_name
                    )

            result = {
                **cond_result,
                "company_name":     company_name,
                "company_rank":     rank,
                "discovery_source": discovery["source"],
            }

        results.append(result)
        print(
            f"      → {result['condition']} "
            f"(source: {result.get('discovery_source', 'none')})",
            file=sys.stderr,
        )

    return results


def resolve_company_youtube_results(company_results: list) -> dict:
    """
    Apply the resolution rule across all per-company YouTube results
    to reach a single lead-level decision.

    Rules (in order):
      1. FAIL anywhere → discard (or REVIEW_FAIL if secondary)
      2. All pass (A/B/C/D/E/F) → use primary (rank 0)
      3. Primary is STAGE2_NEEDED → return as-is for in-session judgment
      4. Primary is ERROR → use best available secondary
    """
    if not company_results:
        return {
            "condition":           "A",
            "reasoning":           "No active companies to check",
            "resolution_rule":     "no_companies",
            "all_company_results": [],
        }

    fails  = [r for r in company_results if r["condition"] == "FAIL"]
    passes = [r for r in company_results if r["condition"] in ("A","B","C","D","E","F")]
    stage2 = [r for r in company_results if r["condition"] == "STAGE2_NEEDED"]
    errors = [r for r in company_results if r["condition"] == "ERROR"]

    # Rule 1: FAIL anywhere
    if fails:
        primary_fails = [f for f in fails if f.get("company_rank") == 0]
        secondary_website_fails = [
            f for f in fails
            if f.get("company_rank", 0) > 0
            and f.get("discovery_source") == "website"
        ]

        if primary_fails:
            best = primary_fails[0]
            return {
                **best,
                "condition":           "FAIL",
                "reasoning":           (
                    f"Primary company ({best['company_name']}) has active polished YouTube. "
                    f"{best.get('reasoning', '')}"
                ),
                "resolution_rule":     "fail_primary",
                "all_company_results": company_results,
            }
        elif secondary_website_fails:
            best = secondary_website_fails[0]
            return {
                **best,
                "condition":           "REVIEW_FAIL",
                "reasoning":           (
                    f"Secondary company ({best['company_name']}) has active YouTube "
                    f"(found via website). Manual review recommended — may be abandoned."
                ),
                "resolution_rule":     "fail_secondary_website",
                "all_company_results": company_results,
            }
        else:
            best = fails[0]
            return {
                **best,
                "condition":           "REVIEW_FAIL",
                "reasoning":           (
                    f"Secondary company ({best['company_name']}) may have active YouTube "
                    f"(found via search, lower confidence). Manual review recommended."
                ),
                "resolution_rule":     "fail_secondary_search",
                "all_company_results": company_results,
            }

    # Rule 2: All pass — use primary (rank 0)
    primary_passes = [r for r in passes if r.get("company_rank") == 0]
    if primary_passes:
        primary = primary_passes[0]
        secondary_notes = "; ".join(
            f"{r['company_name']}={r['condition']}"
            for r in passes
            if r.get("company_rank", 0) > 0
        )
        return {
            **primary,
            "resolution_rule":     "all_pass_use_primary",
            "secondary_channels":  secondary_notes,
            "all_company_results": company_results,
        }

    # Rule 3: Primary needs Stage 2 judgment
    primary_stage2 = [r for r in stage2 if r.get("company_rank") == 0]
    if primary_stage2:
        return {
            **primary_stage2[0],
            "resolution_rule":     "stage2_needed",
            "all_company_results": company_results,
        }

    # Rule 4: Primary errored — use best available secondary
    if errors and passes:
        return {
            **passes[0],
            "resolution_rule":     "primary_error_use_secondary",
            "all_company_results": company_results,
        }

    # Fallback: all errored or empty
    return {
        **(errors[0] if errors else {"condition": "A", "reasoning": "No results"}),
        "resolution_rule":     "all_errors",
        "all_company_results": company_results,
    }


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def qualify_youtube(
    person_name: str,
    company_name: str,
    website_url: str = None,
    no_claude: bool = False,
    active_companies: list = None,
    person_name_search: bool = True,
) -> dict:
    """
    Qualify YouTube presence for a lead.

    If active_companies is provided, qualifies each company independently
    and applies the resolution rule. Otherwise falls back to single-company
    behaviour using person_name, company_name, and website_url.
    """
    companies = active_companies if active_companies else [{
        "company":              company_name,
        "company_website":      website_url,
        "job_title":            "",
        "company_description":  "",
        "company_specialities": "",
        "company_industry":     "",
    }]

    try:
        company_results = qualify_all_companies(
            active_companies=companies,
            person_name=person_name,
            no_claude=no_claude,
            person_name_search=person_name_search,
        )
    except HttpError as e:
        if e.resp.status == 403:
            return {
                "condition": "ERROR", "channel_url": None, "channel_name": None,
                "last_upload_date": None, "upload_count": 0,
                "reasoning": "YouTube API quota exceeded", "stage": 1,
            }
        return {
            "condition": "ERROR", "channel_url": None, "channel_name": None,
            "last_upload_date": None, "upload_count": 0,
            "reasoning": f"YouTube API error: {e}", "stage": 1,
        }

    return resolve_company_youtube_results(company_results)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--test-name-match" in sys.argv:
        # Positive: person name match
        assert _name_match("Mike McCalley Revenue Strategy", "Mike McCalley", "The Vertical Solution")
        # Positive: company token match
        assert _name_match("StraDGy 360 Business Channel", "Zoe Fairfax", "StraDGy 360")
        # Negative: generic channel, no name overlap
        assert not _name_match("The Best Marketing Tips Channel", "John Smith", "Solutions Inc")
        # Negative: short names with no extractable tokens → reject (not wildcard accept)
        assert not _name_match("Anything at all", "Bo Li", "IQ Co")
        # Positive: middle name optional — first + last sufficient
        assert _name_match("Michael Smith Branding", "Michael John Smith", "Acme Consulting")
        # Negative: "jus" must not match "just" (the original false-positive bug)
        assert not _name_match("Just Sport Event", "Jessica Wagner", "Jus B Media")
        # Negative: "iq" must not match "unique" via substring
        assert not _name_match("unique techniques channel", "Jane Doe", "IQ")
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
