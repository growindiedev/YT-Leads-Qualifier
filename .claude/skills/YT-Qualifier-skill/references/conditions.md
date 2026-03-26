# Condition Definitions — YouTube Qualifier

Full definitions for each qualification condition used in the ContentScale lead generation pipeline.

---

## PASS Conditions (lead qualifies)

### Condition A — No Channel
**Definition:** No YouTube channel can be found for the founder or the company after exhausting all discovery methods.

**Detection:**
- YouTube Search API returns no matching channel for founder name
- YouTube Search API returns no matching channel for company name
- Website HTML contains no `youtube.com/` link
- All three checks return nothing

**Record as:**
```
condition: "A"
channel_url: None
reasoning: "No YouTube channel found for [name] or [company] after searching YouTube and checking website."
```

---

### Condition B — Dead Channel
**Definition:** A channel exists but has been abandoned. The most recent uploaded video is more than 60 days old from today's date.

**Detection:**
- Fetch uploads playlist → get `publishedAt` of most recent item
- `(today - publishedAt).days > 60` → Condition B

**Edge case:** Channel has 1–2 polished videos but nothing in 6+ months → still Condition B, not FAIL. Recency matters more than quality.

**Record as:**
```
condition: "B"
channel_url: [url]
last_upload_date: [ISO date]
reasoning: "Channel exists but last video was uploaded [X] days ago on [date]. Abandoned."
```

---

### Condition C — Inconsistent Poster
**Definition:** A recent video exists, proving they're not fully dead, but there is a gap of 2+ months between the most recent video and the one before it. This proves they cannot maintain consistency.

**Detection:**
- Fetch last 3 uploads → extract dates as `[v1, v2, v3]` newest to oldest
- `(v1 - v2).days > 60` → Condition C
- If v1 - v2 is fine but v2 - v3 > 60 → still Condition C (gap exists in recent history)

**Why this matters:** Inconsistent posters have already tried YouTube and failed at consistency. They are warm to the idea but need operational support — exactly what ContentScale sells.

**Record as:**
```
condition: "C"
channel_url: [url]
last_upload_date: [ISO date of most recent]
reasoning: "Posted [X] days ago but previous video was [Y] days before that — [Z] day gap proves inconsistency."
```

---

### Condition D — Podcast/Webinar Only
**Definition:** The channel posts regularly, but all content is raw, unedited podcast clips, guest interviews, or Zoom/webinar recordings. No scripted direct-to-camera authority content. No editing, graphics, or production value.

**Detection (Stage 2 only — Claude call):**
Signals Claude looks for:
- Titles containing: "podcast", "ep.", "episode", "#[number]", "with [guest]", "ft.", "interview", "webinar", "recap", "highlights", "conversation"
- Long durations (45–120 min) consistent with unedited recordings
- No custom thumbnails (auto-generated YouTube thumbnails)
- No evidence of scripted or edited content from titles/descriptions

**Important:** If a channel mixes podcast content WITH some direct-to-camera scripted videos, it may still be FAIL. D requires that ALL content is podcast/webinar style.

**Record as:**
```
condition: "D"
channel_url: [url]
reasoning: "Posts regularly but exclusively podcast/interview content — no scripted direct-to-camera authority videos."
```

---

### Condition E — Shorts Only
**Definition:** The channel exists and may be active, but every video is a YouTube Short (under 60 seconds). No long-form content.

**Detection:**
- Fetch last 10 uploads → check `duration` field for each
- Parse ISO 8601 duration: `PT45S` = 45 seconds, `PT1M30S` = 90 seconds
- If ALL videos are under 60 seconds → Condition E
- If even one video is 60 seconds or longer → do not assign E, send to Stage 2

**Why this matters:** Shorts-only creators have tried video but haven't committed to long-form. They understand content but need help with the format ContentScale specialises in.

**Record as:**
```
condition: "E"
channel_url: [url]
reasoning: "Active channel but exclusively posts YouTube Shorts. No long-form content present."
```

---

### Condition F — Off-Topic Content
**Definition:** The channel posts regularly, but none of the content is related to their business, offer, or industry. They have an active channel that does nothing to support or sell their services.

**Detection (Stage 2 only — Claude call):**
Signals Claude looks for:
- Personal vlogs, hobby content, or lifestyle videos with no connection to their professional offer
- Motivational or generic content not tied to their industry or service
- No videos that would attract their target B2B audience
- Content clearly disconnected from what they sell (e.g. a B2B consultant posting cooking videos)

**Why this matters:** These founders have proven they can show up on camera and post consistently — they just aren't using it for their business. ContentScale can redirect that energy into business-relevant content.

**Record as:**
```
condition: "F"
channel_url: [url]
reasoning: "Active channel but content is entirely unrelated to their business/offer."
```

---

## FAIL Condition (lead discarded)

### FAIL — Active and Polished
**Definition:** The channel consistently posts well-edited, direct-to-camera, scripted, SEO-optimized long-form YouTube videos. This prospect already has what ContentScale sells.

**Detection (Stage 2 — Claude call):**
Signals Claude looks for:
- Regular upload schedule (average gap between last 5 videos under 30 days)
- Custom thumbnails (not auto-generated)
- Titles appear SEO-optimized (keyword-rich, formatted for search)
- Mix of video lengths — some 8–20 min (authority/tutorial format)
- No long gaps in upload history

**Action:** Discard the entire company. Do not add any contacts from this company to the output.

**Record as:**
```
condition: "FAIL"
channel_url: [url]
reasoning: "Active, polished YouTube presence — consistently posting scripted long-form content. ContentScale cannot add value here."
```

---

## Decision Tree Summary

```
Channel found?
├── NO → A
└── YES →
    Last video > 60 days old?
    ├── YES → B
    └── NO →
        Gap between last 2 videos > 60 days?
        ├── YES → C
        └── NO →
            All videos < 60 seconds?
            ├── YES → E
            └── NO → [Stage 2: Claude]
                      ├── Podcast/webinar signals → D
                      ├── Off-topic, unrelated to business → F
                      └── Polished/scripted business content → FAIL
```

---

## Handling Ambiguous Cases

**Between C and FAIL:**
Check the gap between the last 3 videos. If ANY two consecutive videos have a 60+ day gap in recent history → Condition C. Recency of inconsistency matters.

**Between D and FAIL:**
If the channel has even 2–3 scripted, edited direct-to-camera videos mixed in with podcasts → lean toward FAIL. D requires the content to be exclusively unedited.

**Channel found but can't fetch videos:**
Private channel, terminated account, or API error → treat as Condition A. Log the error in reasoning.

**Multiple channels for same person:**
Pick the channel with the highest subscriber count. Note the other in reasoning. Apply conditions to the primary channel only.