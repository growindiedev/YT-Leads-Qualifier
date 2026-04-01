---
name: check-quota
description: Check YouTube Data API v3 quota status. Runs a 1-unit test call and reports whether quota is available or exhausted. Also shows quota cost reference for common operations.
---

# Check YouTube Quota Skill

Run this script and report the result:

```
.venv/bin/python3 - << 'EOF'
import os, sys, json
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath("batch_qualify.py")), ".env"))

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

key = os.getenv("YOUTUBE_API_KEY")
if not key:
    print("ERROR: YOUTUBE_API_KEY not set in .env")
    sys.exit(1)

youtube = build("youtube", "v3", developerKey=key)

try:
    # channels.list costs 1 quota unit — cheapest real API call
    youtube.channels().list(part="id", id="UCxxxxxx").execute()
    print("STATUS: OK")
    print("Quota is available. API key is valid.")
except HttpError as e:
    if e.resp.status == 403:
        print("STATUS: QUOTA EXCEEDED")
        print("Daily limit of 10,000 units reached. Resets at midnight Pacific time.")
        print("Re-run /qualify-leads tomorrow — dedup will skip already-processed leads.")
    elif e.resp.status == 400:
        # 400 on a dummy channel ID = API key works, quota available
        print("STATUS: OK")
        print("Quota is available. API key is valid.")
    else:
        print(f"STATUS: ERROR ({e.resp.status})")
        print(str(e))
except Exception as e:
    print(f"STATUS: ERROR")
    print(str(e))
EOF
```

Then print this reference table:

```
YOUTUBE API QUOTA REFERENCE  (daily limit: 10,000 units)
──────────────────────────────────────────────────────────
search.list (channel search)      100 units  ← old discovery method
channels.list (by handle/ID)        1 unit   ← DDG → validate (new)
playlistItems.list (recent videos)  3 units
videos.list (durations)             3 units
──────────────────────────────────────────────────────────
OLD cost per lead:  ~300–400 units (3–4 search.list calls)
NEW cost per lead:  ~7 units       (DDG finds channel, API just validates + fetches videos)
Leads per day OLD:  ~25–33
Leads per day NEW:  ~1,400
```
