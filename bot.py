"""Kemet bot: you send a finished video in Telegram, it reviews it and you tap choices.

Runs on GitHub Actions (see .github/workflows/bot.yml). No server, no iSH.
Flow: video -> review + fact check -> pick title -> pick description ->
pick thumbnail -> upload (private) -> optional "try to make public".
Nothing is uploaded or published without a tap from you.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent
STATE_FILE = Path(os.getenv("STATE_PATH", ROOT / "state_bot.json"))

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = str(os.environ["TELEGRAM_CHAT_ID"])
GKEY = os.environ["GEMINI_API_KEY"]
MODELS = [os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite"), "gemini-3-flash-preview"]

TG_BASE = os.getenv("TG_BASE", "https://api.telegram.org")
TGAPI = f"{TG_BASE}/bot{TG_TOKEN}"
TGFILE = f"{TG_BASE}/file/bot{TG_TOKEN}"
GBASE = os.getenv("GEMINI_BASE", "https://generativelanguage.googleapis.com")
GOOGLE_TOKEN = os.getenv("GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token")
YT = os.getenv("YT_BASE", "https://www.googleapis.com")
RUN_SECONDS = int(os.getenv("RUN_SECONDS", "240"))

S = requests.Session()
_retry = Retry(total=4, connect=4, read=3, backoff_factor=1.5,
               allowed_methods=None, status_forcelist=[])
S.mount("https://", HTTPAdapter(max_retries=_retry))
S.mount("http://", HTTPAdapter(max_retries=_retry))

NOTICE = ("YouTube rule: only publish videos that follow its policies and that you "
          "have the rights to (voice, images, music). If your video uses realistic "
          "AI-made scenes, switch on the 'altered content' label in YouTube Studio.")


def clean(text):
    """Never let the bot token or keys reach logs or chat."""
    text = str(text)
    for secret in (TG_TOKEN, GKEY):
        if secret:
            text = text.replace(secret, "***")
    return text


# ---------------- state ----------------
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"offset": 0, "jobs": {}}


def save_state(st):
    now = time.time()
    st["jobs"] = {k: v for k, v in st["jobs"].items() if now - v.get("created", now) < 7 * 86400}
    STATE_FILE.write_text(json.dumps(st, indent=1))


# ---------------- telegram ----------------
def tg(method, **data):
    r = S.post(f"{TGAPI}/{method}", json=data, timeout=60)
    try:
        j = r.json()
    except Exception:
        raise RuntimeError(f"Telegram {method} HTTP {r.status_code}")
    if not j.get("ok"):
        raise RuntimeError(f"Telegram {method}: {j.get('description')}")
    return j["result"]


def send(text, rows=None):
    chunks = [text[i:i + 3800] for i in range(0, len(text), 3800)] or [""]
    for i, c in enumerate(chunks):
        d = {"chat_id": CHAT, "text": c, "disable_web_page_preview": True}
        if rows and i == len(chunks) - 1:
            d["reply_markup"] = {"inline_keyboard": rows}
        tg("sendMessage", **d)


def send_photo(path, caption):
    with open(path, "rb") as f:
        r = S.post(f"{TGAPI}/sendPhoto", data={"chat_id": CHAT, "caption": caption},
                   files={"photo": f}, timeout=120)
    r.raise_for_status()


def tg_download(file_id, dest):
    info = tg("getFile", file_id=file_id)
    with S.get(f"{TGFILE}/{info['file_path']}", stream=True, timeout=300) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)


# ---------------- video helpers ----------------
def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "format=duration:stream=width,height", "-of", "json", str(path)],
        capture_output=True, text=True)
    j = json.loads(out.stdout or "{}")
    dur = float(j.get("format", {}).get("duration", 0) or 0)
    st = (j.get("streams") or [{}])[0]
    return dur, int(st.get("width", 0) or 0), int(st.get("height", 0) or 0)


def frame(path, t, out):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(path),
                    "-frames:v", "1", "-q:v", "2", str(out)], check=True)
    return Path(out).exists() and Path(out).stat().st_size > 0


# ---------------- gemini ----------------
def gemini(parts):
    body = {"contents": [{"parts": parts}],
            "generationConfig": {"responseMimeType": "application/json"}}
    last = "no model answered"
    for m in MODELS:
        for attempt in range(3):
            r = S.post(f"{GBASE}/v1beta/models/{m}:generateContent",
                       headers={"x-goog-api-key": GKEY}, json=body, timeout=240)
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"{m} HTTP {r.status_code}"
                time.sleep(6 * (attempt + 1))
                continue
            if r.status_code == 404:
                last = f"{m} not found"
                break
            r.raise_for_status()
            cand = r.json()["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in cand)
            a, b = text.find("{"), text.rfind("}")
            return json.loads(text[a:b + 1])
    raise RuntimeError(f"Gemini failed: {last}")


def gemini_upload(path, mime):
    size = os.path.getsize(path)
    r = S.post(f"{GBASE}/upload/v1beta/files", headers={
        "x-goog-api-key": GKEY, "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start", "X-Goog-Upload-Header-Content-Length": str(size),
        "X-Goog-Upload-Header-Content-Type": mime, "Content-Type": "application/json"},
        json={"file": {"display_name": "kemet-review"}}, timeout=60)
    r.raise_for_status()
    url = r.headers["x-goog-upload-url"]
    with open(path, "rb") as f:
        r = S.post(url, headers={"Content-Length": str(size), "X-Goog-Upload-Offset": "0",
                                 "X-Goog-Upload-Command": "upload, finalize"},
                   data=f, timeout=900)
    r.raise_for_status()
    info = r.json()["file"]
    for _ in range(60):
        if info.get("state", "ACTIVE") == "ACTIVE":
            return info
        if info.get("state") == "FAILED":
            raise RuntimeError("Gemini could not process the video")
        time.sleep(4)
        g = S.get(f"{GBASE}/v1beta/{info['name']}", headers={"x-goog-api-key": GKEY}, timeout=60)
        g.raise_for_status()
        info = g.json()
    raise RuntimeError("Gemini took too long to process the video")


REVIEW_PROMPT = """You are the producer of a YouTube history channel called "Kemet | Ancient Egypt".
The owner made this video himself (his own voice and edit). Watch and listen to it fully.
Rules: be honest and specific, never flatter. Facts about Ancient Egypt must be accurate.
Do not invent claims. If you are not sure something is true, say so.
Video length: {dur:.0f} seconds, size {w}x{h}.

Return JSON only with exactly these keys:
{{
 "verdict": "ready" or "fix first",
 "score": integer 1-10 (hook, pacing, clarity, sound, visuals combined),
 "summary": "2 sentences, honest",
 "issues": [{{"at": "mm:ss", "problem": "...", "fix": "..."}}],  // facts, sound, text, copyright look, pacing. max 6, empty if none
 "strengths": ["..."],  // max 3
 "titles": ["3 different titles, max 70 chars, curious but TRUE, no lies"],
 "descriptions": ["2 options, each 2-3 lines then hashtags (#AncientEgypt, add #Shorts if under 60s vertical)"],
 "tags": ["up to 12 search tags"],
 "thumb_times": [3 numbers in seconds inside the video, sharp frames with a clear subject]
}}"""


def review_video(path, dur, w, h):
    info = gemini_upload(path, "video/mp4")
    try:
        data = gemini([{"file_data": {"mime_type": "video/mp4", "file_uri": info["uri"]}},
                       {"text": REVIEW_PROMPT.format(dur=dur, w=w, h=h)}])
    finally:
        try:
            S.delete(f"{GBASE}/v1beta/{info['name']}", headers={"x-goog-api-key": GKEY}, timeout=30)
        except Exception:
            pass
    data["titles"] = [t[:100] for t in data.get("titles", [])][:3]
    data["descriptions"] = data.get("descriptions", [])[:2]
    times = [float(t) for t in data.get("thumb_times", []) if isinstance(t, (int, float))]
    times = [min(max(t, 0.2), max(dur - 0.5, 0.2)) for t in times][:3]
    while len(times) < 3 and dur > 0:
        times.append(dur * (len(times) + 1) / 4)
    data["thumb_times"] = times
    return data


def more_titles(job):
    r = job["review"]
    data = gemini([{"text": (
        'Channel "Kemet | Ancient Egypt". Video summary: ' + r.get("summary", "")
        + ". Current titles: " + json.dumps(job["review"]["titles"])
        + '. Give 3 NEW different titles, max 70 chars, curious but true. '
          'JSON only: {"titles": ["","",""]}')}])
    return [t[:100] for t in data.get("titles", [])][:3]


# ---------------- youtube ----------------
def yt_token():
    r = S.post(GOOGLE_TOKEN, data={
        "client_id": os.environ["YT_CLIENT_ID"], "client_secret": os.environ["YT_CLIENT_SECRET"],
        "refresh_token": os.environ["YT_REFRESH_TOKEN"], "grant_type": "refresh_token"}, timeout=60)
    r.raise_for_status()
    return r.json()["access_token"]


def yt_upload(path, title, desc, tags, tok):
    meta = {"snippet": {"title": title[:100], "description": desc[:4900], "tags": tags[:15],
                        "categoryId": "27"},
            "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False}}
    size = os.path.getsize(path)
    r = S.post(f"{YT}/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
               headers={"Authorization": f"Bearer {tok}", "X-Upload-Content-Length": str(size),
                        "X-Upload-Content-Type": "video/mp4", "Content-Type": "application/json"},
               json=meta, timeout=60)
    r.raise_for_status()
    url = r.headers["Location"]
    with open(path, "rb") as f:
        r = S.put(url, data=f, headers={"Content-Length": str(size), "Content-Type": "video/mp4"},
                  timeout=1800)
    r.raise_for_status()
    return r.json()["id"]


def yt_thumb(vid, img, tok):
    with open(img, "rb") as f:
        r = S.post(f"{YT}/upload/youtube/v3/thumbnails/set?videoId={vid}&uploadType=media",
                   headers={"Authorization": f"Bearer {tok}", "Content-Type": "image/jpeg"},
                   data=f, timeout=120)
    r.raise_for_status()


def yt_privacy(vid, tok):
    r = S.get(f"{YT}/youtube/v3/videos", params={"part": "status", "id": vid},
              headers={"Authorization": f"Bearer {tok}"}, timeout=60)
    r.raise_for_status()
    items = r.json().get("items", [])
    return items[0]["status"]["privacyStatus"] if items else "unknown"


def yt_set_public(vid, tok):
    r = S.put(f"{YT}/youtube/v3/videos?part=status",
              headers={"Authorization": f"Bearer {tok}"},
              json={"id": vid, "status": {"privacyStatus": "public",
                                          "selfDeclaredMadeForKids": False}}, timeout=60)
    r.raise_for_status()


# ---------------- flow ----------------
def btn(text, jid, act, val=""):
    return {"text": text, "callback_data": f"{jid}|{act}|{val}"}


def review_text(job):
    r = job["review"]
    lines = [f"🎬 {job['name']}  ({job['dur']:.0f}s, {job['w']}x{job['h']})",
             f"Verdict: {r.get('verdict', '?').upper()}   Score: {r.get('score', '?')}/10",
             "", r.get("summary", "")]
    if r.get("issues"):
        lines += ["", "Things to check:"]
        for i in r["issues"][:6]:
            lines.append(f"• {i.get('at', '?')} - {i.get('problem', '')}  → {i.get('fix', '')}")
    if r.get("strengths"):
        lines += ["", "Good:"] + [f"+ {s}" for s in r["strengths"][:3]]
    return "\n".join(lines)


def ask_title(job):
    job["stage"] = "title"
    jid = job["id"]
    body = "Pick a title:\n\n" + "\n".join(f"{i + 1}. {t}" for i, t in enumerate(job["review"]["titles"]))
    rows = [[btn(f"Title {i + 1}", jid, "t", i) for i in range(len(job["review"]["titles"]))],
            [btn("More titles", jid, "m"), btn("Cancel", jid, "x")]]
    send(body, rows)


def ask_desc(job):
    job["stage"] = "desc"
    jid = job["id"]
    ds = job["review"]["descriptions"]
    body = "Pick a description:\n\n" + "\n\n".join(f"{i + 1}.\n{d}" for i, d in enumerate(ds))
    send(body, [[btn(f"Description {i + 1}", jid, "d", i) for i in range(len(ds))],
                [btn("Cancel", jid, "x")]])


def ask_thumb(job, path):
    job["stage"] = "thumb"
    jid = job["id"]
    n = 0
    for i, t in enumerate(job["review"]["thumb_times"]):
        out = Path(path).parent / f"th{i}.jpg"
        if frame(path, t, out):
            send_photo(out, f"Thumbnail {i + 1}")
            n += 1
    if n == 0:
        job["thumb"] = -1
        return ask_confirm(job)
    send("Pick a thumbnail:", [[btn(f"Thumb {i + 1}", jid, "h", i) for i in range(n)],
                               [btn("Skip", jid, "h", -1), btn("Cancel", jid, "x")]])


def ask_confirm(job):
    job["stage"] = "confirm"
    jid = job["id"]
    r = job["review"]
    thumb = "none" if job.get("thumb", -1) < 0 else f"option {job['thumb'] + 1}"
    send(f"Ready to upload (PRIVATE):\n\nTitle: {job['title']}\n\n"
         f"Description:\n{r['descriptions'][job['desc']]}\n\nThumbnail: {thumb}\n\n"
         "It stays private until you publish it.",
         [[btn("⬆️ Upload as private", jid, "u")], [btn("Cancel", jid, "x")]])


def start_job(msg, st):
    v = msg.get("video") or msg.get("document")
    jid = str(msg["message_id"])
    name = (v.get("file_name") or "your video")
    if v.get("file_size", 0) > 19.5 * 1024 * 1024:
        send("This video is over 20 MB, which is the most a Telegram bot can receive.\n"
             "Send it again as a normal video (not as a file) so Telegram shrinks it, "
             "or export it smaller (720x1280).")
        return
    send("Got it. Watching and checking your video now (about 1-3 minutes)...")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "v.mp4"
        tg_download(v["file_id"], p)
        dur, w, h = probe(p)
        if dur <= 0:
            send("I could not read that file as a video. Please send it again.")
            return
        review = review_video(p, dur, w, h)
    job = {"id": jid, "file_id": v["file_id"], "name": name, "dur": dur, "w": w, "h": h,
           "review": review, "created": time.time(), "stage": "title"}
    st["jobs"][jid] = job
    send(review_text(job))
    ask_title(job)


def do_upload(job):
    job["stage"] = "uploading"
    send("Uploading as private... (a minute or two)")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "v.mp4"
        tg_download(job["file_id"], p)
        tok = yt_token()
        desc = job["review"]["descriptions"][job["desc"]]
        vid = yt_upload(p, job["title"], desc, job["review"].get("tags", []), tok)
        thumb_note = ""
        if job.get("thumb", -1) >= 0:
            img = Path(d) / "chosen.jpg"
            try:
                frame(p, job["review"]["thumb_times"][job["thumb"]], img)
                yt_thumb(vid, img, tok)
                thumb_note = "Thumbnail set. "
            except Exception as e:
                thumb_note = f"Thumbnail not set ({clean(e)[:120]}). "
        status = yt_privacy(vid, tok)
    job["video_id"] = vid
    job["stage"] = "uploaded"
    send(f"✅ Uploaded. {thumb_note}Status: {status.upper()}\nhttps://youtu.be/{vid}\n\n{NOTICE}",
         [[btn("Try to make it public", job["id"], "p")],
          [btn("I will publish it myself", job["id"], "k")]])


def do_publish(job):
    tok = yt_token()
    vid = job["video_id"]
    try:
        yt_set_public(vid, tok)
        status = yt_privacy(vid, tok)
    except Exception as e:
        status = "private"
        print("publish error:", clean(e))
    if status == "public":
        job["stage"] = "done"
        send(f"🚀 It is PUBLIC now: https://youtu.be/{vid}")
    else:
        send("YouTube kept it PRIVATE. New API projects are locked to private until Google "
             "audits them, so this is expected.\n\nTo publish: YouTube app → Profile → Your "
             "videos → open it → Edit → Visibility → Public.\n" + f"https://youtu.be/{vid}")


def trigger_report():
    repo = os.getenv("GITHUB_REPOSITORY")
    tok = os.getenv("GITHUB_TOKEN")
    if not repo or not tok:
        send("The report runs by itself every morning. (Manual trigger is not available here.)")
        return
    r = S.post(f"https://api.github.com/repos/{repo}/actions/workflows/daily.yml/dispatches",
               headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"},
               json={"ref": os.getenv("GITHUB_REF_NAME", "main")}, timeout=60)
    send("Report requested. It will arrive in a couple of minutes." if r.status_code == 204
         else f"Could not start the report (HTTP {r.status_code}). It still runs every morning.")


HELP = ("Send me your finished video (as a normal video, under 20 MB).\n"
        "I will check it and give you choices to tap. Nothing goes on YouTube without your tap.\n\n"
        "/report - daily channel report now\n/status - videos waiting for you\n/help")


def on_message(msg, st):
    if str(msg.get("chat", {}).get("id")) != CHAT:
        return
    doc = msg.get("document") or {}
    if msg.get("video") or str(doc.get("mime_type", "")).startswith("video/"):
        return start_job(msg, st)
    text = (msg.get("text") or "").strip().lower()
    if text in ("/start", "/help"):
        send(HELP)
    elif text == "/report":
        trigger_report()
    elif text == "/status":
        open_jobs = [j for j in st["jobs"].values() if j["stage"] not in ("done", "cancelled", "uploaded")]
        send("Waiting for your choice:\n" + "\n".join(f"• {j['name']} ({j['stage']})" for j in open_jobs)
             if open_jobs else "Nothing waiting. Send me a video any time.")
    else:
        send("Send me a video file, or use /help.")


def on_callback(cb, st):
    msg = cb["message"]
    if str(msg["chat"]["id"]) != CHAT:
        return
    try:
        tg("answerCallbackQuery", callback_query_id=cb["id"])
    except Exception:
        pass  # tap is older than a minute (normal: the bot checks on a timer)
    try:
        tg("editMessageReplyMarkup", chat_id=CHAT, message_id=msg["message_id"],
           reply_markup={"inline_keyboard": []})
    except Exception:
        pass
    jid, act, val = (cb["data"].split("|") + ["", ""])[:3]
    job = st["jobs"].get(jid)
    if not job:
        return send("That video is no longer waiting (too old). Send it again.")
    stage = job["stage"]
    if act == "x":
        job["stage"] = "cancelled"
        return send("Cancelled. Nothing was uploaded.")
    if act == "t" and stage == "title":
        job["title"] = job["review"]["titles"][int(val)]
        return ask_desc(job)
    if act == "m" and stage == "title":
        job["review"]["titles"] = more_titles(job)
        return ask_title(job)
    if act == "d" and stage == "desc":
        job["desc"] = int(val)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "v.mp4"
            tg_download(job["file_id"], p)
            return ask_thumb(job, p)
    if act == "h" and stage == "thumb":
        job["thumb"] = int(val)
        return ask_confirm(job)
    if act == "u" and stage == "confirm":
        return do_upload(job)
    if act == "p" and stage == "uploaded":
        return do_publish(job)
    if act == "k":
        job["stage"] = "done"
        return send("OK. It stays private until you publish it in the YouTube app. 🏺")
    send("That button is out of date.")


def main():
    st = load_state()
    end = time.time() + RUN_SECONDS
    first = True
    while first or time.time() < end:
        wait = 0 if RUN_SECONDS <= 0 else int(min(25, max(end - time.time(), 0)))
        first = False
        try:
            updates = S.get(f"{TGAPI}/getUpdates", params={
                "offset": st["offset"], "timeout": wait,
                "allowed_updates": json.dumps(["message", "callback_query"])},
                timeout=wait + 30).json().get("result", [])
        except Exception as e:
            print("poll error:", clean(e))
            time.sleep(3)
            continue
        for u in updates:
            st["offset"] = u["update_id"] + 1
            try:
                if "callback_query" in u:
                    on_callback(u["callback_query"], st)
                elif "message" in u:
                    on_message(u["message"], st)
            except Exception as e:
                print("error:", clean(e))
                try:
                    send("⚠️ Something failed: " + clean(e)[:300] + "\nNothing was published. Try again.")
                except Exception:
                    pass
            save_state(st)
        if RUN_SECONDS <= 0:
            break
    save_state(st)


if __name__ == "__main__":
    main()
