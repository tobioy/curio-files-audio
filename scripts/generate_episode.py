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

This repo is the single source of truth for a day's content. docs/index.html
is the whole app, quiz, narrated audio and archive together, self hosted, so
push notifications can run on it directly with no outside restrictions.
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
ONESIGNAL_SAFARI_WEB_ID = os.environ.get("ONESIGNAL_SAFARI_WEB_ID", "")

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
    sorted_eps = sorted(episodes, key=lambda e: e["date"], reverse=True)
    episodes_for_js = []
    for ep in sorted_eps:
        episodes_for_js.append({
            "date": ep["date"],
            "label": ep["label"],
            "script": ep["script"],
            "cases": ep["cases"],
            "audioUrl": f"audio/{ep['date']}.mp3",
        })
    episodes_json = json.dumps(episodes_for_js, ensure_ascii=False).replace("</script", "<\\/script")

    onesignal_snippet = ""
    if ONESIGNAL_APP_ID:
        # safari_web_id is what OneSignal's own dashboard-generated snippet includes for Safari
        # support specifically, without it Safari (including iOS home screen apps) may never
        # actually register a working push subscription even though everything else runs fine.
        safari_web_id_js = f', safari_web_id: "{ONESIGNAL_SAFARI_WEB_ID}"' if ONESIGNAL_SAFARI_WEB_ID else ""
        onesignal_snippet = f"""
<script src="https://cdn.onesignal.com/sdks/web/v16/OneSignalSDK.page.js" defer></script>
<script>
  // The button's visibility is handled purely by CSS (see .enablePush below), so it shows
  // up even if this script is blocked or OneSignal fails to load, only the click behavior
  // depends on OneSignal. That makes a silent loading failure visible instead of invisible.
  window.OneSignalDeferred = window.OneSignalDeferred || [];
  OneSignalDeferred.push(async function(OneSignal) {{
    var btn = document.getElementById("enablePushBtn");
    try {{
      await OneSignal.init({{ appId: "{ONESIGNAL_APP_ID}"{safari_web_id_js}, notifyButton: {{ enable: false }} }});
    }} catch (e) {{
      if (btn) {{
        btn.textContent = "Notifications unavailable right now";
        btn.disabled = true;
      }}
      console.error("OneSignal failed to initialize", e);
      return;
    }}
    if (!btn) return;
    if (OneSignal.Notifications.permission === true) {{
      btn.style.display = "none";
      return;
    }}
    btn.addEventListener("click", async function() {{
      btn.disabled = true;
      btn.textContent = "Requesting...";
      try {{
        await OneSignal.Notifications.requestPermission();
      }} catch (e) {{
        console.error("requestPermission failed", e);
      }}
      if (OneSignal.Notifications.permission === true) {{
        btn.textContent = "Notifications on";
      }} else {{
        btn.disabled = false;
        btn.textContent = "Turn on notifications for new files";
      }}
    }});
  }});
</script>"""

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{escape_xml(SHOW_TITLE)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="manifest" href="manifest.json">
<link rel="apple-touch-icon" href="icons/apple-touch-icon.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#101a1d">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600;9..144,700&family=Public+Sans:wght@400;500;600;700&family=IBM+Plex+Mono:wght@500;600&display=swap" rel="stylesheet">{onesignal_snippet}
<style>
  :root {{
    --bg: #101a1d;
    --bg-vignette: radial-gradient(ellipse 900px 600px at 50% -10%, #1c2b30 0%, #101a1d 55%);
    --surface: #17262b;
    --surface-2: #1e2f35;
    --surface-3: #24373d;
    --border: #2c4046;
    --border-soft: #21343a;
    --text: #ece3d2;
    --text-dim: #a7b6b3;
    --text-faint: #6f8481;
    --accent-rust: #c85a35;
    --accent-rust-soft: #c85a3522;
    --accent-gold: #d9a441;
    --accent-gold-soft: #d9a44120;
    --accent-moss: #7ba17e;
    --accent-moss-soft: #7ba17e22;
    --shadow: 0 20px 50px -20px rgba(0,0,0,0.6);
    --font-display: 'Fraunces', Georgia, 'Times New Roman', serif;
    --font-body: 'Public Sans', -apple-system, BlinkMacSystemFont, sans-serif;
    --font-mono: 'IBM Plex Mono', 'SF Mono', Consolas, monospace;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    background: var(--bg-vignette), var(--bg);
    color: var(--text);
    font-family: var(--font-body);
    margin: 0;
    min-height: 100vh;
    padding: 40px 20px 56px;
    display: flex;
    justify-content: center;
  }}
  .stage {{ width: 100%; max-width: 560px; }}
  .masthead {{ text-align: center; margin-bottom: 18px; }}
  .masthead .kicker {{
    font-family: var(--font-mono);
    font-size: 11px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--accent-gold);
  }}
  .masthead h1 {{
    font-family: var(--font-display);
    font-weight: 700;
    font-size: clamp(28px, 6vw, 38px);
    margin: 8px 0 6px;
  }}
  .masthead p {{ color: var(--text-dim); font-size: 14px; margin: 0; line-height: 1.5; }}
  .addhome {{
    font-size: 12.5px; color: #cfc8b8; background: var(--surface); border: 1px solid var(--border);
    border-radius: 10px; padding: 12px 14px; margin: 16px 0 0;
  }}
  .enablePush {{
    display: none; width: 100%; margin: 12px 0 0; padding: 13px 16px; font-size: 13.5px; font-weight: 600;
    font-family: inherit; color: var(--bg); background: var(--accent-gold); border: none; border-radius: 10px; cursor: pointer;
  }}
  @media (display-mode: standalone) {{ .enablePush {{ display: block; }} }}
  .nav {{
    display: flex; gap: 6px; background: var(--surface-2); border: 1px solid var(--border-soft);
    border-radius: 10px; padding: 4px; margin: 20px 0 18px;
  }}
  .nav button {{
    flex: 1; font-family: var(--font-mono); font-size: 12px; letter-spacing: 0.06em; text-transform: uppercase;
    padding: 9px 10px; border-radius: 7px; border: none; background: transparent; color: var(--text-faint); cursor: pointer;
  }}
  .nav button.active {{ background: var(--accent-gold); color: var(--bg); }}
  .file-meta {{
    text-align: center; font-family: var(--font-mono); font-size: 11.5px; letter-spacing: 0.06em;
    color: var(--text-faint); text-transform: uppercase; margin-bottom: 14px;
  }}
  .ticks {{ display: flex; gap: 8px; justify-content: center; margin: 0 0 20px; }}
  .tick {{ width: 34px; height: 6px; border-radius: 3px; background: var(--surface-2); border: 1px solid var(--border-soft); }}
  .tick.done {{ background: var(--accent-gold); border-color: var(--accent-gold); }}
  .tick.current {{ background: var(--accent-rust); border-color: var(--accent-rust); }}
  .card {{ background: var(--surface); border: 1px solid var(--border); border-radius: 14px; box-shadow: var(--shadow); overflow: hidden; }}
  .card-head {{
    display: flex; align-items: center; justify-content: space-between; padding: 16px 20px;
    border-bottom: 1px dashed var(--border); background: linear-gradient(180deg, var(--surface-3), var(--surface));
  }}
  .case-no {{ font-family: var(--font-mono); font-size: 12px; letter-spacing: 0.06em; color: var(--text-faint); }}
  .tag {{
    font-family: var(--font-mono); font-size: 10.5px; letter-spacing: 0.1em; text-transform: uppercase;
    padding: 5px 10px; border-radius: 999px; border: 1px solid var(--accent-gold-soft); color: var(--accent-gold); background: var(--accent-gold-soft);
  }}
  .card-body {{ padding: 24px 22px 22px; }}
  .question {{ font-family: var(--font-display); font-weight: 600; font-size: 20px; line-height: 1.35; margin: 0 0 18px; }}
  .options {{ display: flex; flex-direction: column; gap: 9px; }}
  .option {{
    display: flex; align-items: center; gap: 12px; width: 100%; text-align: left; padding: 12px 14px;
    background: var(--surface-2); border: 1px solid var(--border-soft); border-radius: 9px; color: var(--text);
    font-family: var(--font-body); font-size: 14.5px; cursor: pointer;
  }}
  .option:disabled {{ cursor: default; }}
  .option .letter {{ font-family: var(--font-mono); font-size: 12px; color: var(--text-faint); width: 18px; flex: none; }}
  .option.correct {{ border-color: var(--accent-moss); background: var(--accent-moss-soft); }}
  .option.correct .letter {{ color: var(--accent-moss); }}
  .option.wrong {{ border-color: var(--accent-rust); background: var(--accent-rust-soft); }}
  .option.wrong .letter {{ color: var(--accent-rust); }}
  .option.muted {{ opacity: 0.45; }}
  .reveal {{ margin-top: 16px; padding: 16px 16px 14px; border-radius: 10px; background: var(--surface-2); border-left: 3px solid var(--accent-gold); }}
  .reveal.right {{ border-left-color: var(--accent-moss); }}
  .reveal.wrong {{ border-left-color: var(--accent-rust); }}
  .reveal-head {{ font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; margin-bottom: 8px; }}
  .reveal.right .reveal-head {{ color: var(--accent-moss); }}
  .reveal.wrong .reveal-head {{ color: var(--accent-rust); }}
  .reveal p {{ margin: 0 0 8px; font-size: 14px; line-height: 1.6; color: var(--text); }}
  .reveal .source {{ font-family: var(--font-mono); font-size: 11px; color: var(--text-faint); margin: 0; }}
  .actions {{ display: flex; justify-content: flex-end; margin-top: 16px; }}
  .btn {{
    font-family: var(--font-mono); font-size: 12.5px; letter-spacing: 0.06em; text-transform: uppercase;
    color: var(--bg); background: var(--accent-gold); border: none; padding: 11px 18px; border-radius: 8px; cursor: pointer;
  }}
  .script-toggle {{ text-align: center; margin: 16px 0 0; }}
  .script-toggle button {{
    font-family: var(--font-mono); font-size: 11.5px; letter-spacing: 0.06em; text-transform: uppercase;
    color: var(--text-faint); background: none; border: none; text-decoration: underline; cursor: pointer;
  }}
  .script-panel {{
    margin-top: 12px; padding: 16px 18px; background: var(--surface); border: 1px solid var(--border);
    border-radius: 12px; font-size: 13.5px; line-height: 1.7; color: var(--text-dim); white-space: pre-wrap;
    max-height: 320px; overflow-y: auto;
  }}
  .audio-card {{
    display: flex; align-items: center; gap: 14px; padding: 14px 16px; margin-bottom: 18px;
    background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
  }}
  .audio-card .audio-label {{
    font-family: var(--font-mono); font-size: 10.5px; letter-spacing: 0.08em; text-transform: uppercase;
    color: var(--text-faint); margin-bottom: 6px;
  }}
  .audio-card audio {{ width: 100%; height: 34px; }}
  .footer-note {{ text-align: center; color: var(--text-faint); font-size: 12px; margin-top: 24px; line-height: 1.6; }}
  .footer-note strong {{ color: var(--text-dim); font-weight: 600; }}
  .footer-note a {{ color: var(--accent-gold); }}
  .end .card-body {{ text-align: center; padding: 34px 24px 28px; }}
  .end h2 {{ font-family: var(--font-display); font-weight: 700; font-size: 24px; margin: 0 0 6px; }}
  .end .score-line {{ font-family: var(--font-mono); font-size: 13px; color: var(--accent-gold); letter-spacing: 0.06em; margin-bottom: 14px; }}
  .end p.desc {{ color: var(--text-dim); font-size: 14px; line-height: 1.6; max-width: 380px; margin: 0 auto 20px; }}
  .best {{ font-family: var(--font-mono); font-size: 11.5px; color: var(--text-faint); margin-bottom: 20px; }}
  .archive-list {{ display: flex; flex-direction: column; gap: 8px; }}
  .archive-row {{
    display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 16px;
    background: var(--surface); border: 1px solid var(--border); border-radius: 10px; cursor: pointer;
    text-align: left; color: var(--text); font-family: var(--font-body); width: 100%;
  }}
  .archive-row .arow-date {{ font-family: var(--font-display); font-weight: 600; font-size: 16px; }}
  .archive-row .arow-tags {{ font-family: var(--font-mono); font-size: 10.5px; color: var(--text-faint); margin-top: 3px; }}
  .archive-row .arow-arrow {{ color: var(--accent-gold); font-family: var(--font-mono); }}
  .archive-empty {{
    text-align: center; padding: 36px 20px; color: var(--text-faint); font-size: 13.5px;
    background: var(--surface); border: 1px dashed var(--border); border-radius: 12px;
  }}
</style></head>
<body>
<div class="stage">
  <div class="masthead">
    <div class="kicker">Field log, recurring Monday, Wednesday, Saturday</div>
    <h1>{escape_xml(SHOW_TITLE)}</h1>
    <p>True, faintly disgusting things from history, science and art.<br>Guess before you read the case notes.</p>
  </div>
  <p class="addhome" id="addHomeNote">On iPhone, this only works as a real app, and notifications only work at all, once it is added to your home screen. Safari share icon, then Add to Home Screen, then open it from there.</p>
  <button id="enablePushBtn" class="enablePush">Turn on notifications for new files</button>
  <div class="nav">
    <button id="navToday" class="active">Today's File</button>
    <button id="navArchive">Archive</button>
  </div>
  <div id="fileMeta" class="file-meta"></div>
  <div id="audioSlot"></div>
  <div id="ticks" class="ticks"></div>
  <div id="mount"></div>
  <div class="footer-note" id="footerNote"></div>
</div>
<script>
  // Belt and braces alongside the CSS media query above, in case a given iOS version
  // does not treat this as display-mode standalone even when launched from the home screen.
  if (window.navigator.standalone === true) {{
    document.getElementById("enablePushBtn").style.display = "block";
    document.getElementById("addHomeNote").style.display = "none";
  }}
</script>
<script>
(function () {{
  var EPISODES = {episodes_json};
  var RANKS = [
    {{ min: 0, title: "Curious Novice", desc: "A respectable start. The archive rewards repeat visits." }},
    {{ min: 2, title: "Keen Observer", desc: "You're building a file. Most of this stuck." }},
    {{ min: 4, title: "Master Archivist", desc: "Full marks. You are, once again, insufferable at dinner." }}
  ];
  var els = {{
    nav: {{ today: document.getElementById("navToday"), archive: document.getElementById("navArchive") }},
    fileMeta: document.getElementById("fileMeta"),
    audioSlot: document.getElementById("audioSlot"),
    ticks: document.getElementById("ticks"),
    mount: document.getElementById("mount"),
    footerNote: document.getElementById("footerNote")
  }};
  var view = "today";
  var episode = EPISODES.length ? EPISODES[0] : null;
  var idx = 0, score = 0, answered = false, scriptOpen = false;

  function letterFor(i) {{ return String.fromCharCode(65 + i); }}
  function iconCheck() {{ return '<svg width="13" height="13" viewBox="0 0 16 16" fill="none"><path d="M3 8.5L6.2 12L13 4" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>'; }}
  function iconCross() {{ return '<svg width="13" height="13" viewBox="0 0 16 16" fill="none"><path d="M4 4L12 12M12 4L4 12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>'; }}

  function setNav(which) {{
    view = which;
    els.nav.today.classList.toggle("active", which === "today");
    els.nav.archive.classList.toggle("active", which === "archive");
  }}

  function renderFileMeta() {{
    if (view !== "today" || !episode) {{ els.fileMeta.textContent = ""; return; }}
    els.fileMeta.textContent = episode.label || episode.date;
  }}

  function renderAudioSlot() {{
    if (view !== "today" || !episode || !episode.audioUrl) {{ els.audioSlot.innerHTML = ""; return; }}
    els.audioSlot.innerHTML = '<div class="audio-card"><div style="flex:1;min-width:0;"><div class="audio-label">Narrated version</div><audio controls preload="none" src="' + episode.audioUrl + '"></audio></div></div>';
  }}

  function renderTicks() {{
    if (view !== "today" || !episode) {{ els.ticks.innerHTML = ""; return; }}
    els.ticks.innerHTML = "";
    episode.cases.forEach(function (_, i) {{
      var t = document.createElement("div");
      t.className = "tick" + (i < idx ? " done" : i === idx ? " current" : "");
      els.ticks.appendChild(t);
    }});
  }}

  function renderFooter() {{
    if (view === "archive") {{
      els.footerNote.innerHTML = 'Every past file lives here. New ones open <strong>Monday, Wednesday, Saturday</strong>. Prefer a podcast app, subscribe with <a href="feed.xml">feed.xml</a>.';
      return;
    }}
    if (!episode) {{ els.footerNote.innerHTML = ""; return; }}
    if (idx >= episode.cases.length) {{
      els.footerNote.innerHTML = "That's this file, closed. New cases open <strong>Monday, Wednesday, Saturday</strong>.";
    }} else {{
      els.footerNote.innerHTML = "Case file <strong>" + (idx + 1) + " of " + episode.cases.length + "</strong> in this file.";
    }}
  }}

  function scriptToggleHtml() {{
    if (!episode || !episode.script) return "";
    return '<div class="script-toggle"><button id="scriptBtn">' + (scriptOpen ? "Hide the script" : "Read the script for this file") + '</button></div>' +
      (scriptOpen ? '<div class="script-panel">' + episode.script.replace(/&/g, "&amp;").replace(/</g, "&lt;") + '</div>' : "");
  }}

  function bindScriptToggle() {{
    var btn = document.getElementById("scriptBtn");
    if (btn) btn.addEventListener("click", function () {{ scriptOpen = !scriptOpen; renderQuiz(); }});
  }}

  function renderQuiz() {{
    renderFileMeta();
    renderAudioSlot();
    renderTicks();
    renderFooter();

    if (!episode) {{
      els.mount.innerHTML = '<div class="archive-empty">No files yet. Check back Monday, Wednesday or Saturday morning.</div>';
      return;
    }}
    if (idx >= episode.cases.length) {{ renderEnd(); return; }}

    answered = false;
    var c = episode.cases[idx];
    var html = '' +
      '<div class="card">' +
        '<div class="card-head"><span class="case-no">' + c.no + '</span><span class="tag">' + c.tag + '</span></div>' +
        '<div class="card-body">' +
          '<p class="question">' + c.q + '</p>' +
          '<div class="options" id="opts">' +
            c.options.map(function (opt, i) {{ return '<button class="option" data-i="' + i + '"><span class="letter">' + letterFor(i) + '</span><span>' + opt + '</span></button>'; }}).join("") +
          '</div>' +
          '<div id="revealSlot"></div>' +
        '</div>' +
      '</div>' +
      scriptToggleHtml();

    els.mount.innerHTML = html;
    bindScriptToggle();

    Array.prototype.forEach.call(document.querySelectorAll(".option"), function (btn) {{
      btn.addEventListener("click", function () {{ onAnswer(parseInt(btn.getAttribute("data-i"), 10)); }});
    }});
  }}

  function onAnswer(choice) {{
    if (answered) return;
    answered = true;
    var c = episode.cases[idx];
    var isRight = choice === c.correct;
    if (isRight) score++;

    Array.prototype.forEach.call(document.querySelectorAll(".option"), function (btn) {{
      var i = parseInt(btn.getAttribute("data-i"), 10);
      btn.disabled = true;
      if (i === c.correct) btn.classList.add("correct");
      else if (i === choice) btn.classList.add("wrong");
      else btn.classList.add("muted");
    }});

    var slot = document.getElementById("revealSlot");
    slot.innerHTML = '' +
      '<div class="reveal ' + (isRight ? "right" : "wrong") + '">' +
        '<div class="reveal-head">' + (isRight ? iconCheck() : iconCross()) + ' ' + (isRight ? "Correct. " : "Not quite. ") + c.revealTitle + '</div>' +
        '<p>' + c.revealText + '</p>' +
        '<p class="source">From ' + c.source + '</p>' +
      '</div>' +
      '<div class="actions"><button class="btn" id="nextBtn">' + (idx < episode.cases.length - 1 ? "Next case →" : "See results →") + '</button></div>';

    document.getElementById("nextBtn").addEventListener("click", function () {{ idx++; renderQuiz(); }});
  }}

  function getBest(key) {{ try {{ return parseInt(localStorage.getItem(key) || "0", 10); }} catch (e) {{ return 0; }} }}
  function setBest(key, v) {{ try {{ var cur = getBest(key); if (v > cur) localStorage.setItem(key, String(v)); return Math.max(cur, v); }} catch (e) {{ return v; }} }}

  function renderEnd() {{
    renderTicks();
    renderFooter();
    var rank = RANKS[0];
    for (var i = RANKS.length - 1; i >= 0; i--) {{ if (score >= RANKS[i].min) {{ rank = RANKS[i]; break; }} }}
    var bestKey = "curioFilesBest_" + episode.date;
    var best = setBest(bestKey, score);

    els.mount.innerHTML = '' +
      '<div class="card end">' +
        '<div class="card-body">' +
          '<div class="case-no" style="margin-bottom:14px;">FILE CLOSED, ' + episode.cases.length + ' OF ' + episode.cases.length + '</div>' +
          '<h2>' + rank.title + '</h2>' +
          '<div class="score-line">' + score + ' / ' + episode.cases.length + ' CORRECT</div>' +
          '<p class="desc">' + rank.desc + '</p>' +
          '<div class="best">Personal best on this file, ' + best + ' / ' + episode.cases.length + '</div>' +
          '<button class="btn" id="againBtn">Reopen this file</button>' +
        '</div>' +
      '</div>' +
      scriptToggleHtml();
    bindScriptToggle();
    document.getElementById("againBtn").addEventListener("click", function () {{ idx = 0; score = 0; renderQuiz(); }});
  }}

  function openEpisode(ep) {{
    episode = ep; idx = 0; score = 0; scriptOpen = false;
    setNav("today");
    renderQuiz();
  }}

  function renderArchive() {{
    setNav("archive");
    els.fileMeta.textContent = "";
    els.audioSlot.innerHTML = "";
    els.ticks.innerHTML = "";
    renderFooter();

    if (EPISODES.length === 0) {{
      els.mount.innerHTML = '<div class="archive-empty">No files archived yet.</div>';
      return;
    }}
    els.mount.innerHTML = '<div class="archive-list">' + EPISODES.map(function (d) {{
      var tags = (d.cases || []).map(function (c) {{ return c.tag; }}).join(" · ");
      return '<button class="archive-row" data-date="' + d.date + '"><div><div class="arow-date">' + (d.label || d.date) + '</div><div class="arow-tags">' + tags + '</div></div><span class="arow-arrow">→</span></button>';
    }}).join("") + '</div>';

    Array.prototype.forEach.call(document.querySelectorAll(".archive-row"), function (row) {{
      row.addEventListener("click", function () {{
        var date = row.getAttribute("data-date");
        var found = EPISODES.filter(function (d) {{ return d.date === date; }})[0];
        if (found) openEpisode(found);
      }});
    }});
  }}

  els.nav.today.addEventListener("click", function () {{ episode = EPISODES.length ? EPISODES[0] : null; idx = 0; score = 0; scriptOpen = false; setNav("today"); renderQuiz(); }});
  els.nav.archive.addEventListener("click", function () {{ renderArchive(); }});

  renderQuiz();
}})();
</script>
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
