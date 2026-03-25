Qualify a YouTube channel for a lead. Usage: /youtube-qualify "Person Name" "Company Name" ["https://website.com"]

Parse the arguments from $ARGUMENTS: first is person name, second is company name, third (optional) is website URL.

Run the following steps:

## Step 1 — Run the script

Execute the script with `--no-claude` flag:
```
cd youtube_qualifier && python youtube_qualifier.py "{person}" "{company}" ["{website}"] --no-claude
```

Parse the JSON output.

## Step 2 — Check the result

If `condition` is NOT `STAGE2_NEEDED`, print the final result and stop:
```
RESULT:
Condition:    {condition}
Channel:      {channel_name}
Channel URL:  {channel_url}
Last Upload:  {last_upload_date}
Upload Count: {upload_count}
Stage:        {stage}
Reasoning:    {reasoning}
```

## Step 3 — Stage 2 judgment (only if STAGE2_NEEDED)

You have the video data. Evaluate the channel yourself using the criteria below.

CONDITION D (good lead — weak content):
- Videos are exclusively raw podcast recordings, webinar recordings, or interview clips
- No visible editing or production value
- Titles suggest episode format (Ep., #123, "with [guest]", "interview", "podcast", "webinar")
- No direct-to-camera scripted content from the founder

FAIL (bad lead — strong content):
- Direct-to-camera scripted content from the founder
- Titles/thumbnails suggest produced, edited content
- Educational or authority-building content (not just podcast clips)
- Short punchy titles, not episode-style

If genuinely unclear, default to Condition D.

Print the final result in the same format as Step 2, with `Stage: 2`.
