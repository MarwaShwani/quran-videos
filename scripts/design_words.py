"""'Painting + words' design.

A public-domain landscape painting fills the vertical frame with a slow zoom,
and the Quran text appears word by word, in sync with the recitation, as a
short vertical stack (newest word at the bottom, oldest fading out at the top),
like the reference reel the account owner chose.

Word timings come from quran.com (recitations/{id}/by_ayah/{key}?fields=segments),
which are measured on exactly the per-ayah audio files the clips are made of.
"""
import json
import math
import pathlib
import subprocess
import time

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 1080, 1920
FPS = 30
AR_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")

ROOT = pathlib.Path(__file__).resolve().parent.parent
FONT_QURAN = str(ROOT / "fonts" / "AmiriQuran-Regular.ttf")
FONT_REG = str(ROOT / "fonts" / "Amiri-Regular.ttf")
FONT_BOLD = str(ROOT / "fonts" / "Amiri-Bold.ttf")

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
QURAN_API = "https://api.quran.com/api/v4"

# Layout (pixels in the 1080x1920 frame)
WORD_SIZE = 124          # Quran word font size
GAP = 235                # distance between stacked words
Y_NEWEST = 1130          # centre of the newest word; older ones sit above it
VISIBLE = 3              # words visible at once
ANIM = 0.35              # seconds for a word to slide/fade in
MAX_WORD_W = 940
ALPHA = [1.0, 0.82, 0.6, 0.0]   # steady opacity by age: newest, older, oldest, gone


def _font(path, size):
    return ImageFont.truetype(path, size, layout_engine=ImageFont.Layout.RAQM)


# ------------------------------------------------------------------- data

def ayah_words(get_json, s, a):
    d = get_json(f"{QURAN_API}/verses/by_key/{s}:{a}",
                 {"words": "true", "word_fields": "text_uthmani"})
    return [w["text_uthmani"] for w in d["verse"]["words"] if w["char_type_name"] == "word"]


def ayah_segments(get_json, rec_id, s, a):
    d = get_json(f"{QURAN_API}/recitations/{rec_id}/by_ayah/{s}:{a}", {"fields": "segments"})
    files = d.get("audio_files") or []
    return (files[0].get("segments") or []) if files else []


def word_times(words, segs, dur):
    """[(start_s, end_s)] per word, relative to the ayah file.
    Uses measured segments; any word without one is spread by letter count
    over the gap it falls in."""
    n = len(words)
    t = [None] * n
    for seg in segs:
        if len(seg) >= 4 and seg[1] is not None and 1 <= int(seg[1]) <= n:
            p = int(seg[1]) - 1
            if t[p] is None:
                t[p] = (seg[2] / 1000.0, seg[3] / 1000.0)
    if all(x is None for x in t):
        lead, tail = 0.3, 0.4
        weights = [max(1, len(w)) for w in words]
        tot = sum(weights)
        span = max(dur - lead - tail, 0.5)
        cur, out = lead, []
        for wt in weights:
            d = span * wt / tot
            out.append((cur, cur + d))
            cur += d
        return out
    # fill holes between known neighbours
    i = 0
    while i < n:
        if t[i] is not None:
            i += 1
            continue
        j = i
        while j < n and t[j] is None:
            j += 1
        a = t[i - 1][1] if i > 0 else 0.2
        b = t[j][0] if j < n else max(dur - 0.3, a + 0.3 * (j - i))
        step = (b - a) / (j - i)
        for k in range(i, j):
            t[k] = (a + step * (k - i), a + step * (k - i + 1))
        i = j
    return t


def build_items(clip, keys, durations, get_json, rec_id):
    """Timeline of text items for the whole clip: [{'text','t','kind'}],
    times in seconds from the start of the recitation audio."""
    items, offset = [], 0.0
    for (s, a, is_basmala), dur in zip(keys, durations):
        words = ayah_words(get_json, s, a)
        segs = ayah_segments(get_json, rec_id, s, a)
        times = word_times(words, segs, dur)
        for w, (st, en) in zip(words, times):
            items.append({"text": w, "t": offset + st, "kind": "word"})
        if not is_basmala:
            end = offset + (times[-1][1] if times else dur - 0.3)
            items.append({"text": "۝" + str(a).translate(AR_DIGITS), "t": end,
                          "kind": "marker"})
        offset += dur
    # keep strictly increasing start times
    for i in range(1, len(items)):
        if items[i]["t"] <= items[i - 1]["t"]:
            items[i]["t"] = items[i - 1]["t"] + 0.05
    return items


# -------------------------------------------------------------- paintings

def load_paintings(lib_dir, session, log):
    """Download the curated paintings (once) into lib_dir; return list of dicts
    with a local 'file'."""
    spec = json.loads((ROOT / "paintings.json").read_text(encoding="utf-8"))["paintings"]
    lib_dir.mkdir(parents=True, exist_ok=True)
    meta_path = lib_dir / "paintings_lib.json"
    have = {}
    if meta_path.exists():
        have = {x["title"]: x for x in json.loads(meta_path.read_text(encoding="utf-8"))}
    out, changed = [], False
    for i, p in enumerate(spec):
        cached = have.get(p["title"])
        if cached and (lib_dir / cached["file"]).exists():
            out.append({**cached, "x": p.get("x", 0.5)})
            continue
        want_h = 2200
        width = math.ceil(want_h * p["w"] / p["h"]) if p["h"] > want_h else p["w"]
        q = {"action": "query", "format": "json", "titles": p["title"], "prop": "imageinfo",
             "iiprop": "url|extmetadata", "iiurlwidth": width}
        info = None
        for k in range(4):
            try:
                d = session.get(COMMONS_API, params=q, timeout=60).json()
                pg = next(iter(d["query"]["pages"].values()))
                info = pg["imageinfo"][0]
                break
            except Exception as e:  # noqa
                log(f"  commons retry {p['title']}: {e}")
                time.sleep(3 * (k + 1))
        if not info:
            log(f"  skip painting {p['title']}")
            continue
        url = info.get("thumburl") or info["url"]
        name = f"p_{abs(hash(p['title'])) % 10**8:08d}.jpg"
        raw = lib_dir / (name + ".part")
        for k in range(4):
            try:
                r = session.get(url, timeout=180)
                r.raise_for_status()
                raw.write_bytes(r.content)
                break
            except Exception as e:  # noqa
                log(f"  download retry {url}: {e}")
                time.sleep(5 * (k + 1))
        im = Image.open(raw).convert("RGB")
        if im.height > want_h:
            im = im.resize((round(im.width * want_h / im.height), want_h), Image.LANCZOS)
        im.save(lib_dir / name, quality=92)
        raw.unlink(missing_ok=True)
        em = info.get("extmetadata", {})
        entry = {"title": p["title"], "file": name, "x": p.get("x", 0.5),
                 "page": "https://commons.wikimedia.org/wiki/" + p["title"].replace(" ", "_"),
                 "artist": em.get("Artist", {}).get("value", ""),
                 "license": em.get("LicenseShortName", {}).get("value", "")}
        out.append(entry)
        changed = True
        log(f"  painting {len(out)}: {p['title']}")
        time.sleep(1)
    if changed or not meta_path.exists():
        meta_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        (lib_dir / ".new").write_text("1")
    return out


def crop_vertical(img, x_center, scale):
    """Crop a 9:16 window (centred at x_center) and resize to (W*scale, H*scale)."""
    tw, th = round(W * scale), round(H * scale)
    r = max(tw / img.width, th / img.height)
    big = img.resize((max(tw, round(img.width * r)), max(th, round(img.height * r))), Image.LANCZOS)
    cx = int(x_center * big.width)
    left = min(max(cx - tw // 2, 0), big.width - tw)
    top = (big.height - th) // 2
    return big.crop((left, top, left + tw, top + th))


def contact_sheet(paintings, lib_dir, path):
    cols, tw, th = 8, 216, 384
    rows = math.ceil(len(paintings) / cols)
    sheet = Image.new("RGB", (cols * tw, rows * (th + 30)), "white")
    d = ImageDraw.Draw(sheet)
    for i, p in enumerate(paintings):
        im = crop_vertical(Image.open(lib_dir / p["file"]).convert("RGB"), p["x"], 0.2)
        x, y = (i % cols) * tw, (i // cols) * (th + 30)
        sheet.paste(im.resize((tw - 6, th - 6)), (x + 3, y + 3))
        d.text((x + 6, y + th + 4), f"{i}", fill="black")
    sheet.save(path, quality=85)


# -------------------------------------------------------------- graphics

def _sprite(text, font, glow=10, glow_alpha=190, direction="rtl"):
    """White text with a soft dark glow, as a tight RGBA sprite."""
    d0 = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    box = d0.textbbox((0, 0), text, font=font, direction=direction)
    pad = glow * 3
    w, h = box[2] - box[0] + 2 * pad, box[3] - box[1] + 2 * pad
    sharp = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ds = ImageDraw.Draw(sharp)
    ds.text((pad - box[0], pad - box[1]), text, font=font, fill=(255, 255, 255, 255),
            direction=direction)
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    a = sharp.getchannel("A").point(lambda v: v * glow_alpha // 255)
    shadow.putalpha(a)
    shadow = shadow.filter(ImageFilter.GaussianBlur(glow))
    out = Image.alpha_composite(shadow, shadow)          # stronger glow
    out = Image.alpha_composite(out, sharp)
    return out


def word_sprite(text, kind):
    size = WORD_SIZE if kind == "word" else int(WORD_SIZE * 0.95)
    spr = _sprite(text, _font(FONT_QURAN, size))
    if spr.width > MAX_WORD_W + 60:
        f = (MAX_WORD_W + 60) / spr.width
        spr = spr.resize((round(spr.width * f), round(spr.height * f)), Image.LANCZOS)
    return spr


def _ease(p):
    p = min(max(p, 0.0), 1.0)
    return 1 - (1 - p) ** 3


def _with_alpha(spr, a):
    if a >= 0.999:
        return spr
    s = spr.copy()
    s.putalpha(spr.getchannel("A").point(lambda v: int(v * a)))
    return s


def render(clip, painting, lib_dir, items, wav, a_dur, out_path, handle, label, log):
    total = round(a_dur + 1.5, 2)          # 0.5 s lead-in, 1 s tail (audio delayed 0.5 s)
    lead = 0.5
    nframes = int(math.ceil(total * FPS))

    src = Image.open(lib_dir / painting["file"]).convert("RGB")
    ZOOM = 1.07
    base = crop_vertical(src, painting.get("x", 0.5), ZOOM)
    # gentle darkening for legibility, a bit more behind the words
    dark = Image.new("L", base.size, 0)
    dd = ImageDraw.Draw(dark)
    bh = base.height
    for y in range(bh):
        yy = y / bh
        band = math.exp(-((yy - 0.47) / 0.20) ** 2)       # around the word stack
        dd.line([(0, y), (base.width, y)], fill=int(255 * (0.06 + 0.20 * band)))
    base = Image.composite(Image.new("RGB", base.size, (0, 0, 0)), base, dark)

    sprites = [word_sprite(it["text"], it["kind"]) for it in items]
    label_spr = _sprite(label, _font(FONT_BOLD, 56), glow=8) if label else None
    handle_spr = _sprite(handle, _font(FONT_REG, 38), glow=6, direction="ltr") if handle else None
    if handle_spr:
        handle_spr = _with_alpha(handle_spr, 0.75)
    starts = [it["t"] + lead for it in items]

    cmd = ["ffmpeg", "-y", "-v", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", str(wav),
           "-filter_complex",
           f"[0:v]fade=t=in:st=0:d=0.6,fade=t=out:st={max(total - 1.0, 0)}:d=1.0,format=yuv420p[v];"
           f"[1:a]adelay=500|500,loudnorm=I=-14:TP=-1.5:LRA=11,apad=whole_dur={total},"
           f"afade=t=in:st=0:d=0.4,afade=t=out:st={max(total - 0.8, 0)}:d=0.8[a]",
           "-map", "[v]", "-map", "[a]", "-t", str(total),
           "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-r", str(FPS),
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart",
           str(out_path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    k = -1
    t0 = time.time()
    for f in range(nframes):
        t = f / FPS
        # background: slow zoom-in with a slight upward drift
        prog = t / total
        sc = ZOOM - (ZOOM - 1.0) * prog
        bw, bh2 = W * sc, H * sc
        cx = base.width / 2
        cy = base.height / 2 - (base.height - H) * 0.25 * prog
        box = (cx - bw / 2, cy - bh2 / 2, cx + bw / 2, cy + bh2 / 2)
        frame = base.transform((W, H), Image.EXTENT, box, Image.BILINEAR)

        while k + 1 < len(starts) and starts[k + 1] <= t:
            k += 1
        if k >= 0:
            p = _ease((t - starts[k]) / ANIM)
            for j in range(max(0, k - VISIBLE), k + 1):
                rel = k - j
                y = Y_NEWEST - rel * GAP + (1 - p) * GAP * (1 if rel > 0 else 0.35)
                a = ALPHA[rel - 1] + (ALPHA[rel] - ALPHA[rel - 1]) * p if rel > 0 else p
                if a <= 0.01:
                    continue
                spr = _with_alpha(sprites[j], a)
                frame.paste(spr, (int(W / 2 - spr.width / 2), int(y - spr.height / 2)), spr)
        if label_spr:
            frame.paste(label_spr, (int(W / 2 - label_spr.width / 2), 210), label_spr)
        if handle_spr:
            frame.paste(handle_spr, (int(W / 2 - handle_spr.width / 2), 1690), handle_spr)
        proc.stdin.write(frame.tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise SystemExit("ffmpeg failed while encoding")
    log(f"  rendered {nframes} frames in {time.time() - t0:.0f}s")
    return total
