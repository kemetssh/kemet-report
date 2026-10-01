import json, os, sys
from datetime import datetime, timedelta, timezone
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

S = requests.Session()
RETRY = Retry(total=5, backoff_factor=2, allowed_methods=None,
              status_forcelist=[429, 500, 502, 503, 504],
              raise_on_status=False)
S.mount("https://", HTTPAdapter(max_retries=RETRY))

TG = "https://api.telegram.org/bot" + os.environ["TELEGRAM_BOT_TOKEN"]
CHAT = os.environ["TELEGRAM_CHAT_ID"]
GKEY = os.environ["GEMINI_API_KEY"]
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
GURL = ("https://generativelanguage.googleapis.com/v1beta/models/"
        + MODEL + ":generateContent")
YT = "https://www.googleapis.com/youtube/v3"
AN = "https://youtubeanalytics.googleapis.com/v2/reports"
SNAP = "daily.json"


def tell(text):
    payload = {"chat_id": CHAT, "text": text[:3900]}
    S.post(TG + "/sendMessage", json=payload, timeout=30)


def load():
    if os.path.exists(SNAP):
        return json.load(open(SNAP))
    return {}


def token():
    data = {"client_id": os.environ["YT_CLIENT_ID"],
            "client_secret": os.environ["YT_CLIENT_SECRET"],
            "refresh_token": os.environ["YT_REFRESH_TOKEN"],
            "grant_type": "refresh_token"}
    r = S.post("https://oauth2.googleapis.com/token", data=data,
               timeout=60)
    j = r.json()
    if "access_token" not in j:
        raise RuntimeError("YouTube login expired. Run login.py again "
                           "and update the YT_REFRESH_TOKEN secret.")
    return j["access_token"]


def channel(h):
    p = {"part": "snippet,statistics", "mine": "true"}
    r = S.get(YT + "/channels", params=p, headers=h)
    c = r.json()["items"][0]
    s = c["statistics"]
    return (c["id"], int(s.get("subscriberCount", 0)),
            int(s.get("viewCount", 0)), int(s.get("videoCount", 0)))


def my_videos(h):
    p = {"part": "contentDetails", "mine": "true"}
    r = S.get(YT + "/channels", params=p, headers=h)
    rel = r.json()["items"][0]["contentDetails"]["relatedPlaylists"]
    p = {"part": "contentDetails", "playlistId": rel["uploads"],
         "maxResults": 50}
    r = S.get(YT + "/playlistItems", params=p, headers=h)
    items = r.json().get("items", [])
    ids = [i["contentDetails"]["videoId"] for i in items]
    if not ids:
        return []
    p = {"part": "snippet,statistics", "id": ",".join(ids)}
    r = S.get(YT + "/videos", params=p, headers=h)
    out = []
    for v in r.json().get("items", []):
        out.append({"id": v["id"], "title": v["snippet"]["title"],
                    "views": int(v["statistics"].get("viewCount", 0))})
    return out


def new_comments(h, me):
    p = {"part": "snippet", "allThreadsRelatedToChannelId": me,
         "maxResults": 50, "order": "time"}
    r = S.get(YT + "/commentThreads", params=p, headers=h)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    n = 0
    for t in r.json().get("items", []):
        top = t["snippet"]["topLevelComment"]["snippet"]
        if top.get("authorChannelId", {}).get("value") == me:
            continue
        try:
            when = datetime.fromisoformat(
                top["publishedAt"].replace("Z", "+00:00"))
        except Exception:
            continue
        if when >= cutoff:
            n += 1
    return n


def week(h):
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=7)
    q = {"ids": "channel==MINE", "startDate": str(start),
         "endDate": str(end),
         "metrics": "views,estimatedMinutesWatched,subscribersGained"}
    r = S.get(AN, params=q, headers=h)
    if r.status_code != 200:
        return None
    rows = r.json().get("rows") or []
    if not rows or not any(rows[0]):
        return None
    return rows[0]


def market(h, me):
    since = datetime.now(timezone.utc) - timedelta(days=30)
    p = {"part": "snippet", "q": "ancient egypt", "type": "video",
         "videoDuration": "short", "order": "viewCount",
         "publishedAfter": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
         "maxResults": 10, "relevanceLanguage": "en"}
    r = S.get(YT + "/search", params=p, headers=h)
    if r.status_code != 200:
        return []
    items = [i for i in r.json().get("items", [])
             if i["snippet"]["channelId"] != me]
    ids = [i["id"]["videoId"] for i in items]
    if not ids:
        return []
    p = {"part": "statistics", "id": ",".join(ids)}
    r = S.get(YT + "/videos", params=p, headers=h)
    views = {}
    for v in r.json().get("items", []):
        views[v["id"]] = int(v["statistics"].get("viewCount", 0))
    out = [(i["snippet"]["title"], views.get(i["id"]["videoId"], 0))
           for i in items]
    out.sort(key=lambda x: -x[1])
    return out[:6]


def make_ideas(summary, trending):
    prompt = (
        "You advise a small YouTube Shorts channel, Kemet | Ancient "
        "Egypt (facts and myth, calm cinematic tone). Today's data:\n"
        + summary + "\nRecent popular videos on the topic (title, "
        "views):\n" + trending + "\nSuggest 3 NEW video ideas that fit "
        "the channel and could earn views and subscribers. Use proven "
        "patterns from the popular titles but do not copy them, and "
        "stay factually accurate. Return ONLY a JSON array of 3 "
        "objects with keys: topic (short working title), hook (the "
        "first spoken line, a surprising fact or question), why (one "
        "sentence)."
    )
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        r = S.post(GURL, headers={"x-goog-api-key": GKEY},
                   json=body, timeout=120)
        parts = r.json()["candidates"][0]["content"]["parts"]
        text = "".join(p.get("text", "") for p in parts)
        text = text.replace("```json", "").replace("```", "").strip()
        ideas = json.loads(text)
        return [i for i in ideas if isinstance(i, dict)
                and i.get("topic")][:3]
    except Exception as e:
        print("ideas failed:", e)
        return []


def build():
    h = {"Authorization": "Bearer " + token()}
    snap = load()
    me, subs, views, count = channel(h)
    vids = my_videos(h)
    first = "videos" not in snap
    old = snap.get("videos", {})
    today = datetime.now(timezone.utc).date()
    text = "KEMET DAILY REPORT - " + str(today) + "\n\n"
    if first:
        text += "Channel: %d subscribers, %d views, %d videos\n" % (
            subs, views, count)
        text += "(first report, changes start tomorrow)\n"
    else:
        text += "Channel: %d subscribers (%+d), %d views (%+d), " % (
            subs, subs - snap.get("subs", subs),
            views, views - snap.get("views", views))
        text += "%d videos\n" % count
        gains = []
        for v in vids:
            gains.append((v["views"] - old.get(v["id"], v["views"]),
                          v["title"], v["views"]))
        gains.sort(key=lambda x: -x[0])
        text += "\nMost new views since yesterday:\n"
        for g, t, tot in gains[:3]:
            text += "- %s: %+d (%d total)\n" % (t[:40], g, tot)
    best = sorted(vids, key=lambda v: -v["views"])[:3]
    text += "\nBest videos overall:\n"
    for v in best:
        text += "- %s: %d views\n" % (v["title"][:40], v["views"])
    w = week(h)
    if w:
        text += "\nLast 7 days: %d views, %d minutes watched, " % (
            w[0], w[1])
        text += "%+d subscribers\n" % w[2]
    n = new_comments(h, me)
    text += "\nNew comments (24h): %d\n" % n
    trend = market(h, me)
    trending = "\n".join("%s (%d)" % (t[:70], v) for t, v in trend)
    if trend:
        text += "\nPopular Ancient Egypt shorts this month:\n"
        for t, v in trend[:4]:
            text += "- %s (%d views)\n" % (t[:50], v)
    ideas = make_ideas(text[:900], trending or "none found")
    if ideas:
        text += "\nIDEAS TO TRY:\n"
        for i, d in enumerate(ideas, 1):
            text += "%d. %s\n   Hook: %s\n   Why: %s\n" % (
                i, d.get("topic"), d.get("hook", ""), d.get("why", ""))
    text += "\nIdeas are educated guesses; nobody can promise a "
    text += "viral video."
    snap["videos"] = {v["id"]: v["views"] for v in vids}
    snap["subs"] = subs
    snap["views"] = views
    snap["ideas"] = ideas
    json.dump(snap, open(SNAP, "w"))
    return text


def main():
    try:
        tell(build())
    except Exception as e:
        tell("Daily report failed: " + str(e)[:300])
        sys.exit(1)


if __name__ == "__main__":
    main()
