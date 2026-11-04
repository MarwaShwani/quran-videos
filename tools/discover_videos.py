#!/usr/bin/env python3
"""One-off: search Pixabay for painterly / animated-painting nature videos and
make numbered contact sheets so the backgrounds can be picked by eye."""
import json, os, pathlib, time, requests
from PIL import Image, ImageDraw
OUT = pathlib.Path("disc"); OUT.mkdir(exist_ok=True)
KEY = os.environ["PIXABAY_API_KEY"]
S = requests.Session()
blocked = set(json.loads(pathlib.Path("config.json").read_text())["backgrounds"]["blocked_tags"])
queries = ["oil painting landscape", "painting animation nature", "animated painting", "painted landscape",
           "watercolor landscape", "anime landscape", "anime nature", "fantasy landscape", "cartoon nature",
           "illustration nature", "art landscape animation", "dreamy landscape", "cinemagraph nature",
           "aesthetic nature", "lofi landscape", "ghibli", "impressionism", "autumn painting", "sunset painting",
           "lake painting", "forest illustration", "mountains illustration", "deer forest animation",
           "nature animation loop", "2d animation landscape"]
cands, seen = [], set()
for q in queries:
    try:
        d = S.get("https://pixabay.com/api/videos/", params={"key": KEY, "q": q, "safesearch": "true",
                  "order": "popular", "per_page": 40}, timeout=60).json()
    except Exception as e:
        print("fail", q, e); continue
    for h in d.get("hits", []):
        tags = {t.strip().lower() for t in h.get("tags", "").split(",")}
        words = {w for t in tags for w in t.split()}
        if h["id"] in seen or (tags | words) & blocked or h.get("duration", 0) < 8:
            continue
        v = h["videos"].get("large") or h["videos"].get("medium") or {}
        if v.get("height", 0) < 720:
            continue
        seen.add(h["id"])
        thumb = (h["videos"].get("medium") or {}).get("thumbnail") or (h["videos"].get("small") or {}).get("thumbnail")
        cands.append({"id": h["id"], "q": q, "tags": h.get("tags"), "page": h.get("pageURL"),
                      "type": h.get("type"), "dur": h.get("duration"), "thumb": thumb,
                      "w": v.get("width"), "h": v.get("height")})
    time.sleep(1)
print("candidates", len(cands))
(OUT / "video_candidates.json").write_text(json.dumps(cands, ensure_ascii=False, indent=1))
thumbs = []
for i, c in enumerate(cands):
    try:
        r = S.get(c["thumb"], timeout=60); r.raise_for_status()
        p = OUT / f"t{i:03d}.jpg"; p.write_bytes(r.content); thumbs.append((i, p))
    except Exception as e:
        print("thumb fail", i, e)
per = 20
for s in range(0, len(thumbs), per):
    sheet = Image.new("RGB", (4 * 300, 5 * 200), "white"); d = ImageDraw.Draw(sheet)
    for k, (i, p) in enumerate(thumbs[s:s + per]):
        im = Image.open(p).convert("RGB"); im.thumbnail((290, 190))
        x, y = (k % 4) * 300 + 5, (k // 4) * 200 + 5
        sheet.paste(im, (x, y)); d.rectangle([x, y, x + 40, y + 22], fill="black"); d.text((x + 5, y + 5), f"{i}", fill="yellow")
    sheet.save(OUT / f"vsheet_{s // per:02d}.jpg", quality=80)
for p in OUT.glob("t*.jpg"): p.unlink()
