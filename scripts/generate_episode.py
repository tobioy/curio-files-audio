#!/usr/bin/env python3
"""
Generates one episode of Curio Files.

Runs on a schedule via GitHub Actions (Monday, Wednesday, Saturday). Each run:
  1. Reads docs/episodes.json to see which facts have already been used.
  2. Asks Claude to research and write BOTH a 4-question quiz and a connected
     narrative script covering the same facts, told differently, matching the
     schema the Curio Files app (a Claude Artifact) already uses.
  3. Sends the narration script to ElevenLabs for real audio.
  4. Saves the mp3 under docs/audio/, updates docs/episodes.json, and rebuilds
     docs/feed.xml (a standard podcast RSS feed) and docs/index.html.

This repo is the single source of truth for a day's content. A separate
Claude scheduled task reads docs/episodes.json from here and mirrors each
new episode (quiz, script, and a link to this repo's hosted mp3) into the
Curio Files app, so the quiz and the podcast live in one place for you,
even though the audio itself is generated here.
"""

import json
import os
import re
import sys
import datetime
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")
AUDIO_DIR = os.path.join(DOCS, "audio")
EPISODES_JSON = os.path.join(DOCS, "episodes.json")
FEED_XML = os.path.join(DOCS, "feed.xml")
INDEX_HTML = os.path.join(DOCS, "index.html")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")  # "Rachel", a default ElevenLabs voice
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")
SHOW_TITLE = os.environ.get("SHOW_TITLE", "Curio Files")
SHOW_AUTHOR = os.environ.get("SHOW_AUTHOR", "Curio Files")
ONESIGNAL_APP_ID = os.environ.get("ONESIGNAL_APP_ID", "")
ONESIGNAL_REST_API_KEY = os.environ.get("ONESIGNAL_REST_API_KEY", "")

GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY", "")  # "owner/repo", set automatically by GitHub Actions


def base_url():
    if "/" in GITHUB_REPOSITORY:
        owner, repo = GITHUB_REPOSITORY.split("/", 1)
        return f"https://{owner}.github.io/{repo}"
    return os.environ.get("PAGES_BASE_URL", "").rstrip("/")


def load_episodes():
    if os.path.exists(EPISODES_JSON):
        with open(EPISODES_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_episodes(episodes):
    with open(EPISODES_JSON, "w", encoding="utf-8") as f:
        json.dump(episodes, f, indent=2, ensure_ascii=False)


def call_claude(prior_facts):
    if not ANTHROPIC_API_KEY:
        print("ANTHROPIC_API_KEY is not set, cannot write an episode.", file=sys.stderr)
        sys.exit(1)

    today = datetime.date.today()
    prior_list = "\n".join(f"- {f}" for f in prior_facts[-40:]) or "(none yet, this is the first episode)"

    system_prompt = (
        "You write episodes of Curio Files, a recurring history, science and art trivia and mini podcast. "
        "The tone is fast, funny, a little gross, always true, in the spirit of the Horrible Histories book "
        "series. Every fact must be real and checkable, with a real, nameable source. "
        "Each episode covers 4 facts spanning history, science and art, and produces TWO things covering "
        "those SAME 4 facts, told two different ways. "
        "First, a quiz, 4 multiple choice questions, one per fact, each with 4 options, one correct answer, "
        "and a short terse case notes reveal paragraph, 2 to 3 sentences, fast recall style. "
        "Second, a narration script, 500 to 700 words of flowing connected narrative prose that tells the "
        "story behind all 4 facts together under one loose theme or throughline, with scene setting and "
        "texture and a closing thought that ties them together, the way a good longform podcast segment "
        "reads, not a list of trivia read aloud, and not structured as separate segments. "
        "The script should never just restate the quiz's reveal text in longer sentences, it should feel "
        "like a genuinely different, deeper piece of writing about the same material. "
        "End the script with the sentence New file Monday, Wednesday, Saturday. "
        "Do not reuse any fact already covered before, listed below. "
        "Strict style rule, never use an em dash or a colon anywhere in the output, use periods, commas or "
        "hyphens instead. "
        "Respond with ONLY a JSON object, no other text, no markdown fences, shaped exactly like "
        '{"label": "Weekday, Month D, e.g. Thursday, September 10", '
        '"facts_used": ["one short slug per fact, for future de-duplication"], '
        '"script": "the full 500 to 700 word narration script", '
        '"cases": [{"no": "CF-001", "tag": "History or Science or Art or a combination", '
        '"q": "question text", "options": ["4 options"], "correct": 0, '
        '"revealTitle": "Case notes", "revealText": "2 to 3 sentence reveal", '
        '"source": "Publication, Title"}, ... exactly 4 of these, no in the quiz should be numbered CF-001 through CF-004]}'
    )

    user_prompt = (
        f"Today's date is {today.isoformat()}. Write today's episode. "
        f"Facts already covered in earlier episodes, do not repeat these or close variants of them.\n{prior_list}"
    )

    body = json.dumps({
        "model": CLAUDE_MODEL,
        "max_tokens": 8000,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "content-type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print("Claude API call failed:", e.read().decode("utf-8"), file=sys.stderr)
        sys.exit(1)

    text = None
    for block in payload.get("content", []):
        if block.get("type") == "text" and block.get("text"):
            text = block["text"].strip()
            break
    if text is None:
        print("Claude's response did not contain a text block, full reply below.", file=sys.stderr)
        print(json.dumps(payload, indent=2), file=sys.stderr)
        sys.exit(1)
    text = re.sub(r"^```(json)?", "", text.strip())
    text = re.sub(r"```$", "", text.strip())
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        stop_reason = payload.get("stop_reason", "unknown")
        if stop_reason == "max_tokens":
            print(
                "Claude's response got cut off before finishing, it hit the max_tokens limit. "
                "Raise max_tokens in call_claude and try again.",
                file=sys.stderr,
            )
        print("Could not parse Claude's response as JSON:\n", text, file=sys.stderr)
        sys.exit(1)

    for key in ("label", "facts_used", "script", "cases"):
        if key not in data:
            print(f"Claude's response is missing '{key}':\n{text}", file=sys.stderr)
            sys.exit(1)
    if len(data["cases"]) != 4:
        print(f"Expected 4 cases, got {len(data['cases'])}:\n{text}", file=sys.stderr)
        sys.exit(1)
    return data


def call_elevenlabs(text, out_path):
    if not ELEVENLABS_API_KEY:
        print("ELEVENLABS_API_KEY is not set, cannot generate audio.", file=sys.stderr)
        sys.exit(1)

    body = json.dumps({
        "text": text,
        "model_id": "eleven_multilingual_v2",
        "voice_settings": {"stability": 0.45, "similarity_boost": 0.75},
    }).encode("utf-8")

    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
        data=body,
        headers={
            "xi-api-key": ELEVENLABS_API_KEY,
            "content-type": "application/json",
            "accept": "audio/mpeg",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            audio_bytes = resp.read()
    except urllib.error.HTTPError as e:
        print("ElevenLabs API call failed:", e.read().decode("utf-8"), file=sys.stderr)
        sys.exit(1)

    with open(out_path, "wb") as f:
        f.write(audio_bytes)
    return len(audio_bytes)


def send_push(title, message):
    if not ONESIGNAL_APP_ID or not ONESIGNAL_REST_API_KEY:
        print("OneSignal is not configured, skipping the push notification.")
        return
    body = json.dumps({
        "app_id": ONESIGNAL_APP_ID,
        "target_channel": "push",
        "headings": {"en": title},
        "contents": {"en": message},
        "included_segments": ["All"],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.onesignal.com/notifications",
        data=body,
        headers={
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Key {ONESIGNAL_REST_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        print("Push notification sent.")
    except urllib.error.HTTPError as e:
        # A push failure should never fail the whole run, the episode itself already saved fine.
        print("Push notification failed (non-fatal):", e.read().decode("utf-8"), file=sys.stderr)


def rfc2822(dt):
    return dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


def escape_xml(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&apos;"))


def rebuild_feed(episodes, url_base):
    items = []
    for ep in sorted(episodes, key=lambda e: e["date"], reverse=True):
        pub = datetime.datetime.strptime(ep["date"], "%Y-%m-%d")
        mp3_url = f"{url_base}/audio/{ep['date']}.mp3"
        items.append(f"""    <item>
      <title>{escape_xml(ep['label'])}</title>
      <description>{escape_xml(ep['script'][:400])}...</description>
      <pubDate>{rfc2822(pub)}</pubDate>
      <enclosure url="{mp3_url}" length="{ep.get('bytes', 0)}" type="audio/mpeg" />
      <guid isPermaLink="false">{ep['date']}</guid>
    </item>""")

    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{escape_xml(SHOW_TITLE)}</title>
    <link>{url_base}/</link>
    <language>en-us</language>
    <description>True, faintly disgusting things from history, science and art. New file Monday, Wednesday, Saturday.</description>
    <itunes:author>{escape_xml(SHOW_AUTHOR)}</itunes:author>
    <itunes:explicit>false</itunes:explicit>
{chr(10).join(items)}
  </channel>
</rss>
"""
    with open(FEED_XML, "w", encoding="utf-8") as f:
        f.write(feed)


def rebuild_index(episodes, url_base):
    rows = []
    for ep in sorted(episodes, key=lambda e: e["date"], reverse=True):
        rows.append(f"""<div class="ep">
  <h2>{escape_xml(ep['label'])}</h2>
  <audio controls preload="none" src="audio/{ep['date']}.mp3"></audio>
  <p>{escape_xml(ep['script']).replace(chr(10)+chr(10), '</p><p>')}</p>
</div>""")

    onesignal_snippet = ""
    if ONESIGNAL_APP_ID:
        onesignal_snippet = f"""
<script src="https://cdn.onesignal.com/sdks/web/v16/OneSignalSDK.page.js" defer></script>
<script>
  window.OneSignalDeferred = window.OneSignalDeferred || [];
  OneSignalDeferred.push(async function(OneSignal) {{
    await OneSignal.init({{ appId: "{ONESIGNAL_APP_ID}", notifyButton: {{ enable: true }} }});
    // On iPhone, the permission prompt only works once this page is opened from the
    // home screen icon rather than a normal Safari tab, so only ask automatically then.
    var isStandalone = window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone === true;
    if (isStandalone && OneSignal.Notifications.permission !== true) {{
      OneSignal.Notifications.requestPermission();
    }}
  }});
</script>"""

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{escape_xml(SHOW_TITLE)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="manifest" href="manifest.json">
<link rel="apple-touch-icon" href="icons/apple-touch-icon.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#101a1d">{onesignal_snippet}
<style>
body {{ font-family: -apple-system, sans-serif; max-width: 640px; margin: 40px auto; padding: 0 20px; background: #101a1d; color: #ece3d2; }}
h1 {{ font-size: 24px; }}
a {{ color: #d9a441; }}
.note {{ font-size: 13px; color: #a7b6b3; }}
.addhome {{ font-size: 13px; color: #cfc8b8; background: #17262b; border: 1px solid #2c4046; border-radius: 10px; padding: 14px 16px; margin: 16px 0; }}
.ep {{ border-top: 1px solid #2c4046; padding: 24px 0; }}
.ep h2 {{ margin-bottom: 12px; }}
.ep audio {{ width: 100%; margin-bottom: 12px; }}
.ep p {{ line-height: 1.6; color: #cfc8b8; }}
</style></head>
<body>
<h1>{escape_xml(SHOW_TITLE)}, raw feed</h1>
<p class="note">This page is the audio backend. The quiz plus this same audio, in one place, lives in the Curio Files app. Subscribe here in any podcast app with <a href="feed.xml">{url_base}/feed.xml</a></p>
<p class="addhome">On iPhone, notifications for new episodes only work once this page is added to your home screen. Safari share icon, then Add to Home Screen, then open it from there once and allow notifications when asked.</p>
{"".join(rows)}
</body></html>
"""
    with open(INDEX_HTML, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    os.makedirs(AUDIO_DIR, exist_ok=True)
    episodes = load_episodes()
    today = datetime.date.today().isoformat()
    url_base = base_url()

    if any(e["date"] == today for e in episodes):
        print(f"Episode for {today} already exists, refreshing the page and feed only, not generating new content.")
        rebuild_feed(episodes, url_base)
        rebuild_index(episodes, url_base)
        return

    prior_facts = []
    for e in episodes:
        prior_facts.extend(e.get("facts_used", []))

    print("Asking Claude to write today's quiz and script...")
    written = call_claude(prior_facts)

    mp3_path = os.path.join(AUDIO_DIR, f"{today}.mp3")
    print("Generating narration with ElevenLabs...")
    nbytes = call_elevenlabs(written["script"], mp3_path)
    print(f"Wrote {nbytes} bytes of audio to {mp3_path}")

    episodes.append({
        "date": today,
        "label": written["label"],
        "facts_used": written["facts_used"],
        "script": written["script"],
        "cases": written["cases"],
        "bytes": nbytes,
    })
    save_episodes(episodes)

    rebuild_feed(episodes, url_base)
    rebuild_index(episodes, url_base)
    send_push("New Curio Files episode", written["label"] + ", quiz and audio are up now.")
    print(f"Done. episodes.json, feed and audio are ready to push. Base URL is {url_base}")


if __name__ == "__main__":
    main()
