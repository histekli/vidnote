#!/usr/bin/env python3
"""
VidNote: videoyu yazıya döken araç. Terminalden ve tarayıcıdan aynı şekilde çalışır.

  vidnote <link ya da dosya>     dökümü çıkarır, ekrana yazar ve not klasörüne kaydeder
  vidnote serve                  tarayıcı sayfasını açar (ayarlıysa Telegram botunu da)
  vidnote list                   kayıtlı dökümleri listeler
  vidnote show <kimlik>          kayıtlı bir dökümü ekrana yazar

Terminalden çıkarılan dökümler tarayıcı sayfasında da görünür; ikisi aynı klasörü kullanır.
Ayarlar: bu dosyanın yanındaki ayarlar.env
"""
import argparse
import datetime
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from pathlib import Path

import requests
from flask import Flask, abort, jsonify, request, send_file

import vidnote

HERE = Path(__file__).resolve().parent
URL_RE = re.compile(r"https?://[^\s<>\"']+")
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".3gp"}
UPLOAD_EXT = VIDEO_EXT | {".zip"}             # .zip: tarayıcı eklentisinin ürettiği VidNote paketi
TG_DOWNLOAD_LIMIT = 20 * 1024 * 1024      # Telegram botları en fazla 20 MB dosya indirebilir
TG_TEXT_LIMIT = 3900                      # mesaj sınırı 4096 karakter; pay bırak


# ---- ayarlar --------------------------------------------------------------
def load_settings():
    cfg = {
        "TELEGRAM_TOKEN": "", "TELEGRAM_USERS": "", "TELEGRAM_API": "https://api.telegram.org",
        "COOKIES_BROWSER": "", "LANGUAGE": "", "WHISPER_MODEL": "large-v3-turbo",
        "NOTES_DIR": "~/VidNote", "HOST": "127.0.0.1", "PORT": "8765", "MAX_FRAMES": "60",
        "COOKIES_FILE": "", "WHISPER_DEVICE": "cpu", "TEXT_SCAN": "1", "LINK_COMMENTS": "0",
        "ASR_MODEL_DIR": "", "VAD_MODEL": "",
    }
    f = HERE / "ayarlar.env"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    for k in cfg:                                   # ortam değişkeni dosyadaki değeri ezer
        if os.environ.get("VIDNOTE_" + k):
            cfg[k] = os.environ["VIDNOTE_" + k]
    cfg["NOTES_DIR"] = Path(cfg["NOTES_DIR"]).expanduser()
    cfg["USERS"] = {int(x) for x in re.findall(r"\d+", cfg["TELEGRAM_USERS"])}
    return cfg


CFG = load_settings()
NOTES = CFG["NOTES_DIR"]
INBOX = NOTES / ".gelen"


def log(msg):
    print(f"[{datetime.datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


# ---- iş kuyruğu -----------------------------------------------------------
jobs = {}                 # id -> iş bilgisi (bellekte; biten işler ayrıca diske yazılır)
job_queue = queue.Queue()
lock = threading.Lock()


def new_job(source, origin, description="", chat_id=None, upload=None, lang=None):
    jid = f"{datetime.datetime.now():%Y-%m-%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    return {"id": jid, "source": source, "origin": origin, "description": description, "chat_id": chat_id,
            "upload": str(upload) if upload else None, "status": "queued", "stage": "Sırada", "error": None,
            "title": Path(source).name if upload else source, "lang": lang}


def add_job(source, origin, description="", chat_id=None, upload=None):
    """Sıraya iş ekler. source: link ya da dosya yolu. upload: iş bitince silinecek geçici dosya."""
    job = new_job(source, origin, description, chat_id, upload)
    with lock:
        jobs[job["id"]] = job
    job_queue.put(job["id"])
    return job


def process(job):
    """Bir işi çalıştırır, not klasörüne yazar; (klasör, not bilgisi) döndürür. Hata olursa klasörü siler."""
    out = NOTES / job["id"]
    argv = [job["source"], "-o", str(out), "--max-frames", CFG["MAX_FRAMES"],
            "--whisper-model", CFG["WHISPER_MODEL"], "--whisper-device", CFG["WHISPER_DEVICE"],
            "--text-scan", CFG["TEXT_SCAN"], "--comments", CFG["LINK_COMMENTS"]]
    lang = job.get("lang") or CFG["LANGUAGE"]
    if lang:
        argv += ["--lang", lang]
    if CFG["COOKIES_BROWSER"]:
        argv += ["--cookies-from-browser", CFG["COOKIES_BROWSER"]]
    if CFG["COOKIES_FILE"]:
        argv += ["--cookies", CFG["COOKIES_FILE"]]
    if CFG["ASR_MODEL_DIR"]:
        argv += ["--asr-model-dir", CFG["ASR_MODEL_DIR"]]
    if CFG["VAD_MODEL"]:
        argv += ["--vad-model", CFG["VAD_MODEL"]]
    if job["description"]:
        argv += ["--description", job["description"]]
    is_package = Path(job["source"]).suffix.lower() == ".zip"
    if job["upload"] and not is_package:            # pakette başlık ve kaynak paket.json'dan gelir
        argv += ["--title", Path(job["title"]).stem, "--source-label", job["title"]]
    args = vidnote.build_parser().parse_args(argv)
    try:
        result = vidnote.run(args, progress=lambda m: job.update(stage=m))
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        raise
    meta = result["meta"]
    note = {"id": job["id"], "title": meta["title"], "source": meta.get("webpage_url") or meta["source"],
            "uploader": meta.get("uploader", ""), "created": meta["created"], "duration": meta["duration"],
            "frames": meta["frames"], "origin": job["origin"], "seconds": result["timings"]["toplam"],
            "sheets": [p.name for p in result["sheets"]]}
    (out / "job.json").write_text(json.dumps(note, ensure_ascii=False, indent=2), encoding="utf-8")
    return out, note


def waiting_count():
    with lock:
        return sum(j["status"] in ("queued", "running") for j in jobs.values())


def worker():
    while True:
        jid = job_queue.get()
        job = jobs[jid]
        job.update(status="running", stage="Başlıyor")
        try:
            log(f"iş başladı: {job['source'][:90]}")
            out, note = process(job)
            job.update(status="done", stage="Bitti", title=note["title"])
            log(f"iş bitti ({note['seconds']} sn): {note['title'][:70]}")
            if job["origin"] == "telegram":
                tg_send_result(job["chat_id"], out, note)
        except vidnote.VidnoteError as e:
            fail(job, str(e))
        except Exception as e:
            traceback.print_exc()
            fail(job, f"Beklenmeyen hata: {type(e).__name__}: {e}")
        finally:
            if job["upload"]:
                Path(job["upload"]).unlink(missing_ok=True)
            job_queue.task_done()


def fail(job, message):
    job.update(status="error", stage="Hata", error=message)
    log(f"iş başarısız: {message.splitlines()[0][:150]}")
    if job["origin"] == "telegram":
        tg_text(job["chat_id"], "Olmadı.\n\n" + message[:700])


def list_notes():
    notes = []
    if NOTES.exists():
        for d in NOTES.iterdir():
            f = d / "job.json"
            if d.is_dir() and f.exists():
                try:
                    notes.append(json.loads(f.read_text(encoding="utf-8")))
                except ValueError:
                    pass
    return sorted(notes, key=lambda n: n["id"], reverse=True)


def note_dir(nid):
    """Not klasörünü döndürür; klasör dışına çıkan adları reddeder."""
    if not re.fullmatch(r"[\w\-]+", nid):
        abort(404)
    d = NOTES / nid
    if not (d / "job.json").exists():
        abort(404)
    return d


# ---- tarayıcı sayfası -----------------------------------------------------
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 ** 3


@app.before_request
def only_trusted_origins():
    """Başka bir web sitesinin, tarayıcın üzerinden bu yerel sunucuya iş göndermesini engeller.
    İzin verilenler: sayfanın kendisi, tarayıcı eklentileri ve Origin göndermeyen araçlar (terminal, curl)."""
    origin = request.headers.get("Origin")
    if request.method == "POST" and origin:
        host = origin.split("://", 1)[-1].split(":")[0]
        if not (origin.startswith(("chrome-extension://", "moz-extension://")) or host in ("localhost", "127.0.0.1", request.host.split(":")[0])):
            return jsonify(error="Bu kaynaktan gelen istek kabul edilmiyor."), 403


@app.get("/api/ping")
def api_ping():
    return jsonify(app="vidnote", ok=True)


@app.get("/")
def page():
    return send_file(HERE / "arayuz.html")


@app.get("/api/status")
def api_status():
    with lock:
        active = [{k: j[k] for k in ("id", "title", "status", "stage", "error", "origin")}
                  for j in jobs.values() if j["status"] in ("queued", "running", "error")]
    return jsonify(active=active, telegram=bool(CFG["TELEGRAM_TOKEN"]), notes_dir=str(NOTES))


@app.post("/api/jobs")
def api_new_job():
    if "file" in request.files:
        f = request.files["file"]
        name = Path(f.filename or "video.mp4").name
        if Path(name).suffix.lower() not in UPLOAD_EXT:
            return jsonify(error="Bu dosya türü desteklenmiyor. Video (mp4, mov, mkv, webm) ya da VidNote paketi (.zip) seç."), 400
        INBOX.mkdir(parents=True, exist_ok=True)
        dest = INBOX / f"{uuid.uuid4().hex[:8]}_{name}"
        f.save(dest)
        job = add_job(str(dest), "web", description=request.form.get("description", "").strip(), upload=dest)
        job["title"] = name
        return jsonify(id=job["id"])
    data = request.get_json(silent=True) or {}
    m = URL_RE.search(data.get("url", ""))
    if not m:
        return jsonify(error="Geçerli bir link bulunamadı. http:// ya da https:// ile başlamalı."), 400
    return jsonify(id=add_job(m.group(0), "web")["id"])


@app.post("/api/jobs/<jid>/dismiss")
def api_dismiss(jid):
    with lock:
        if jid in jobs and jobs[jid]["status"] == "error":
            del jobs[jid]
    return jsonify(ok=True)


@app.get("/api/notes")
def api_notes():
    return jsonify(notes=list_notes())


@app.get("/api/notes/<nid>")
def api_note(nid):
    d = note_dir(nid)
    note = json.loads((d / "job.json").read_text(encoding="utf-8"))
    note["transcript"] = (d / "transkript.md").read_text(encoding="utf-8")
    return jsonify(note)


@app.get("/files/<nid>/<path:rel>")
def api_file(nid, rel):
    d = note_dir(nid)
    target = (d / rel).resolve()
    if d.resolve() not in target.parents or not target.is_file():
        abort(404)
    return send_file(target, as_attachment=request.args.get("indir") == "1")


@app.post("/api/notes/<nid>/open")
def api_open(nid):
    d = note_dir(nid)
    opener = "open" if sys.platform == "darwin" else ("xdg-open" if os.environ.get("DISPLAY") and shutil.which("xdg-open") else None)
    if opener:                                      # masaüstü varsa dosya yöneticisinde aç
        subprocess.Popen([opener, str(d)])
        return jsonify(ok=True)
    return jsonify(ok=False, path=str(d))           # ekransız sunucu: yolu göster


# ---- Telegram -------------------------------------------------------------
def tg(method, files=None, http_timeout=60, **params):
    url = f"{CFG['TELEGRAM_API']}/bot{CFG['TELEGRAM_TOKEN']}/{method}"
    r = requests.post(url, data=params, files=files, timeout=http_timeout)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method}: {data.get('description', r.status_code)}")
    return data["result"]


def tg_text(chat_id, text):
    try:
        tg("sendMessage", chat_id=chat_id, text=text, disable_web_page_preview="true")
    except Exception as e:
        log(f"telegram mesajı gönderilemedi: {e}")
    time.sleep(1.1)                                 # aynı sohbete saniyede en fazla bir mesaj


def split_text(text, limit=TG_TEXT_LIMIT):
    """Uzun metni satır sınırlarından bölerek Telegram mesaj boyuna indirir."""
    parts, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:                    # tek satır bile sığmıyorsa kelime sınırından kes
            cut = line.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(line[:cut])
            line = line[cut:].lstrip()
        if len(cur) + len(line) > limit:
            parts.append(cur)
            cur = ""
        cur += line
    if cur.strip():
        parts.append(cur)
    return parts


def tg_send_result(chat_id, out, note):
    """Dökümü sohbete yollar: önce okunabilir metin, sonra .md dosyası ve kare sayfaları."""
    text = (out / "transkript.md").read_text(encoding="utf-8")
    body = text.split("## Görseller")[0].rstrip()   # görsel listesi sohbette gereksiz
    parts = split_text(body)
    for part in parts[:5]:
        tg_text(chat_id, part)
    if len(parts) > 5:
        tg_text(chat_id, "Döküm uzun; tamamı ekteki dosyada.")
    try:
        with open(out / "transkript.md", "rb") as f:
            tg("sendDocument", chat_id=chat_id, files={"document": ("transkript.md", f, "text/markdown")})
        time.sleep(1.1)
        for name in note["sheets"][:4]:             # sıkıştırılmasın diye fotoğraf değil dosya olarak
            with open(out / "sheets" / name, "rb") as f:
                tg("sendDocument", chat_id=chat_id, files={"document": (f"kareler_{name[-6:]}", f, "image/jpeg")},
                   caption="Videodan seçilen kareler")
            time.sleep(1.1)
    except Exception as e:
        log(f"telegram dosyası gönderilemedi: {e}")


def tg_handle(msg):
    chat_id = msg["chat"]["id"]
    user = msg.get("from", {}).get("id")
    if user not in CFG["USERS"]:
        log(f"izinsiz telegram kullanıcısı: {user}")
        tg_text(chat_id, "Bu bot özel.\n\n"
                         f"Kimlik numaran: {user}\n"
                         "Bot senin ise bilgisayardaki ayarlar.env dosyasında TELEGRAM_USERS satırına bu numarayı yazıp "
                         "uygulamayı yeniden başlat.")
        return
    text = msg.get("text") or msg.get("caption") or ""
    media = msg.get("video") or msg.get("video_note") or msg.get("animation")
    doc = msg.get("document")
    if not media and doc and (str(doc.get("mime_type", "")).startswith("video/")
                              or Path(doc.get("file_name", "")).suffix.lower() in VIDEO_EXT):
        media = doc
    if media:
        if media.get("file_size", 0) > TG_DOWNLOAD_LIMIT:
            tg_text(chat_id, "Bu video 20 MB'tan büyük; Telegram botları bu boyuttaki dosyayı indiremiyor.\n"
                             "Videonun linkini gönder ya da bilgisayarda tarayıcı sayfasından yükle.")
            return
        try:
            info = tg("getFile", file_id=media["file_id"])
            url = f"{CFG['TELEGRAM_API']}/file/bot{CFG['TELEGRAM_TOKEN']}/{info['file_path']}"
            name = Path(media.get("file_name") or info["file_path"]).name
            if Path(name).suffix.lower() not in VIDEO_EXT:
                name += ".mp4"
            INBOX.mkdir(parents=True, exist_ok=True)
            dest = INBOX / f"{uuid.uuid4().hex[:8]}_{name}"
            with requests.get(url, stream=True, timeout=120) as r:
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        f.write(chunk)
        except Exception as e:
            tg_text(chat_id, f"Videoyu Telegram'dan alamadım: {e}")
            return
        job = add_job(str(dest), "telegram", description=text.strip(), chat_id=chat_id, upload=dest)
        job["title"] = name
        tg_text(chat_id, queued_message())
        return
    urls = list(dict.fromkeys(URL_RE.findall(text)))[:5]
    if urls:
        for u in urls:
            add_job(u.rstrip(").,"), "telegram", chat_id=chat_id)
        tg_text(chat_id, queued_message(len(urls)))
        return
    tg_text(chat_id, "Bana bir video linki (Instagram, YouTube…) ya da 20 MB'a kadar bir video dosyası gönder.\n"
                     "Açıklamasını, konuşmasını ve ekrandaki yazıları döküm olarak geri yollarım.")


def queued_message(n=1):
    w = waiting_count()
    head = "Alındı, işliyorum." if n == 1 else f"{n} link alındı, sırayla işliyorum."
    return head + (f" Sırada {w} iş var." if w > n else "")


def tg_loop():
    offset = None
    try:
        me = tg("getMe")
        log(f"telegram botu bağlı: @{me.get('username')}")
    except Exception as e:
        log(f"telegram botuna bağlanılamadı ({e}). Token'ı kontrol et; bot olmadan devam ediliyor.")
        return
    if not CFG["USERS"]:
        log("uyarı: TELEGRAM_USERS boş. Bota bir mesaj at; cevapta kimlik numaran yazacak, onu ayarlar.env'e ekle.")
    while True:
        try:
            params = {"timeout": 50, "allowed_updates": json.dumps(["message"])}
            if offset is not None:
                params["offset"] = offset
            for u in tg("getUpdates", http_timeout=70, **params):
                offset = u["update_id"] + 1
                if "message" in u:
                    try:
                        tg_handle(u["message"])
                    except Exception:
                        traceback.print_exc()
        except Exception as e:
            log(f"telegram bağlantı hatası: {e}; 10 sn sonra yeniden denenecek")
            time.sleep(10)


# ---- başlat ---------------------------------------------------------------
def cmd_serve(argv):
    ap = argparse.ArgumentParser(prog="vidnote serve", description="Tarayıcı sayfasını (ve ayarlıysa Telegram botunu) çalıştırır.")
    ap.add_argument("--no-browser", action="store_true", help="başlarken tarayıcıyı açma")
    a = ap.parse_args(argv)
    NOTES.mkdir(parents=True, exist_ok=True)
    if INBOX.exists():                              # önceki çalıştırmadan kalan yarım yüklemeler
        shutil.rmtree(INBOX, ignore_errors=True)
    threading.Thread(target=worker, daemon=True).start()
    if CFG["TELEGRAM_TOKEN"]:
        threading.Thread(target=tg_loop, daemon=True).start()
    host, port = CFG["HOST"], int(CFG["PORT"])
    url = f"http://localhost:{port}"
    log(f"notlar: {NOTES}")
    log(f"sayfa: {url}   (kapatmak için Ctrl+C)")
    if host not in ("127.0.0.1", "localhost"):
        log("uyarı: sayfa ağa açık ve şifresiz. Yalnızca güvendiğin bir ağda ya da VPN arkasında kullan.")
    if not a.no_browser and (os.environ.get("DISPLAY") or sys.platform == "darwin"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    import logging
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    app.run(host=host, port=port, threaded=True)


def cmd_list(argv):
    notes = list_notes()
    if not notes:
        print("Henüz döküm yok.")
    for n in notes:
        print(f"{n['id']}  {vidnote.mmss(n['duration']):>6}  {n['title'][:70]}")


def cmd_show(argv):
    if not argv or not re.fullmatch(r"[\w\-]+", argv[0]) or not (NOTES / argv[0] / "transkript.md").exists():
        sys.exit("Böyle bir döküm yok. Kimlikleri görmek için: vidnote list")
    print((NOTES / argv[0] / "transkript.md").read_text(encoding="utf-8"))


def cmd_run(argv):
    ap = argparse.ArgumentParser(prog="vidnote", description="Bir videonun dökümünü çıkarır: açıklama, konuşma, ekrandaki yazılar.",
                                 epilog="Diğer komutlar: vidnote serve | vidnote list | vidnote show <kimlik>")
    ap.add_argument("input", help="video linki ya da dosya yolu")
    ap.add_argument("--description", default="", help="açıklamayı elle ver (dosyadan işlerken)")
    ap.add_argument("--lang", help="konuşma dili (tr, en ...). Boşsa ayarlardaki ya da otomatik")
    ap.add_argument("--json", action="store_true", help="çıktıyı JSON olarak ver (döküm metni ve dosya yolları)")
    ap.add_argument("--quiet", action="store_true", help="dökümü ekrana yazma; yalnızca dosya yolunu yaz")
    a = ap.parse_args(argv)
    NOTES.mkdir(parents=True, exist_ok=True)
    is_url = bool(re.match(r"https?://", a.input))
    src = a.input if is_url else str(Path(a.input).expanduser().resolve())
    job = new_job(src, "terminal", description=a.description, lang=a.lang)
    if not is_url:
        job["title"] = Path(src).name
        job["upload"] = None
    try:
        out, note = process(job)
    except vidnote.VidnoteError as e:
        sys.exit(f"Hata: {e}")
    text = (out / "transkript.md").read_text(encoding="utf-8")
    sheets = [str(out / "sheets" / s) for s in note["sheets"]]
    if a.json:
        print(json.dumps({"id": note["id"], "title": note["title"], "folder": str(out), "transcript_file": str(out / "transkript.md"),
                          "frame_sheets": sheets, "transcript": text}, ensure_ascii=False, indent=2))
    elif a.quiet:
        print(out / "transkript.md")
    else:
        print(text)
        print(f"---\nDöküm dosyası: {out / 'transkript.md'}\nKare sayfaları: {', '.join(sheets)}")


def main():
    argv = sys.argv[1:]
    commands = {"serve": cmd_serve, "list": cmd_list, "show": cmd_show}
    if argv and argv[0] in commands:
        commands[argv[0]](argv[1:])
    elif not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
    else:
        cmd_run(argv)


if __name__ == "__main__":
    main()
