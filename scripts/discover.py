#!/usr/bin/env python3
"""One-off discovery for the new video design: word timings + painting candidates.
Writes everything to disc/ which the workflow uploads as release 'discovery'."""
import json, pathlib, time, requests
from PIL import Image, ImageDraw
OUT = pathlib.Path("disc"); OUT.mkdir(exist_ok=True)
S = requests.Session(); S.headers["User-Agent"] = "quran-videos-bot/1.0 (https://github.com/MarwaShwani/quran-videos)"
rep = {}
for name, url in {
    "by_ayah_seg": "https://api.quran.com/api/v4/recitations/9/by_ayah/2:105?fields=segments",
    "verses_audio": "https://api.quran.com/api/v4/verses/by_key/2:105?words=true&word_fields=text_uthmani,text_qpc_hafs&audio=9&fields=text_uthmani",
    "qdc_reciters": "https://api.qurancdn.com/api/qdc/audio/reciters?locale=en",
}.items():
    try:
        r = S.get(url, timeout=60); rep[name] = {"status": r.status_code, "body": r.text[:6000]}
    except Exception as e:
        rep[name] = {"error": str(e)}
try:
    rec = json.loads(rep["qdc_reciters"]["body"] if len(rep["qdc_reciters"].get("body",""))<6000 else S.get("https://api.qurancdn.com/api/qdc/audio/reciters?locale=en",timeout=60).text)
    mins = [x for x in rec.get("reciters", []) if "inshaw" in json.dumps(x).lower()]
    rep["minshawi_qdc"] = mins
    for m in mins:
        r = S.get(f"https://api.qurancdn.com/api/qdc/audio/reciters/{m['id']}/audio_files?chapter=2&segments=true", timeout=60)
        rep[f"qdc_{m['id']}"] = {"status": r.status_code, "body": r.text[:3000]}
except Exception as e:
    rep["qdc_err"] = str(e)
(OUT / "report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1))

API = "https://commons.wikimedia.org/w/api.php"
queries = ["Albert Bierstadt landscape painting", "Albert Bierstadt sunset", "Albert Bierstadt lake",
           "Thomas Cole landscape", "Frederic Edwin Church landscape", "Ivan Shishkin forest",
           "Ivan Shishkin landscape", "Arkhip Kuindzhi", "Isaac Levitan landscape", "Thomas Moran landscape",
           "Asher Brown Durand landscape", "Jasper Francis Cropsey autumn", "Sanford Robinson Gifford",
           "Hudson River School painting", "Ivan Aivazovsky sea", "Thomas Hill Yosemite painting",
           "William Keith landscape painting", "George Inness landscape"]
cands, seen = [], set()
for q in queries:
    p = {"action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
         "gsrsearch": q + " filetype:bitmap", "gsrlimit": 25, "prop": "imageinfo",
         "iiprop": "url|size|extmetadata", "iiurlwidth": 400}
    try:
        d = S.get(API, params=p, timeout=60).json()
    except Exception as e:
        print("search fail", q, e); continue
    for pg in (d.get("query", {}).get("pages", {}) or {}).values():
        ii = (pg.get("imageinfo") or [{}])[0]
        if pg["title"] in seen or ii.get("width", 0) < 1400: continue
        lic = ii.get("extmetadata", {}).get("LicenseShortName", {}).get("value", "")
        if "public domain" not in lic.lower() and "pd" not in lic.lower(): continue
        seen.add(pg["title"])
        cands.append({"title": pg["title"], "url": ii["url"], "thumb": ii.get("thumburl"),
                      "w": ii["width"], "h": ii["height"], "license": lic, "query": q,
                      "artist": ii.get("extmetadata", {}).get("Artist", {}).get("value", "")[:200]})
    time.sleep(1)
print("candidates", len(cands))
(OUT / "candidates.json").write_text(json.dumps(cands, ensure_ascii=False, indent=1))
# contact sheets, 4x5 numbered
thumbs = []
for i, c in enumerate(cands):
    try:
        r = S.get(c["thumb"], timeout=60); r.raise_for_status()
        p = OUT / f"t{i:03d}.jpg"; p.write_bytes(r.content); thumbs.append((i, p))
    except Exception as e:
        print("thumb fail", i, e)
    time.sleep(0.2)
per = 20
for s in range(0, len(thumbs), per):
    sheet = Image.new("RGB", (4 * 300, 5 * 320), "white"); d = ImageDraw.Draw(sheet)
    for k, (i, p) in enumerate(thumbs[s:s + per]):
        im = Image.open(p).convert("RGB"); im.thumbnail((290, 290))
        x, y = (k % 4) * 300 + 5, (k // 4) * 320 + 5
        sheet.paste(im, (x, y)); d.rectangle([x, y, x + 46, y + 26], fill="black"); d.text((x + 6, y + 6), f"{i}", fill="yellow")
    sheet.save(OUT / f"sheet_{s // per:02d}.jpg", quality=80)
for p in OUT.glob("t*.jpg"): p.unlink()
