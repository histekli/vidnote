#!/usr/bin/env python3
"""
vidnote — bir videoyu, yapay zekânın (ve senin) okuyabileceği bir bağlam paketine çevirir.

  video  ->  açıklama + zaman damgalı konuşma + ekrandaki yazılar (OCR) + anahtar kareler
         ->  transkript.md (derli toplu döküm), sheets/ (kareler), pack.md / pack.json (ayrıntı)

Kullanım:
  python3 vidnote.py video.mp4
  python3 vidnote.py "https://www.youtube.com/watch?v=..." --lang tr
  python3 vidnote.py video.mp4 --max-frames 40 --whisper-model small

Gerekenler: ffmpeg, numpy, Pillow. İsteğe bağlı: tesseract (ekran yazısı),
faster-whisper veya sherpa-onnx (konuşma), yt-dlp (URL).
"""
import argparse
import datetime
import difflib
import json
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unicodedata
import wave
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ---- kare seçimi ayarları -------------------------------------------------
AW = AH = 192         # analiz karesi boyutu (piksel)
GRID = 24             # 24x24 blok (8x8 piksel)
BLOCK_THR = 14.0      # bir bloğun "değişti" sayılması için ortalama fark (0-255)
LOCAL_THR = 0.008     # blokların >=%0,8'i değiştiyse: küçük/yerel değişim (yazı, simge)
GLOBAL_THR = 0.12     # >=%12: belirgin değişim
CUT_THR = 0.60        # ardışık iki kare arasında >=%60: sahne kesmesi
SETTLE_THR = 0.006    # ardışık kare farkı bunun altındaysa görüntü "durulmuş"
HOLD = {"scene": 0.25, "change": 0.25, "local": 0.5}  # durulma süresi (sn)
MAX_WAIT = 1.5        # durulmayan (sürekli hareketli) görüntüde bekleme sınırı (sn)
WINDOW = 40           # A-B-A dönüşlerini yakalamak için bakılan son kare sayısı
SCENE_WAIT = 0.5      # kesmeden sonra görüntü durulmasa da en geç bu kadar bekle (sn)
STILL_STEP = 0.004    # durulmuş sayılmak için ardışık karede kıpırdayan blok oranı üst sınırı
SHOT_THR = 0.30       # hareketli çekimde "aynı plan" eşiği (3x3 renk dağılımı farkı)
RECHECK = 0.5         # hareketli çekimde yeniden bakma aralığı (sn)
PRIORITY = {"start": 10.0, "scene": 3.0, "text": 2.0, "change": 1.0, "local": 0.5}
LABEL = {"start": "açılış", "scene": "yeni sahne", "change": "görüntü değişti", "local": "ekrana yeni öğe geldi",
         "text": "ekrana yeni yazı geldi"}
FRAME_SCALE = "scale='if(gt(iw,ih),min(iw,{0}),-2)':'if(gt(iw,ih),-2,min(ih,{0}))'"


class VidnoteError(Exception):
    """Kullanıcıya gösterilecek, beklenen hata (dosya yok, indirilemedi...)."""


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def mmss(t):
    t = int(round(t))
    return f"{t // 3600}:{t % 3600 // 60:02d}:{t % 60:02d}" if t >= 3600 else f"{t // 60:02d}:{t % 60:02d}"


# ---- girdi ----------------------------------------------------------------
def fetch_url(url, workdir, cookies_from_browser=None, cookies_file=None, max_comments=0):
    """URL'yi yt-dlp ile indirir (en fazla 720p). (yol, üst veri) döndürür.
    Üst veri: başlık, açıklama, hesap, yayın tarihi, sayfa adresi."""
    try:
        import yt_dlp
    except ImportError:
        raise VidnoteError("Link için yt-dlp gerekli:  pip install yt-dlp")
    opts = {
        "outtmpl": str(Path(workdir) / "source.%(ext)s"),
        "format": "bv*[height<=720]+ba/b[height<=720]/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }
    if max_comments > 0:                       # yorumlar isteğe bağlı; bazı sitelerde yavaştır
        opts["getcomments"] = True
        opts["extractor_args"] = {"youtube": {"max_comments": [str(max_comments), "all", "20", "5"]}}
    if cookies_from_browser:
        opts["cookiesfrombrowser"] = (cookies_from_browser,)
    if cookies_file:
        if not Path(cookies_file).expanduser().exists():
            raise VidnoteError(f"Çerez dosyası bulunamadı: {cookies_file}")
        opts["cookiefile"] = str(Path(cookies_file).expanduser())
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)
    except Exception as e:
        msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
        raise VidnoteError(f"Video indirilemedi: {msg}\nGiriş isteyen sitelerde (ör. Instagram) ayarlara çerez dosyası (COOKIES_FILE) "
                           "ekleyin ya da videoyu indirip dosya olarak verin.")
    p = Path(path)
    if not p.exists():
        cands = list(Path(workdir).glob("source.*"))
        if not cands:
            raise VidnoteError("Video indirildi ama dosya bulunamadı.")
        p = cands[0]
    date = info.get("upload_date") or ""
    meta = {
        "title": info.get("title") or url,
        "description": info.get("description") or "",
        "uploader": info.get("uploader") or info.get("channel") or info.get("uploader_id") or "",
        "upload_date": f"{date[:4]}-{date[4:6]}-{date[6:8]}" if len(date) == 8 else "",
        "webpage_url": info.get("webpage_url") or url,
        "site": info.get("extractor_key") or "",
        "stats": {k: v for k, v in (("likes", info.get("like_count")), ("comments", info.get("comment_count")),
                                    ("plays", info.get("view_count"))) if isinstance(v, (int, float))},
    }
    return str(p), meta, comments_from_ytdlp(info, max_comments) if max_comments > 0 else []


VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".3gp"}


def open_package(zip_path, workdir):
    """VidNote paketini açar: içinde bir video ve paket.json (açıklama, hesap, yorumlar) bulunur.
    Tarayıcı eklentisi bu paketi üretir. (video yolu, üst veri, yorumlar) döndürür."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise VidnoteError("Bu .zip dosyası açılamadı.")
    with zf:
        names = [i for i in zf.infolist() if not i.is_dir()]
        vid = next((i for i in names if Path(i.filename).suffix.lower() in VIDEO_SUFFIXES), None)
        if vid is None:
            raise VidnoteError("Pakette video dosyası yok.")
        if vid.file_size > 4 * 1024 ** 3:
            raise VidnoteError("Paketteki video çok büyük.")
        dest = Path(workdir) / ("source" + Path(vid.filename).suffix.lower())    # dosya adını pakete bırakma
        with zf.open(vid) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out)
        info = {}
        js = next((i for i in names if Path(i.filename).name == "paket.json"), None)
        if js is not None and js.file_size < 20 * 1024 ** 2:
            try:
                info = json.loads(zf.read(js).decode("utf-8"))
            except ValueError:
                log("uyarı: paket.json okunamadı; yalnızca video işlenecek")
    stats = info.get("stats") or {}
    meta = {
        "title": (f"@{info['uploader']}" if info.get("uploader") else "") + (" · " + info["shortcode"] if info.get("shortcode") else "")
                 or Path(zip_path).stem,
        "description": info.get("caption") or "",
        "uploader": info.get("uploader") or "",
        "upload_date": (info.get("taken_at") or "")[:10],
        "webpage_url": info.get("url") or "",
        "site": info.get("source") or "",
        "stats": {k: stats[k] for k in ("likes", "comments", "plays") if isinstance(stats.get(k), (int, float))},
    }
    comments = [c for c in (info.get("comments") or []) if isinstance(c, dict) and str(c.get("text", "")).strip()]
    return str(dest), meta, comments


def comments_from_ytdlp(info, limit):
    """yt-dlp'nin getirdiği yorumları paket biçimine çevirir (yanıtlar üst yorumun altına)."""
    top, by_id = [], {}
    for c in (info.get("comments") or [])[: limit * 3]:
        item = {"text": str(c.get("text") or "").strip(), "likes": c.get("like_count") or 0,
                "by_owner": bool(c.get("author_is_uploader")), "replies": []}
        if not item["text"]:
            continue
        by_id[c.get("id")] = item
        parent = by_id.get(c.get("parent"))
        (parent["replies"] if parent else top).append(item)
    return top[:limit]


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
        capture_output=True, text=True, check=True).stdout
    info = json.loads(out)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if v is None:
        raise VidnoteError("Dosyada video akışı yok.")
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list", []):
        rot = int(sd.get("rotation", rot) or rot)
    if abs(rot) % 180 == 90:
        w, h = h, w
    dur = float(info["format"].get("duration") or v.get("duration") or 0)
    return {"duration": dur, "width": w, "height": h,
            "has_audio": any(s["codec_type"] == "audio" for s in info["streams"])}


# ---- 1) kare seçimi -------------------------------------------------------
def changed_fraction(a, b):
    """İki analiz karesi arasında değişen blokların oranı (0..1)."""
    d = np.abs(a - b).mean(axis=2)
    blocks = d.reshape(GRID, AH // GRID, GRID, AW // GRID).mean(axis=(1, 3))
    return float((blocks > BLOCK_THR).mean())


def grid_hist(f, g=3, bins=6):
    """Kareyi 3x3 bölgeye ayırıp her bölgenin renk dağılımını çıkarır. Kamera ya da
    kişi kıpırdadığında az değişir; ekrana yeni bir görsel girdiğinde çok değişir."""
    q = (f.astype(np.int32) * bins) // 256
    idx = q[..., 0] * bins * bins + q[..., 1] * bins + q[..., 2]
    s = AH // g
    return np.stack([np.bincount(idx[i * s:(i + 1) * s, j * s:(j + 1) * s].ravel(), minlength=bins ** 3)
                     for i in range(g) for j in range(g)]) / float(s * s)


def shot_distance(h1, h2):
    """9 bölgeden en çok değişen 3.'sünün farkı: karenin kabaca üçte biri yenilendi mi?"""
    return float(np.sort(np.abs(h1 - h2).sum(axis=1) / 2)[-3])


def select_frames(path, fps, duration, max_frames):
    """Videoyu tek geçişte küçük karelerle tarar; anlamlı değişimlerde,
    görüntü durulduktan sonra kare seçer. Tekrar eden çekimleri atlar."""
    cmd = ["ffmpeg", "-v", "error", "-i", path, "-vf", f"fps={fps},scale={AW}:{AH}:flags=area",
           "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    nbytes = AW * AH * 3
    kept, revisits = [], []
    cur = 0               # ekranda şu an hangi seçilmiş karenin planı var
    since_new = False     # son dönüş notundan beri yeni bir kare seçildi mi
    next_ok_t = 0.0       # hareketli çekimde bir sonraki kontrol zamanı
    ref = prev = None
    pending = None
    stable_run = 0
    i = -1
    while True:
        buf = proc.stdout.read(nbytes)
        if len(buf) < nbytes:
            break
        i += 1
        f = np.frombuffer(buf, np.uint8).reshape(AH, AW, 3).astype(np.int16)
        t = i / fps
        if prev is None:
            motion, step = 0.0, 0.0
        else:
            motion = float(np.abs(f - prev).mean() / 255.0)
            step = changed_fraction(f, prev)
        stable_run = stable_run + 1 if (motion <= SETTLE_THR and step <= STILL_STEP) else 0
        prev = f

        if ref is None:                       # ilk kare
            kept.append({"idx": i, "t": t, "t0": t, "reason": "start", "score": 1.0, "thumb": f, "hist": grid_hist(f)})
            ref = f
            continue

        frac = changed_fraction(f, ref)
        if frac < LOCAL_THR:                  # ekrandaki durum değişmedi (ya da eski haline döndü)
            pending = None
            continue
        if pending is None:
            pending = {"first": i, "since": i, "score": frac, "cut_at": None}
        pending["score"] = max(pending["score"], frac)
        if step >= CUT_THR and pending["cut_at"] is None:
            pending["cut_at"] = i
        is_cut = pending["cut_at"] is not None
        reason = "scene" if is_cut else ("change" if pending["score"] >= GLOBAL_THR else "local")

        settled = stable_run / fps >= HOLD[reason]
        if is_cut:      # yeni sahne: hareketli olsa bile ilk karesini geciktirme
            forced = (i - pending["cut_at"]) / fps >= SCENE_WAIT and t >= next_ok_t
        else:           # sürekli hareket (elde kamera, konuşan kişi): aralıklarla bak
            forced = (i - pending["first"]) / fps >= MAX_WAIT and t >= next_ok_t
        if not (settled or forced):
            continue
        # t0: bu görüntünün ekranda başladığı an (kare biraz sonra, durulunca alınır)
        t0 = pending["cut_at"] / fps if is_cut else (pending["since"] / fps if settled else t)
        window = list(reversed(range(max(0, len(kept) - WINDOW), len(kept))))

        if settled:
            # Durağan içerik (slayt, ekran kaydı, grafik): blok bazında birebir karşılaştır
            dup = next((k for k in window if changed_fraction(f, kept[k]["thumb"]) < LOCAL_THR), None)
            h = None
        else:
            # Hareketli çekim: kıpırtıyı değil, kadraja yeni bir şey girip girmediğini ölç
            h = grid_hist(f)
            if shot_distance(h, kept[cur]["hist"]) < SHOT_THR:     # aynı plan sürüyor
                next_ok_t = t + RECHECK
                pending.update(since=i, score=frac, cut_at=None)
                continue
            dup = next((k for k in window if shot_distance(h, kept[k]["hist"]) < SHOT_THR), None)
        if dup is not None:                                   # daha önce gösterilen plana dönüş (A-B-A)
            if dup != cur and since_new:
                revisits.append({"t": t0, "at": t, "of": dup})
                since_new = False
            cur = dup
        else:
            kept.append({"idx": i, "t": t, "t0": t0, "reason": reason, "score": pending["score"],
                         "settled": bool(settled), "thumb": f, "hist": h if h is not None else grid_hist(f)})
            cur = len(kept) - 1
            since_new = True
        next_ok_t = t + RECHECK
        ref = f
        pending = None
    proc.wait()
    analyzed = i + 1

    # Bütçe: fazla kare varsa en az bilgi taşıyanları at
    while len(kept) > max_frames:
        j = min(range(1, len(kept)),
                key=lambda k: PRIORITY[kept[k]["reason"]] + kept[k]["score"] - (0 if kept[k].get("settled", True) else 0.5))
        for r in revisits:                   # dönüş kayıtlarının indekslerini düzelt
            if r["of"] == j:
                r["of"] = None
            elif r["of"] is not None and r["of"] > j:
                r["of"] -= 1
        kept.pop(j)
    revisits = [r for r in revisits if r["of"] is not None]
    return kept, revisits, analyzed


def extract_frames(path, fps, kept, outdir, full_side=1280):
    """Seçilen kareleri (OCR için) yüksek çözünürlükte dışarı alır."""
    outdir.mkdir(parents=True, exist_ok=True)
    sel = "+".join(f"eq(n\\,{k['idx']})" for k in kept)
    scale = f"scale='if(gt(iw,ih),min(iw,{full_side}),-2)':'if(gt(iw,ih),-2,min(ih,{full_side}))'"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-vf", f"fps={fps},select='{sel}',{scale}",
                    "-vsync", "0", "-q:v", "2", str(outdir / "raw_%04d.jpg")], check=True)
    files = sorted(outdir.glob("raw_*.jpg"))
    if len(files) != len(kept):
        log(f"uyarı: {len(kept)} kare beklenirken {len(files)} çıktı")
    return files


# ---- 2) ekran yazısı (OCR) ------------------------------------------------
def ocr_image(path, langs):
    """Tesseract ile karedeki yazı satırlarını (yukarıdan aşağıya) döndürür."""
    out = subprocess.run(["tesseract", str(path), "stdout", "-l", langs, "--psm", "11", "tsv"],
                         capture_output=True, text=True).stdout
    lines = {}
    for row in out.splitlines()[1:]:
        r = row.split("\t")
        if len(r) < 12:
            continue
        try:
            conf = float(r[10])
        except ValueError:
            continue
        word = r[11].strip()
        if conf < 55 or not re.search(r"\w", word):
            continue
        key = (int(r[2]), int(r[3]), int(r[4]))
        ln = lines.setdefault(key, {"top": int(r[7]), "words": []})
        ln["words"].append((int(r[6]), word))
    result = []
    for ln in sorted(lines.values(), key=lambda v: v["top"]):
        text = " ".join(w for _, w in sorted(ln["words"]))
        if len(re.findall(r"\w", text)) >= 3:
            result.append(text)
    return result


_tess_cache = {}
MIN_TEXT_H = 0.012    # kare yüksekliğinin %1,2'sinden küçük satırlar OCR'a alınmaz


def tess_line(crop, langs):
    """Tek satırlık kırpıntıyı tesseract ile okur -> (metin, ortalama güven)."""
    import cv2
    g = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    if g.shape[0] < 72:                                   # küçük yazıyı büyüt
        k = 72 / g.shape[0]
        g = cv2.resize(g, None, fx=k, fy=k, interpolation=cv2.INTER_CUBIC)
    if g.mean() < 110:                                    # koyu zemin üstü açık yazı -> ters çevir
        g = 255 - g
    g = cv2.copyMakeBorder(g, 12, 12, 12, 12, cv2.BORDER_REPLICATE)
    ok, png = cv2.imencode(".png", g)
    out = subprocess.run(["tesseract", "stdin", "stdout", "-l", langs, "--psm", "7", "tsv"],
                         input=png.tobytes(), capture_output=True).stdout.decode("utf-8", "replace")
    words, confs = [], []
    for row in out.splitlines()[1:]:
        r = row.split("\t")
        if len(r) >= 12 and re.search(r"\w", r[11]):
            words.append(r[11].strip())
            confs.append(float(r[10]))
    return " ".join(words), (sum(confs) / len(confs) if confs else 0.0)


def ocr_frame(path, langs, detector, have_tess):
    """Önce metin kutularını bulur (karmaşık arka planda da çalışır), sonra her
    satırı Türkçe destekli tesseract ile okur. Tesseract zayıf kalırsa
    dedektörün kendi okumasına (Türkçe karakter bilmez) düşer."""
    if detector is None:
        return [{"text": x, "y": None, "h": None} for x in ocr_image(path, langs)] if have_tess else []
    import cv2
    img = cv2.imread(str(path))
    res, _ = detector(img, use_cls=False)           # yazı yönü sınıflandırması gereksiz; atlamak hızlandırır
    H, W = img.shape[:2]
    # kutuları dikdörtgene çevir; aynı satırdaki komşu kutuları birleştir
    rects = []
    for box, rtxt, rconf in res or []:
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        rects.append([min(xs), min(ys), max(xs), max(ys), rtxt.strip(), float(rconf)])
    rects.sort(key=lambda r: (r[1], r[0]))
    merged = []
    for r in rects:
        m = merged[-1] if merged else None
        if m:
            h = min(m[3] - m[1], r[3] - r[1])
            overlap = min(m[3], r[3]) - max(m[1], r[1])
            a, b = (m, r) if m[0] <= r[0] else (r, m)
            if overlap >= 0.5 * h and b[0] - a[2] <= 1.2 * h:
                merged[-1] = [a[0], min(m[1], r[1]), max(m[2], r[2]), max(m[3], r[3]), a[4] + " " + b[4], min(m[5], r[5])]
                continue
        merged.append(r)
    lines = []
    for bx0, by0, bx1, by1, text, rconf in merged:
        h = by1 - by0
        x0, x1 = int(max(0, bx0 - h * 0.3)), int(min(W, bx1 + h * 0.3))
        y0, y1 = int(max(0, by0 - h * 0.2)), int(min(H, by1 + h * 0.2))
        if have_tess and y1 - y0 >= 8:
            key = (text, x0 // 16, y0 // 16, x1 // 16, y1 // 16)
            if key not in _tess_cache:
                _tess_cache[key] = tess_line(img[y0:y1, x0:x1], langs)
            ttxt, tconf = _tess_cache[key]
            n_r, n_t = len(re.findall(r"\w", text)), len(re.findall(r"\w", ttxt))
            if tconf >= 60 and n_t >= 0.7 * n_r:
                text = ttxt
        if h / H < MIN_TEXT_H:          # okunamayacak kadar küçük yazı: uydurma riski yüksek
            continue
        if rconf >= 0.5 and len(re.findall(r"\w", text)) >= 2:
            lines.append({"text": text, "y": round((by0 + by1) / 2 / H, 3), "h": round(h / H, 3),
                          "x0": bx0 / W, "x1": bx1 / W})
    return reading_order(lines)


def reading_order(lines):
    """Satırları okuma sırasına dizer. Yan yana iki blok varsa (iki kapak, iki sütun)
    önce soldakini, sonra sağdakini okur; yoksa yukarıdan aşağıya."""
    cols, spanning = [], []
    for ln in sorted(lines, key=lambda l: l["x1"] - l["x0"]):          # dar satırlar sütunları kurar
        w = ln["x1"] - ln["x0"]
        hits = [c for c in cols
                if min(c["x1"], ln["x1"]) - max(c["x0"], ln["x0"]) > 0.3 * min(w, c["x1"] - c["x0"])]
        if w >= 0.6 or len(hits) >= 2:
            spanning.append(ln)                                         # sütunları aşan satır (başlık, altyazı)
        elif hits:
            c = hits[0]
            c["lines"].append(ln)
            c["x0"], c["x1"] = min(c["x0"], ln["x0"]), max(c["x1"], ln["x1"])
        else:
            cols.append({"x0": ln["x0"], "x1": ln["x1"], "lines": [ln]})
    by_y = lambda l: l["y"]
    if sum(len(c["lines"]) >= 2 for c in cols) < 2:
        return sorted(lines, key=by_y)
    top = min(l["y"] for c in cols for l in c["lines"])
    out = sorted([l for l in spanning if l["y"] < top], key=by_y)
    for c in sorted(cols, key=lambda c: c["x0"]):
        out += sorted(c["lines"], key=by_y)
    return out + sorted([l for l in spanning if l["y"] >= top], key=by_y)


def scan_text(path, duration, interval, tmp, langs, detector, have_tess):
    """Videoyu düzenli aralıklarla tarayıp ekrandaki yazıların tamamını toplar.
    Kare seçimi görüntü değişimine bakar; bu tarama ise yazıya bakar: hareketli çekimin
    üstüne gelen küçük bir yazı ya da akıp giden altyazılar burada yakalanır."""
    step = max(interval, duration / 600)                 # çok uzun videoda en fazla ~600 örnek
    d = tmp / "scan"
    d.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-vf", f"fps=1/{step:.3f}," + FRAME_SCALE.format(960),
                    "-q:v", "3", str(d / "s_%05d.jpg")], check=True)
    items, prev_small, prev_lines = [], None, []
    for n, f in enumerate(sorted(d.glob("s_*.jpg"))):
        small = np.asarray(Image.open(f).convert("RGB").resize((AW, AH), Image.BOX)).astype(np.int16)
        if prev_small is not None and changed_fraction(small, prev_small) < LOCAL_THR / 2:
            lines = [dict(ln) for ln in prev_lines]      # görüntü aynı: yeniden okumaya gerek yok
        else:
            lines = ocr_frame(f, langs, detector, have_tess)
        prev_small, prev_lines = small, lines
        items.append({"t": n * step, "t0": n * step, "reason": "scan", "ocr": lines})
    return items, step


def text_streams(scan, kept):
    """Altyazı olmayan ekran yazıları ve altyazılar için iki ayrı akış çıkarır.
    Ekran yazılarında her satır bir kez listelenir; seçilen karelerdeki (daha yüksek
    çözünürlüklü) okuma önceliklidir, tarama yalnızca orada olmayanları ekler."""
    seen, screen = [], []
    def unseen(key):
        return len(key) >= 3 and not difflib.get_close_matches(key, seen, n=1, cutoff=0.7)
    for k in kept:
        keys = {ln["text"]: ln["key"] for ln in k.get("ocr", [])}
        lines = [x for x in k["text_new"] if unseen(keys.get(x, x))]
        seen += [ln["key"] for ln in k.get("ocr", [])]
        if lines:
            screen.append({"t": k["t0"], "lines": lines})
    for it in scan:
        keys = {ln["text"]: ln["key"] for ln in it["ocr"]}
        lines = []
        for x in it["text_new"]:
            if unseen(keys.get(x, x)):
                lines.append(x)
                seen.append(keys.get(x, x))
        if lines:
            screen.append({"t": it["t"], "lines": lines})
    screen.sort(key=lambda x: x["t"])
    captions, prev = [], []
    for it in scan:
        keys = {ln["text"]: ln["key"] for ln in it["ocr"]}
        fresh = [c for c in it["text_caption"] if not difflib.get_close_matches(keys.get(c, c), prev, n=1, cutoff=0.8)]
        if fresh:
            captions.append({"t": it["t"], "lines": fresh})
        prev = [keys.get(c, c) for c in it["text_caption"]]
    return screen, captions


def frames_for_new_text(path, scan, step, kept, revisits, tmp, langs, detector, have_tess, limit=15):
    """Taramada görülen ama hiçbir seçilmiş karede olmayan kalıcı yazılar için kare ekler."""
    known = [ln["key"] for k in kept for ln in k.get("ocr", [])]
    times = []
    for n, it in enumerate(scan):
        nxt = [ln["key"] for ln in scan[n + 1]["ocr"]] if n + 1 < len(scan) else []
        cand = [ln for ln in it["ocr"] if ln["text"] in it["text_new"] and len(ln["key"]) >= 6
                and not difflib.get_close_matches(ln["key"], known, n=1, cutoff=0.75)
                and difflib.get_close_matches(ln["key"], nxt, n=1, cutoff=0.75)]      # bir sonraki örnekte de duruyor
        near = any(abs(k["t"] - it["t"]) <= 1.5 for k in kept)            # o an için zaten bir kare var
        if cand and not near and len(times) < limit:
            times.append(it["t"])
            known += [ln["key"] for ln in it["ocr"]]
    if not times:
        return 0
    for r in revisits:
        r["obj"] = kept[r["of"]]
    for n, t in enumerate(times):
        f = tmp / "raw" / f"extra_{n:03d}.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t + min(0.3, step / 2):.2f}", "-i", path, "-frames:v", "1",
                        "-vf", FRAME_SCALE.format(1280), "-q:v", "2", str(f)], check=True)
        if f.exists():
            kept.append({"t": t + min(0.3, step / 2), "t0": t, "reason": "text", "score": 1.0, "settled": True, "raw": f,
                         "ocr": ocr_frame(f, langs, detector, have_tess)})
    kept.sort(key=lambda k: k["t0"])
    for r in revisits:
        r["of"] = kept.index(r.pop("obj"))
    return len(times)


def norm_words(s):
    s = unicodedata.normalize("NFKD", s.casefold())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.findall(r"\w+", s)


def _said(word, spoken):
    """Kelime konuşmada geçiyor mu? Konuşma tanıma hatalarına karşı yaklaşık eşleşme."""
    if word in spoken:
        return True
    return len(word) >= 4 and bool(difflib.get_close_matches(word, spoken, n=1, cutoff=0.8))


def classify_text(kept, transcript):
    """Her karede: konuşmanın tekrarı olan altyazıları ve önceki karede zaten
    görünen satırları ayıklar; geriye yalnızca *yeni* ekran bilgisi kalır."""
    # 1. geçiş: konuşmayla eşleşen satırlar = altyazı
    for n, k in enumerate(kept):
        t0 = k["t0"] - 6
        t1 = (kept[n + 1]["t0"] if n + 1 < len(kept) else k["t"] + 30) + 2
        spoken = set()
        for s in transcript:
            if s["end"] >= t0 and s["start"] <= t1:
                spoken.update(norm_words(s["text"]))
        spoken = list(spoken)
        for ln in k.get("ocr", []):
            words = [w for w in norm_words(ln["text"]) if len(w) >= 2]
            ln["key"] = "".join(words)
            ln["caption"] = (len(words) >= 2 and len(ln["key"]) >= 8
                             and sum(_said(w, spoken) for w in words) / len(words) >= 0.7)
    # 2. geçiş: altyazıların ekrandaki yerini öğren; o banttaki diğer satırlar da altyazıdır
    caps = sorted((ln["y"], ln["h"]) for k in kept for ln in k.get("ocr", []) if ln["caption"] and ln["y"] is not None)
    clusters = []
    for y, h in caps:                              # altyazılar birden fazla yükseklikte olabilir
        if clusters and y - clusters[-1][0][0] <= 0.05:
            clusters[-1].append((y, h))
        else:
            clusters.append([(y, h)])
    # bant = (ortalama konum, tipik yazı boyu); en az iki eşleşmiş altyazı gerekir
    bands = [(float(np.mean([y for y, _ in c])), float(np.median([h for _, h in c]))) for c in clusters if len(c) >= 2]
    counts = {}
    for k in kept:
        for key in {ln["key"] for ln in k.get("ocr", [])}:
            counts[key] = counts.get(key, 0) + 1
    prev_lines = []
    for k in kept:
        k["text_new"], k["text_caption"] = [], []
        cur = set()
        for ln in k.get("ocr", []):
            cur.add(ln["key"])
            in_band = ln["y"] is not None and any(
                abs(ln["y"] - by) <= 0.04 and 0.7 <= ln["h"] / bh <= 1.4 for by, bh in bands)
            if ln["caption"] or (in_band and counts[ln["key"]] <= 2):
                k["text_caption"].append(ln["text"])
            elif not difflib.get_close_matches(ln["key"], prev_lines, n=1, cutoff=0.8):
                k["text_new"].append(ln["text"])
        prev_lines = list(cur)
    return bands


# ---- 3) konuşma -----------------------------------------------------------
def extract_audio(path, wav):
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", path, "-vn", "-ac", "1", "-ar", "16000",
                    "-c:a", "pcm_s16le", str(wav)], check=True)


def asr_faster_whisper(wav, model, lang, device="cpu"):
    from faster_whisper import WhisperModel
    m = WhisperModel(model, device=device, compute_type="int8" if device == "cpu" else "default")
    segs, info = m.transcribe(str(wav), language=lang, vad_filter=True)
    out = [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segs]
    return [s for s in out if s["text"]], info.language


def asr_mlx(wav, model, lang):
    """Apple Silicon Mac'lerde GPU ile çalışan whisper (mlx-whisper)."""
    import mlx_whisper
    repo = model if "/" in model else f"mlx-community/whisper-{model}"
    r = mlx_whisper.transcribe(str(wav), path_or_hf_repo=repo, language=lang)
    out = [{"start": s["start"], "end": s["end"], "text": s["text"].strip()} for s in r["segments"]]
    return [s for s in out if s["text"]], r.get("language")


def asr_sherpa(wav, model_dir, vad_model, lang):
    import sherpa_onnx
    d = Path(model_dir)
    pick = lambda pat: (sorted(d.glob(pat + ".int8.onnx")) or sorted(d.glob(pat + ".onnx")))[0]
    rec = sherpa_onnx.OfflineRecognizer.from_whisper(
        encoder=str(pick("*encoder*")), decoder=str(pick("*decoder*")),
        tokens=str(sorted(d.glob("*tokens*.txt"))[0]), language=lang or "", task="transcribe", num_threads=2)
    with wave.open(str(wav)) as w:
        sr = w.getframerate()
        samples = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0

    def decode(chunk):
        st = rec.create_stream()
        st.accept_waveform(sr, chunk)
        rec.decode_stream(st)
        return st.result.text.strip()

    out = []
    if vad_model:                                   # sessizlikleri atla, konuşma parçalarını zaman damgala
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = str(vad_model)
        cfg.silero_vad.min_silence_duration = 0.7
        cfg.silero_vad.max_speech_duration = 20
        cfg.sample_rate = sr
        vad = sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=120)
        win = cfg.silero_vad.window_size

        def drain():
            while not vad.empty():
                seg = vad.front
                chunk = np.array(seg.samples, dtype=np.float32)
                text = decode(chunk)
                if text:
                    out.append({"start": seg.start / sr, "end": seg.start / sr + len(chunk) / sr, "text": text})
                vad.pop()

        for p in range(0, len(samples), win):
            vad.accept_waveform(samples[p:p + win])
            drain()
        vad.flush()
        drain()
    else:                                           # VAD yoksa 25 sn'lik sabit parçalar
        step = 25 * sr
        for p in range(0, len(samples), step):
            chunk = samples[p:p + step]
            if np.abs(chunk).max() < 0.01:
                continue
            text = decode(chunk)
            if text:
                out.append({"start": p / sr, "end": (p + len(chunk)) / sr, "text": text})
    return out, lang or "otomatik"


def transcribe(wav, args):
    if args.reuse_transcript:      # önceki bir pack.json'daki transkripti yeniden kullan
        old = json.loads(Path(args.reuse_transcript).read_text(encoding="utf-8"))
        return old["transcript"], old["meta"].get("language"), old["meta"].get("asr", "hazır transkript")
    if args.asr == "none":
        return [], None, "kapalı"
    if args.asr in ("auto", "sherpa") and args.asr_model_dir:
        segs, lang = asr_sherpa(wav, args.asr_model_dir, args.vad_model, args.lang)
        return segs, lang, f"sherpa-onnx ({Path(args.asr_model_dir).name})"
    engines = []
    if args.asr in ("auto", "mlx") and sys.platform == "darwin" and platform.machine() == "arm64":
        engines.append(("mlx-whisper", asr_mlx))
    if args.asr in ("auto", "faster-whisper"):
        engines.append(("faster-whisper", asr_faster_whisper))
    for name, fn in engines:
        try:
            extra = (args.whisper_device,) if fn is asr_faster_whisper else ()
            segs, lang = fn(wav, args.whisper_model, args.lang, *extra)
            return segs, lang, f"{name} ({args.whisper_model})"
        except Exception as e:  # kütüphane yok, model indirilemedi vb. -> sıradakini dene
            log(f"uyarı: {name} çalışmadı ({type(e).__name__}: {str(e)[:200]})")
    log("uyarı: konuşma metne çevrilemedi. Transkriptsiz devam ediliyor.")
    return [], None, "başarısız"


# ---- 4) paket -------------------------------------------------------------
def font(size):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/System/Library/Fonts/Helvetica.ttc",
              "C:/Windows/Fonts/arialbd.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            pass
    return ImageFont.load_default()


def make_sheets(kept, outdir, cols=3, tile=384, per_sheet=9):
    """Kareleri zaman damgalı kontak baskılara dizer: aynı içerik, çok daha az görsel token."""
    outdir.mkdir(parents=True, exist_ok=True)
    sheets, fnt = [], font(22)
    for s in range(0, len(kept), per_sheet):
        group = kept[s:s + per_sheet]
        ims = []
        for k in group:
            im = Image.open(k["file"]).convert("RGB")
            im.thumbnail((tile, tile * 2))
            ims.append(im)
        th = max(im.height for im in ims)
        rows = -(-len(ims) // cols)
        sheet = Image.new("RGB", (min(cols, len(ims)) * tile, rows * th), "black")
        draw = ImageDraw.Draw(sheet)
        for n, (k, im) in enumerate(zip(group, ims)):
            x, y = (n % cols) * tile, (n // cols) * th
            sheet.paste(im, (x + (tile - im.width) // 2, y))
            label = f"#{k['n']}  {mmss(k['t0'])}"
            box = draw.textbbox((x + 6, y + 6), label, font=fnt)
            draw.rectangle((box[0] - 4, box[1] - 4, box[2] + 4, box[3] + 4), fill="black")
            draw.text((x + 6, y + 6), label, fill="yellow", font=fnt)
        p = outdir / f"sheet_{len(sheets) + 1:02d}.jpg"
        sheet.save(p, quality=85)
        sheets.append(p)
    return sheets


def img_tokens(path):
    w, h = Image.open(path).size
    return w * h / 750  # kaba tahmin; modele göre değişir


PROMPT = """Aşağıdaki paket bir videodan çıkarıldı: kronolojik anahtar kareler, karelerdeki ekran yazıları ve zaman damgalı konuşma.
Bunları birlikte okuyup videonun içeriğini bana not olarak çıkar:

1. **Tek cümlelik özet** — video ne anlatıyor?
2. **Ana noktalar** — maddeler hâlinde, her birinin yanında zaman damgası.
3. **Ekranda gösterilen bilgiler** — listeler, adımlar, fiyatlar, isimler, linkler, kodlar: eksiksiz ve yazıldığı gibi.
4. **Görsel olarak ne var** — konuşmada geçmeyen ama görüntüde olan şeyler (ürün, mekân, grafik, arayüz, hareket).
5. **Yapılacaklar / işime yarayacaklar** — varsa.
6. **Emin olunamayanlar** — okunamayan yazı, kareler arasında kaçmış olabilecek hareket.

Kurallar: "Ekran yazısı" satırları OCR'dan geldi ve hatalı olabilir; görseldeki yazıyla çelişirse görsele güven.
Karelerde ve konuşmada olmayan hiçbir şeyi ekleme.
"""


def build_pack(out, meta, kept, revisits, transcript, sheets, timings, screen_text=None, captions=None, comments=None):
    events = [("frame", k["t0"], k) for k in kept] + [("revisit", r["t"], r) for r in revisits]
    events.sort(key=lambda e: e[1])
    bounds = [k["t0"] for k in kept] + [float("inf")]

    text_chars = sum(len(s["text"]) for s in transcript) + sum(len(x) for k in kept for x in k["text_new"])
    tok_text = int(text_chars / 3.2) + 40 * len(kept)
    tok_frames = int(sum(img_tokens(k["file"]) for k in kept))
    tok_sheets = int(sum(img_tokens(s) for s in sheets))
    meta["tokens"] = {"text": tok_text, "frames": tok_frames, "sheets": tok_sheets}

    L = [f"# Video bağlam paketi — {meta['title']}", ""]
    L += [f"- Kaynak: {meta.get('webpage_url') or meta['source']}",
          f"- Süre: {mmss(meta['duration'])} · {meta['width']}x{meta['height']}",
          f"- Kare: {len(kept)} seçildi / {meta['analyzed']} tarandı ({meta['fps']} kare/sn)"
          + (f" · {meta['pruned_captions']} altyazı karesi elendi" if meta["pruned_captions"] else ""),
          f"- Konuşma: {len(transcript)} bölüm · motor: {meta['asr']}" + (f" · dil: {meta['language']}" if meta.get("language") else ""),
          f"- Ekran yazısı: {meta['ocr']}",
          f"- Yaklaşık boyut: metin ~{tok_text} token · tek tek kareler ~{tok_frames} token · kontak baskı ~{tok_sheets} token",
          ""]
    if meta.get("description"):
        L += ["## Açıklama (yayıncının yazdığı)", "", meta["description"].strip()[:1500], ""]
    L += ["## Okuma notu", "",
          "Kareler kronolojik. Bir kare, bir sonraki kareye kadar ekranda olanı temsil eder. "
          "\"Ekran yazısı\" yalnızca o karede *yeni* beliren ve konuşmanın tekrarı olmayan yazıları listeler (OCR; hatalı olabilir). "
          "Kareler arasındaki hareket pakette yoktur.", "",
          "## Zaman çizelgesi", ""]
    for kind, t, e in events:
        if kind == "revisit":
            L += [f"↩ **{mmss(t)}** — Kare {kept[e['of']]['n']}'deki görüntüye geri dönülüyor (kare tekrarlanmadı).", ""]
            continue
        n = e["n"]
        L += [f"### {mmss(t)} — Kare {n} ({LABEL[e['reason']]})", "", f"![Kare {n}](frames/{Path(e['file']).name})", ""]
        if e["text_new"]:
            L += ["**Ekran yazısı:**"] + [f"- {x}" for x in e["text_new"]] + [""]
        if e["text_caption"]:
            L += ["_Ekrandaki altyazı (konuşmanın yazılı hâli):_ " + " / ".join(e["text_caption"]), ""]
        said = [s for s in transcript if bounds[n - 1] <= s["start"] < bounds[n]]
        if n == 1:
            said = [s for s in transcript if s["start"] < bounds[1]]
        if said:
            L += ["**Konuşma:**"] + [f"- [{mmss(s['start'])}–{mmss(s['end'])}] {s['text']}" for s in said] + [""]
    if sheets:
        L += ["## Kontak baskılar", "", "Kareleri tek tek yüklemek yerine bunları vermek görsel token'ı düşürür.", ""]
        L += [f"![{p.name}](sheets/{p.name})" for p in sheets] + [""]
    (out / "pack.md").write_text("\n".join(L), encoding="utf-8")
    (out / "prompt.md").write_text(PROMPT, encoding="utf-8")
    (out / "transcript.txt").write_text(
        "\n".join(f"[{mmss(s['start'])}] {s['text']}" for s in transcript), encoding="utf-8")
    js = {"meta": meta, "timings_sec": timings,
          "frames": [{"n": k["n"], "t": round(k["t"], 2), "shown_from": round(k["t0"], 2), "time": mmss(k["t0"]), "reason": k["reason"],
                      "file": f"frames/{Path(k['file']).name}", "ocr_all": [ln["text"] for ln in k.get("ocr", [])],
                      "text_new": k["text_new"], "text_caption": k["text_caption"]} for k in kept],
          "revisits": [{"t": round(r["t"], 2), "of_frame": kept[r["of"]]["n"]} for r in revisits],
          "screen_text": screen_text, "captions": captions, "comments": comments or [],
          "transcript": [{"start": round(s["start"], 2), "end": round(s["end"], 2), "text": s["text"]} for s in transcript]}
    (out / "pack.json").write_text(json.dumps(js, ensure_ascii=False, indent=2), encoding="utf-8")


def write_transcript(out, meta, kept, transcript, sheets, screen_text=None, captions=None, comments=None):
    """Tek dosyada, modele verilmeye hazır döküm: açıklama + konuşma + ekrandaki yazılar + altyazılar + kareler.
    Özet değildir; videodaki bilginin tamamını metne çevirmeyi hedefler."""
    full = screen_text is not None
    if not full:                                    # tarama kapalıysa yalnızca seçilen karelerdeki yazılar
        screen_text = [{"t": k["t0"], "lines": k["text_new"]} for k in kept if k["text_new"]]
        captions = [{"t": k["t0"], "lines": k["text_caption"]} for k in kept if k["text_caption"]]
    L = [f"# {meta['title']}", ""]
    L.append(f"- Kaynak: {meta.get('webpage_url') or meta['source']}")
    if meta.get("uploader"):
        L.append(f"- Hesap: {meta['uploader']}")
    if meta.get("upload_date"):
        L.append(f"- Yayın tarihi: {meta['upload_date']}")
    L.append(f"- Süre: {mmss(meta['duration'])}")
    st = meta.get("stats") or {}
    if st:
        names = (("plays", "izlenme"), ("likes", "beğeni"), ("comments", "yorum"))
        L.append("- Etkileşim: " + " · ".join(f"{int(st[k]):,} {tr}".replace(",", ".") for k, tr in names if k in st))
    L.append(f"- İşlenme: {meta['created']} · konuşma: {meta['asr']}" + (f" · dil: {meta['language']}" if meta.get("language") else ""))
    L += ["", "## Açıklama", ""]
    L += [meta["description"].strip() if meta.get("description", "").strip() else "_Açıklama yok._", ""]

    L += ["## Konuşma", ""]
    if transcript:
        L += [f"[{mmss(s['start'])}] {s['text']}" for s in transcript]
    else:
        L.append("_Konuşma bulunamadı ya da çözülemedi._")
    L.append("")

    L += ["## Ekrandaki yazılar", ""]
    if screen_text:
        L += [f"- [{mmss(x['t'])}] " + " / ".join(x["lines"]) for x in screen_text]
        L += ["", "_Her yazı ilk göründüğü anda bir kez listelendi. Konuşmanın altyazıları aşağıda ayrı verildi. "
                  "Yazılar otomatik okundu; hatalı olabilir._"]
    else:
        L.append("_Konuşmanın altyazısı dışında ekran yazısı bulunamadı._")
    L.append("")

    if captions:
        L += ["## Ekrandaki altyazılar" if full else "## Ekrandaki altyazılardan örnekler", "",
              "_Videoyu yapanın ekrana yazdığı altyazılar. Konuşma dökümüyle çelişen bir kelime varsa altyazı doğrudur._", ""]
        L += [f"[{mmss(x['t'])}] " + " / ".join(x["lines"]) for x in captions] + [""]

    if comments:
        def line(c, indent=""):
            who = "video sahibi" if c.get("by_owner") else (f"@{c['user']}" if c.get("user") else "")
            likes = f"{c['likes']} beğeni" if c.get("likes") else ""
            tag = ", ".join(x for x in (who, likes) if x)
            text = " ".join(str(c["text"]).split())
            return f"{indent}- " + (f"({tag}) " if tag else "") + text
        L += [f"## Yorumlar ({len(comments)})", "",
              "_Gönderinin altındaki yorumlar; videonun içeriği değildir, izleyenlerin yazdıklarıdır._", ""]
        for c in comments:
            L.append(line(c))
            L += [line(r, "  ") for r in (c.get("replies") or []) if isinstance(r, dict) and str(r.get("text", "")).strip()]
        L.append("")

    L += ["## Görseller", ""]
    if sheets:
        names = ", ".join(f"`sheets/{p.name}`" for p in sheets)
        L += [f"{len(kept)} kare seçildi; zaman damgalarıyla şu dosyalarda: {names}. Görüntüde ne olduğunu anlamak için bu dosyaları da modele verin.", ""]
    L += ["Karelerin zamanı: " + ", ".join(mmss(k["t0"]) for k in kept), ""]
    path = out / "transkript.md"
    path.write_text("\n".join(L), encoding="utf-8")
    return path


# ---- ana akış -------------------------------------------------------------
def build_parser():
    ap = argparse.ArgumentParser(description="Videoyu yapay zekânın okuyabileceği bir döküme çevirir.")
    ap.add_argument("input", help="video dosyası, link ya da VidNote paketi (.zip)")
    ap.add_argument("-o", "--out", help="çıktı klasörü (varsayılan: <video adı>_pack)")
    ap.add_argument("--max-frames", type=int, default=60, help="en fazla kare (varsayılan 60)")
    ap.add_argument("--frame-size", type=int, default=768, help="pakete giren karenin uzun kenarı (px)")
    ap.add_argument("--lang", help="konuşma dili (tr, en ...). Boşsa otomatik")
    ap.add_argument("--asr", choices=["auto", "mlx", "faster-whisper", "sherpa", "none"], default="auto")
    ap.add_argument("--whisper-model", default="large-v3-turbo", help="whisper modeli (varsayılan large-v3-turbo)")
    ap.add_argument("--asr-model-dir", help="sherpa-onnx whisper model klasörü (çevrimdışı kullanım)")
    ap.add_argument("--vad-model", help="silero_vad.onnx yolu (sherpa ile zaman damgası için)")
    ap.add_argument("--reuse-transcript", help="önceki bir çalıştırmanın pack.json dosyası; konuşmayı yeniden çözmez")
    ap.add_argument("--ocr-lang", default="tur+eng", help="tesseract dilleri (varsayılan tur+eng)")
    ap.add_argument("--no-ocr", action="store_true")
    ap.add_argument("--text-scan", type=float, default=1.0,
                    help="ekrandaki tüm yazılar için tarama aralığı, saniye (varsayılan 1; 0 = kapalı, daha hızlı)")
    ap.add_argument("--keep-captions", action="store_true", help="yalnızca altyazı değişimi olan kareleri eleme")
    ap.add_argument("--cookies-from-browser", help="giriş isteyen siteler için tarayıcı adı (chrome, firefox)")
    ap.add_argument("--cookies", help="giriş isteyen siteler için cookies.txt dosyası (sunucuda tarayıcı yoksa)")
    ap.add_argument("--whisper-device", default="cpu", choices=["cpu", "cuda", "auto"], help="faster-whisper için aygıt")
    ap.add_argument("--comments", type=int, default=0,
                    help="linkten işlerken en fazla bu kadar yorum da al (varsayılan 0 = alma)")
    ap.add_argument("--title", help="başlığı elle ver")
    ap.add_argument("--source-label", help="dökümde 'Kaynak' olarak yazılacak ad (geçici dosya yolu yerine)")
    ap.add_argument("--description", help="açıklamayı elle ver (dosyadan işlerken)")
    return ap


def run(args, progress=None):
    """Bir videoyu işler; çıktı klasörünü ve özet bilgileri döndürür.
    progress: her aşamada kısa bir durum metniyle çağrılan işlev (arayüz için)."""
    say = progress or (lambda msg: None)
    timings, t_all = {}, time.time()
    tmp = Path(tempfile.mkdtemp(prefix="vidnote_"))
    try:
        t = time.time()
        src_meta = {"title": "", "description": "", "uploader": "", "upload_date": "", "webpage_url": "", "site": "", "stats": {}}
        comments = []
        if re.match(r"https?://", args.input):
            say("Video indiriliyor")
            log("indiriliyor…")
            src, src_meta, comments = fetch_url(args.input, tmp, args.cookies_from_browser, args.cookies, args.comments)
            default_out = re.sub(r"[^\w\-]+", "_", src_meta["title"])[:60].strip("_") or "video"
        else:
            src = args.input
            if not Path(src).exists():
                raise VidnoteError(f"Dosya bulunamadı: {src}")
            default_out = Path(src).stem
            if Path(src).suffix.lower() == ".zip":          # eklentinin ürettiği paket
                say("Paket açılıyor")
                src, src_meta, comments = open_package(src, tmp)
                log(f"paket: {len(comments)} yorum, açıklama {len(src_meta['description'])} karakter")
            else:
                src_meta["title"] = Path(src).stem
        if args.title:
            src_meta["title"] = args.title
        if args.description:
            src_meta["description"] = args.description
        out = Path(args.out or f"{default_out}_pack")
        if out.exists():
            shutil.rmtree(out)
        (out / "frames").mkdir(parents=True)
        timings["girdi"] = round(time.time() - t, 2)

        info = probe(src)
        dur = info["duration"]
        fps = 4 if dur <= 600 else (2 if dur <= 3600 else 1)
        log(f"video: {mmss(dur)}, {info['width']}x{info['height']}")

        say("Kareler seçiliyor")
        t = time.time()
        kept, revisits, analyzed = select_frames(src, fps, dur, args.max_frames)
        raw = extract_frames(src, fps, kept, tmp / "raw")
        for k, f in zip(kept, raw):
            k["raw"] = f
        timings["kare_secimi"] = round(time.time() - t, 2)
        log(f"kare: {analyzed} tarandı → {len(kept)} seçildi, {len(revisits)} tekrar atlandı")

        say("Konuşma yazıya dökülüyor")
        t = time.time()
        transcript, language, asr_name = [], None, "ses yok"
        if info["has_audio"]:
            wav = tmp / "audio.wav"
            extract_audio(src, wav)
            transcript, language, asr_name = transcribe(wav, args)
        timings["konusma"] = round(time.time() - t, 2)
        log(f"konuşma: {len(transcript)} bölüm ({asr_name})")

        say("Ekrandaki yazılar okunuyor")
        t = time.time()
        ocr_name = "kapalı"
        if not args.no_ocr:
            have_tess = bool(shutil.which("tesseract"))
            try:
                from rapidocr_onnxruntime import RapidOCR
                detector = RapidOCR()
            except Exception:
                detector = None
            if detector is None and not have_tess:
                log("uyarı: OCR motoru yok (pip install rapidocr-onnxruntime ve/veya tesseract kurun).")
                ocr_name = "yok"
            else:
                ocr_name = " + ".join(x for x in ["metin kutusu tespiti" if detector else "", "tesseract" if have_tess else ""] if x)
                for k in kept:
                    k["ocr"] = ocr_frame(k["raw"], args.ocr_lang, detector, have_tess)
        classify_text(kept, transcript)
        timings["ocr"] = round(time.time() - t, 2)

        # Ekrandaki yazıların tamamı: düzenli aralıklı tarama
        screen_text, captions, added = None, None, 0
        if ocr_name not in ("kapalı", "yok") and args.text_scan > 0:
            say("Ekrandaki tüm yazılar taranıyor")
            t = time.time()
            scan, step = scan_text(src, dur, args.text_scan, tmp, args.ocr_lang, detector, have_tess)
            classify_text(scan, transcript)
            added = frames_for_new_text(src, scan, step, kept, revisits, tmp, args.ocr_lang, detector, have_tess)
            if added:
                classify_text(kept, transcript)
            screen_text, captions = text_streams(scan, kept)
            timings["yazi_taramasi"] = round(time.time() - t, 2)
            log(f"yazı taraması: {len(scan)} örnek ({step:.1f} sn arayla) → {len(screen_text)} ekran yazısı, "
                f"{len(captions)} altyazı, {added} ek kare")

        # Tek yeniliği konuşmanın altyazısı olan kareleri ele
        pruned = 0
        while not args.keep_captions:
            keep_idx = [n for n, k in enumerate(kept)
                        if n == 0 or not (k["reason"] == "local" and k["text_caption"] and not k["text_new"])]
            if len(keep_idx) == len(kept):
                break
            pruned += len(kept) - len(keep_idx)
            remap = {old: new for new, old in enumerate(keep_idx)}
            kept = [kept[n] for n in keep_idx]
            revisits = [dict(r, of=remap[r["of"]]) for r in revisits if r["of"] in remap]
            classify_text(kept, transcript)

        say("Döküm hazırlanıyor")
        for n, k in enumerate(kept, 1):
            k["n"] = n
            im = Image.open(k["raw"]).convert("RGB")
            im.thumbnail((args.frame_size, args.frame_size))
            k["file"] = out / "frames" / f"f{n:03d}_{mmss(k['t0']).replace(':', '-')}.jpg"
            im.save(k["file"], quality=82)
        sheets = make_sheets(kept, out / "sheets")

        timings["toplam"] = round(time.time() - t_all, 2)
        meta = dict(src_meta, source=args.source_label or args.input, duration=round(dur, 2), width=info["width"], height=info["height"],
                    fps=fps, analyzed=analyzed, frames=len(kept), pruned_captions=pruned, asr=asr_name, ocr=ocr_name,
                    language=language, created=datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        build_pack(out, meta, kept, revisits, transcript, sheets, timings, screen_text, captions, comments)
        tpath = write_transcript(out, meta, kept, transcript, sheets, screen_text, captions, comments)
        tk = meta["tokens"]
        log(f"bitti: {tpath}  ({timings['toplam']} sn)")
        log(f"yaklaşık token: metin {tk['text']} · kareler {tk['frames']} · kontak baskı {tk['sheets']}")
        return {"out": out, "transcript": tpath, "sheets": sheets, "meta": meta, "timings": timings}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    args = build_parser().parse_args()
    try:
        run(args)
    except VidnoteError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
