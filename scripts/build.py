#!/usr/bin/env python3
"""Build a batch of short Quran recitation videos over nature backgrounds.

Each clip is a run of consecutive ayat from one surah (never cut mid-ayah),
roughly 30-60 seconds long, recited by the reciter set in config.json, laid
over a nature video from Pixabay, with the surah name and ayah range on top.

Outputs (in --out):
  *.mp4           the rendered clips
  manifest.json   one entry per clip: file, url, surah, ayah range, caption,
                  planned publish time, background credit
The reading position is advanced in state.json so the next batch continues
where this one stopped.
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import random
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, features

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
STATE_PATH = ROOT / "state.json"
FONT_BOLD = str(ROOT / "fonts" / "Amiri-Bold.ttf")
FONT_REG = str(ROOT / "fonts" / "Amiri-Regular.ttf")

QURAN_API = "https://api.quran.com/api/v4"
AUDIO_BASE = "https://verses.quran.com/"
PIXABAY_API = "https://pixabay.com/api/videos/"

W, H = 1080, 1920
AR_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")

session = requests.Session()
session.headers["User-Agent"] = "quran-videos/1.0 (+https://github.com)"


def log(*a):
    print(*a, flush=True)


def get_json(url, params=None, tries=5):
    for i in range(tries):
        try:
            r = session.get(url, params=params, timeout=60)
            if r.status_code == 429:
                time.sleep(10 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            if i == tries - 1:
                raise
            log(f"  retry {url}: {e}")
            time.sleep(3 * (i + 1))


def download(url, path, tries=5):
    path = pathlib.Path(path)
    if path.exists() and path.stat().st_size > 0:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    for i in range(tries):
        try:
            with session.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        f.write(chunk)
            tmp.rename(path)
            return path
        except requests.RequestException as e:
            if i == tries - 1:
                raise
            log(f"  retry download {url}: {e}")
            time.sleep(3 * (i + 1))


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        log("COMMAND FAILED:", " ".join(map(str, cmd)))
        log(p.stderr[-4000:])
        raise SystemExit(1)
    return p.stdout


def duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", str(path)])
    return float(out.strip())


# ---------------------------------------------------------------- Quran data

def find_recitation_id():
    rc = CONFIG["reciter"]
    if rc.get("recitation_id"):
        return int(rc["recitation_id"])
    data = get_json(f"{QURAN_API}/resources/recitations", {"language": "en"})
    for r in data["recitations"]:
        name = (r.get("reciter_name") or "").lower()
        style = (r.get("style") or "").lower()
        if rc["quran_com_reciter_match"] in name and rc["quran_com_style_match"] in style:
            log(f"Recitation: id={r['id']} {r['reciter_name']} ({r.get('style')})")
            return int(r["id"])
    raise SystemExit("Could not find the reciter on quran.com: "
                     + json.dumps(data["recitations"], ensure_ascii=False))


def load_chapters():
    data = get_json(f"{QURAN_API}/chapters", {"language": "ar"})
    return {c["id"]: c for c in data["chapters"]}


_audio_index = {}


def chapter_audio_urls(rec_id, chapter):
    if chapter in _audio_index:
        return _audio_index[chapter]
    urls, page = {}, 1
    while True:
        data = get_json(f"{QURAN_API}/recitations/{rec_id}/by_chapter/{chapter}",
                        {"per_page": 50, "page": page})
        for f in data["audio_files"]:
            u = f["url"]
            if u.startswith("//"):
                u = "https:" + u
            elif not u.startswith("http"):
                u = AUDIO_BASE + u.lstrip("/")
            urls[int(f["verse_key"].split(":")[1])] = u
        nxt = (data.get("pagination") or {}).get("next_page")
        if not nxt:
            break
        page = nxt
    _audio_index[chapter] = urls
    return urls


class Audio:
    def __init__(self, rec_id, cache):
        self.rec_id, self.cache = rec_id, pathlib.Path(cache)
        self._dur = {}

    def path(self, chapter, ayah):
        url = chapter_audio_urls(self.rec_id, chapter)[ayah]
        ext = os.path.splitext(url.split("?")[0])[1] or ".mp3"
        return download(url, self.cache / f"{chapter:03d}{ayah:03d}{ext}")

    def dur(self, chapter, ayah):
        k = (chapter, ayah)
        if k not in self._dur:
            self._dur[k] = duration(self.path(chapter, ayah))
        return self._dur[k]


def plan_clip(state, chapters, audio):
    """Return (clip, new_state). A clip never crosses a surah boundary."""
    cmin = CONFIG["clip"]["min_seconds"]
    cmax = CONFIG["clip"]["max_seconds"]
    s, a = state["next_surah"], state["next_ayah"]
    ch = chapters[s]
    n = ch["verses_count"]
    parts, total = [], 0.0
    with_basmala = (a == 1 and ch.get("bismillah_pre") and CONFIG["clip"]["prepend_basmala"])
    if with_basmala:
        parts.append(audio.path(1, 1))
        total += audio.dur(1, 1)
    first, last = a, a - 1
    while a <= n:
        d = audio.dur(s, a)
        if last >= first and (total >= cmin or total + d > cmax):
            break
        parts.append(audio.path(s, a))
        total += d
        last = a
        a += 1
    # Avoid leaving a tiny tail at the end of a surah: fold up to 3 short ayat in.
    remaining = list(range(last + 1, n + 1))
    if 0 < len(remaining) <= 3:
        rest = sum(audio.dur(s, x) for x in remaining)
        if rest < 15 and total + rest <= cmax + 15:
            for x in remaining:
                parts.append(audio.path(s, x))
            total += rest
            last = n
    new = dict(state)
    if last >= n:
        if s == 114:
            new["next_surah"], new["next_ayah"] = 1, 1
            new["khatmas_completed"] = state.get("khatmas_completed", 0) + 1
        else:
            new["next_surah"], new["next_ayah"] = s + 1, 1
    else:
        new["next_surah"], new["next_ayah"] = s, last + 1
    clip = {"surah_number": s, "surah_name": ch["name_arabic"], "from_ayah": first,
            "to_ayah": last, "with_basmala": bool(with_basmala),
            "audio_seconds": round(total, 2), "parts": [str(p) for p in parts]}
    return clip, new


# --------------------------------------------------------------- backgrounds

def build_library(lib_dir):
    key = os.environ.get("PIXABAY_API_KEY")
    if not key:
        raise SystemExit("PIXABAY_API_KEY secret is missing")
    bg = CONFIG["backgrounds"]
    blocked = set(bg["blocked_tags"])
    target = bg["library_size"]
    per_query = max(2, -(-target // len(bg["queries"])))
    lib_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = lib_dir / "_raw"
    items, seen = [], set()
    for q in bg["queries"]:
        data = get_json(PIXABAY_API, {"key": key, "q": q, "video_type": "film",
                                      "safesearch": "true", "order": "popular",
                                      "per_page": 30})
        picked = 0
        for hit in data.get("hits", []):
            tags = {t.strip().lower() for t in hit.get("tags", "").split(",")}
            words = {w for t in tags for w in t.split()}
            if hit["id"] in seen or (tags | words) & blocked:
                continue
            if hit.get("duration", 0) < bg["min_duration"]:
                continue
            vids = hit["videos"]
            choice = None
            for size in ("medium", "large", "small"):
                v = vids.get(size) or {}
                if v.get("url") and v.get("height", 0) >= 720:
                    choice = v
                    break
            if not choice:
                continue
            seen.add(hit["id"])
            raw = download(choice["url"], raw_dir / f"{hit['id']}.mp4")
            out = lib_dir / f"bg_{hit['id']}.mp4"
            # Standardise: vertical 1080x1920, 30 fps, no audio, max 30 s.
            run(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-t", "30", "-an",
                 "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                        f"crop={W}:{H},fps=30,setsar=1",
                 "-c:v", "libx264", "-preset", "medium", "-crf", "22",
                 "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)])
            raw.unlink(missing_ok=True)
            items.append({"file": out.name, "pixabay_id": hit["id"],
                          "page": hit.get("pageURL"), "user": hit.get("user"),
                          "query": q, "tags": hit.get("tags")})
            log(f"  background {len(items)}: {q} -> {hit.get('pageURL')}")
            picked += 1
            if picked >= per_query or len(items) >= target:
                break
        if len(items) >= target:
            break
        time.sleep(1)
    if not items:
        raise SystemExit("No background videos found on Pixabay")
    random.Random(42).shuffle(items)
    (lib_dir / "library.json").write_text(json.dumps(items, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    (lib_dir / ".new").write_text("1")
    try:
        raw_dir.rmdir()
    except OSError:
        pass
    return items


def load_library(lib_dir):
    meta = lib_dir / "library.json"
    if meta.exists():
        items = json.loads(meta.read_text(encoding="utf-8"))
        if all((lib_dir / it["file"]).exists() for it in items):
            log(f"Using existing background library ({len(items)} videos)")
            return items
    log("Building background library from Pixabay ...")
    return build_library(lib_dir)


# ------------------------------------------------------------------ graphics

def _font(path, size):
    if features.check("raqm"):
        return ImageFont.truetype(path, size, layout_engine=ImageFont.Layout.RAQM)
    return ImageFont.truetype(path, size)


def _shape(text):
    if features.check("raqm"):
        return text
    import arabic_reshaper
    from bidi.algorithm import get_display
    return get_display(arabic_reshaper.reshape(text))


def _draw_centered(layer, text, font, y, fill):
    d = ImageDraw.Draw(layer)
    t = _shape(text)
    kw = {"direction": "rtl"} if features.check("raqm") else {}
    box = d.textbbox((0, 0), t, font=font, **kw)
    x = (W - (box[2] - box[0])) // 2 - box[0]
    d.text((x, y), t, font=font, fill=fill, **kw)


def make_overlay(clip, path):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    # Soft dark bands behind the text so it stays readable on bright scenes.
    shade = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shade)
    for y in range(0, 620):
        a = int(150 * (1 - y / 620) ** 1.4)
        sd.line([(0, y), (W, y)], fill=(0, 0, 0, a))
    for y in range(1180, 1700):
        a = int(110 * (1 - abs(y - 1440) / 260) ** 1.2) if abs(y - 1440) < 260 else 0
        sd.line([(0, y), (W, y)], fill=(0, 0, 0, a))
    img = Image.alpha_composite(img, shade)

    rc = CONFIG["reciter"]
    rng = ayah_range_text(clip)
    texts = [
        (f"سورة {clip['surah_name']}", _font(FONT_BOLD, 120), 230, (255, 255, 255, 255)),
        (f"بصوت {rc['name_ar']}", _font(FONT_REG, 50), 420, (235, 235, 235, 255)),
        (rng, _font(FONT_BOLD, 72), 1380, (255, 255, 255, 255)),
    ]
    handle = CONFIG.get("overlay", {}).get("handle")
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sharp = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for t, f, y, col in texts:
        _draw_centered(glow, t, f, y, (0, 0, 0, 200))
        _draw_centered(sharp, t, f, y, col)
    if handle:
        f = ImageFont.truetype(FONT_REG, 40)
        d = ImageDraw.Draw(sharp)
        box = d.textbbox((0, 0), handle, font=f)
        d.text(((W - box[2]) // 2, 1500), handle, font=f, fill=(255, 255, 255, 190))
    glow = glow.filter(ImageFilter.GaussianBlur(8))
    img = Image.alpha_composite(img, glow)
    img = Image.alpha_composite(img, sharp)
    img.save(path)


def ayah_range_text(clip):
    a, b = clip["from_ayah"], clip["to_ayah"]
    if a == b:
        return f"الآية {str(a).translate(AR_DIGITS)}"
    return f"الآيات {str(a).translate(AR_DIGITS)} - {str(b).translate(AR_DIGITS)}"


def render(clip, bg_path, overlay_path, out_path, work):
    lst = work / "concat.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in clip["parts"]), encoding="utf-8")
    wav = work / "audio.wav"
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-ar", "48000", "-ac", "2", str(wav)])
    a_dur = duration(wav)
    total = round(a_dur + 1.5, 2)          # 0.5 s lead-in, 1 s tail
    fo = max(total - 1.0, 0)
    vf = (f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
          f"setsar=1,fps=30,eq=brightness=-0.05:saturation=1.05[bg];"
          f"[bg][2:v]overlay=0:0:format=auto,"
          f"fade=t=in:st=0:d=0.6,fade=t=out:st={fo}:d=1.0,format=yuv420p[v];"
          f"[1:a]adelay=500|500,loudnorm=I=-14:TP=-1.5:LRA=11,"
          f"apad=whole_dur={total},afade=t=in:st=0:d=0.4,"
          f"afade=t=out:st={max(total - 0.8, 0)}:d=0.8[a]")
    run(["ffmpeg", "-y", "-v", "error",
         "-stream_loop", "-1", "-i", str(bg_path),
         "-i", str(wav),
         "-loop", "1", "-i", str(overlay_path),
         "-filter_complex", vf, "-map", "[v]", "-map", "[a]", "-t", str(total),
         "-c:v", "libx264", "-preset", "medium", "-crf", "21", "-r", "30",
         "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
         "-movflags", "+faststart", str(out_path)])
    return total


# -------------------------------------------------------------------- caption

def caption(clip):
    c = CONFIG["caption"]
    rc = CONFIG["reciter"]
    surah_tag = c["surah_hashtag_prefix"] + clip["surah_name"].replace(" ", "_")
    tags = " ".join(c["hashtags"] + [surah_tag])
    return c["template"].format(surah=clip["surah_name"], range=ayah_range_text(clip),
                                reciter=rc["name_ar"], style=rc["style_ar"], hashtags=tags)


def slots(start_date, count):
    sc = CONFIG["schedule"]
    tz = ZoneInfo(sc["timezone"])
    times = sc["times"][: sc["posts_per_day"]]
    out = []
    for i in range(count):
        day = start_date + dt.timedelta(days=i // len(times))
        hh, mm = map(int, times[i % len(times)].split(":"))
        out.append(dt.datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz).isoformat())
    return out


# ----------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=21)
    ap.add_argument("--start-date", required=True, help="YYYY-MM-DD of the first post")
    ap.add_argument("--tag", required=True, help="GitHub release tag for this batch")
    ap.add_argument("--out", default="out")
    ap.add_argument("--library", default="library")
    ap.add_argument("--work", default="work")
    args = ap.parse_args()

    out, work, lib = (pathlib.Path(p) for p in (args.out, args.work, args.library))
    out.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    log("Start position:", state)

    rec_id = find_recitation_id()
    chapters = load_chapters()
    audio = Audio(rec_id, work / "audio")
    library = load_library(lib)
    repo = os.environ.get("GITHUB_REPOSITORY", "OWNER/REPO")
    start = dt.date.fromisoformat(args.start_date)
    times = slots(start, args.count)

    entries = []
    for i in range(args.count):
        clip, state = plan_clip(state, chapters, audio)
        bg = library[state["next_background"] % len(library)]
        state["next_background"] = state["next_background"] + 1
        name = (f"{clip['surah_number']:03d}_{clip['from_ayah']:03d}-"
                f"{clip['to_ayah']:03d}.mp4")
        ov = work / "overlay.png"
        make_overlay(clip, ov)
        secs = render(clip, lib / bg["file"], ov, out / name, work)
        state["clips_made"] = state.get("clips_made", 0) + 1
        entry = {
            "index": i,
            "file": name,
            "url": f"https://github.com/{repo}/releases/download/{args.tag}/{name}",
            "surah_number": clip["surah_number"],
            "surah_name": clip["surah_name"],
            "from_ayah": clip["from_ayah"],
            "to_ayah": clip["to_ayah"],
            "with_basmala": clip["with_basmala"],
            "seconds": secs,
            "publish_at": times[i],
            "caption": caption(clip),
            "background": {"source": "Pixabay", "page": bg.get("page"),
                           "user": bg.get("user")},
        }
        entries.append(entry)
        log(f"[{i + 1}/{args.count}] {name}  {secs:.1f}s  {entry['publish_at']}")

    manifest = {"tag": args.tag, "created": dt.datetime.now(dt.timezone.utc).isoformat(),
                "recitation_id": rec_id, "start_date": args.start_date,
                "posts": entries}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                          encoding="utf-8")
    log("Next position:", state)


if __name__ == "__main__":
    main()
