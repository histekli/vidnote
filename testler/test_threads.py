#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VidNote Threads çözücüsü için testler — pytest yok, düz python3.

Çalıştırma:
    python3 testler/test_threads.py

Her senaryo tek satır 'OK/FAIL ad' basar; en sonda 'N test geçti' yazar.
Başarısız test varsa çıkış kodu 1 olur.

Not: Yalnızca threads_video.html ve threads_carousel.html içeriğindeki gerçek
değerler sabit yazılmıştır (bu iki fixture okunarak doğrulandı). Diğer üç fixture
(reply, text, login_wall) için beklenen kod değerleri sabit yazılmaz; çalışma
anında fixture HTML'inden regex ile çıkarılır.
"""
import re
import sys
from pathlib import Path

# vidnote.py kök dizinde; bu test testler/ altında. Kökü içe aktarma yoluna ekle.
KÖK = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(KÖK))
import vidnote as v  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def oku(ad):
    """Fixture dosyasını okur."""
    return (FIXTURES / ad).read_text(encoding="utf-8")


def kodlari_cikar(html):
    """Fixture HTML'inden gönderi kodlarını (sıra korunur, yineleme atılır) regex ile çıkarır.
    Böylece okunmamış fixture'lar için sabit kod değeri yazmaya gerek kalmaz."""
    return list(dict.fromkeys(re.findall(r'"code"\s*:\s*"([^"]+)"', html)))


# --- senaryolar -----------------------------------------------------------

def test_video():
    """Tek videolu gönderi: kod bulunur, en geniş video seçilir, meta alanları doğrudur."""
    html = oku("threads_video.html")
    post = v._threads_json_from_html(html, "VidCode123")
    assert post.get("code") == "VidCode123", post.get("code")
    # video sürümleri 480/1080/720 genişlikte; en geniş olan 1080 seçilmeli
    assert v._threads_best_video(post) == "https://cdn.threads.com/v/1080.mp4", v._threads_best_video(post)
    meta = v._threads_meta(post, "https://www.threads.com/@gezginfoto/post/VidCode123")
    assert meta["title"] == "@gezginfoto · VidCode123", meta["title"]
    assert meta["description"] == "Gün batımında drone çekimi 🌅", meta["description"]
    assert meta["uploader"] == "gezginfoto", meta["uploader"]
    assert meta["upload_date"] == "2021-01-01", meta["upload_date"]        # taken_at=1609459200 (UTC)
    assert meta["site"] == "Threads", meta["site"]
    assert meta["stats"] == {"likes": 4210}, meta["stats"]


def test_carousel():
    """3 öğeli carousel: _threads_media üç öğe döndürür, her öğenin en geniş medyası seçilir."""
    html = oku("threads_carousel.html")
    post = v._threads_json_from_html(html, "CarCode456")
    media = v._threads_media(post)
    assert len(media) == 3, len(media)
    # 1. öğe görsel: 480/1080/750 -> en geniş 1080
    assert v._threads_best_image(media[0]) == "https://cdn.threads.com/i/c1_1080.jpg", v._threads_best_image(media[0])
    # 2. öğe video: 540/1080 -> en geniş 1080
    assert v._threads_best_video(media[1]) == "https://cdn.threads.com/v/c2_1080.mp4", v._threads_best_video(media[1])
    # 3. öğe görsel: 320/1440 -> en geniş 1440
    assert v._threads_best_image(media[2]) == "https://cdn.threads.com/i/c3_1440.jpg", v._threads_best_image(media[2])


def test_reply():
    """Yanıt içeren sayfa: iki gönderi vardır; istenen kod tam olarak o gönderiyi döndürür
    (asıl kod istenince asıl gönderi gelir, yanıt gönderisi gelmez)."""
    html = oku("threads_reply.html")
    kodlar = kodlari_cikar(html)
    assert len(kodlar) == 2, kodlar                        # biri asıl, biri yanıt
    asil, yanit = kodlar[0], kodlar[1]
    assert asil != yanit, (asil, yanit)
    # asıl kod istenince: asıl gönderi döner, yanıt gönderisi değil
    secilen = v._threads_json_from_html(html, asil)
    assert secilen.get("code") == asil, secilen.get("code")
    assert secilen.get("code") != yanit, secilen.get("code")
    # seçim koda göre: yanıt kodu istenince de yalnız o gönderi döner (asıl değil)
    yanit_post = v._threads_json_from_html(html, yanit)
    assert yanit_post.get("code") == yanit, yanit_post.get("code")
    assert yanit_post.get("code") != asil, yanit_post.get("code")


def test_eslesmeyen_kod():
    """Veri olan sayfada eşleşmeyen kod ('YOK') istenince anlaşılır VidnoteError (giriş duvarı değil)."""
    html = oku("threads_video.html")               # bu sayfada veri VAR ama 'YOK' kodu yok
    try:
        v._threads_json_from_html(html, "YOK")
    except v.VidnoteError as e:
        assert str(e) != v.THREADS_LOGIN_HELP, "giriş duvarı mesajı olmamalı"
        assert "YOK" in str(e), str(e)
    else:
        assert False, "VidnoteError bekleniyordu ama hata atılmadı"


def test_text():
    """Medyasız (media_type 19) metin gönderisi: tek öğe döner, video/görsel yoktur, meta description vardır."""
    html = oku("threads_text.html")
    kodlar = kodlari_cikar(html)
    assert len(kodlar) == 1, kodlar
    post = v._threads_json_from_html(html, kodlar[0])
    media = v._threads_media(post)
    assert len(media) == 1, len(media)                     # tek öğe (carousel değil)
    assert v._threads_best_video(media[0]) is None, v._threads_best_video(media[0])
    assert v._threads_best_image(media[0]) is None, v._threads_best_image(media[0])
    meta = v._threads_meta(post, "https://www.threads.com/@x/post/" + kodlar[0])
    assert isinstance(meta["description"], str) and meta["description"] != "", repr(meta["description"])
    assert meta["site"] == "Threads", meta["site"]


def test_login_wall():
    """Veri içermeyen (giriş duvarı) sayfa: VidnoteError atar ve mesajı THREADS_LOGIN_HELP ile aynıdır."""
    html = oku("threads_login_wall.html")
    try:
        v._threads_json_from_html(html, "HERHANGI")        # veri yok; kod ne olursa olsun aynı hata
    except v.VidnoteError as e:
        assert str(e) == v.THREADS_LOGIN_HELP, str(e)
    else:
        assert False, "VidnoteError bekleniyordu ama hata atılmadı"


def test_shortcode():
    """_threads_shortcode üç durum: /post/ (sorgu atılır), /t/, ve eşleşmeyen (None)."""
    assert v._threads_shortcode("https://www.threads.com/@u/post/ABC123?igshid=1") == "ABC123"
    assert v._threads_shortcode("https://www.threads.net/t/XyZ_9-0") == "XyZ_9-0"
    assert v._threads_shortcode("https://www.threads.com/@u") is None


def test_threads_re():
    """THREADS_RE threads.com ve threads.net'i yakalar; instagram/youtube'u yakalamaz."""
    assert v.THREADS_RE.search("https://www.threads.com/@u/post/x")
    assert v.THREADS_RE.search("https://threads.net/@u/post/x")
    assert not v.THREADS_RE.search("https://www.instagram.com/p/x")
    assert not v.THREADS_RE.search("https://www.youtube.com/watch?v=x")


def test_threads_embed():
    """Gerçek Threads /embed sayfası kaydı: embed URL'si, sayfadan çıkarılan caption/hesap/tarih ve meta."""
    html = oku("threads_embed.html")
    # Sayfadaki gerçek değerler (fixture'dan; sabit yazmak yerine HTML'den çıkarılıyor)
    cap = re.search(r'class="BodyTextContainer"><span>([^<]+)</span>', html)
    assert cap, "fixture'da BodyTextContainer bulunamadı"
    kullanici = re.search(r'threads\.com/@([\w.]+)/post/', html)
    assert kullanici, "fixture'da gönderi linki bulunamadı"
    tarih = re.search(r"(\d{1,2} \w{3} \d{4})", html)
    assert tarih, "fixture'da tarih metni bulunamadı"

    embed = v._threads_embed_url("https://www.threads.com/@x/post/AbC123", "AbC123")
    assert embed == "https://www.threads.com/@x/post/AbC123/embed", embed
    assert v._threads_embed_url("https://www.threads.com/t/AbC123/", "AbC123").endswith("/t/AbC123/embed")

    meta = v._threads_text_meta("", "AbC123", "https://www.threads.com/@%s/post/AbC123" % kullanici.group(1),
                                caption=cap.group(1), kullanici=kullanici.group(1), tarih_metni=tarih.group(1))
    assert meta["description"] == cap.group(1), meta["description"]
    assert meta["uploader"] == kullanici.group(1), meta["uploader"]
    assert meta["title"] == "@%s · AbC123" % kullanici.group(1), meta["title"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", meta["upload_date"]), meta["upload_date"]
    assert meta["site"] == "Threads"


TESTLER = [
    ("video fixture: kod + en geniş video + meta alanları", test_video),
    ("carousel: 3 öğe + en geniş görsel/video", test_carousel),
    ("reply: asıl kod seçilir, yanıt kodu seçilmez", test_reply),
    ("eşleşmeyen kod ('YOK') -> VidnoteError", test_eslesmeyen_kod),
    ("text fixture: meta description + tek öğe", test_text),
    ("login_wall -> VidnoteError (THREADS_LOGIN_HELP)", test_login_wall),
    ("_threads_shortcode üç durum", test_shortcode),
    ("THREADS_RE threads.com/net yakalar, instagram/youtube yakalamaz", test_threads_re),
    ("threads /embed: URL + caption/hesap/tarih + meta", test_threads_embed),
]


def main():
    gecen = 0
    basarisiz = 0
    for ad, fn in TESTLER:
        try:
            fn()
        except AssertionError as e:
            basarisiz += 1
            print(f"FAIL {ad}  --  {e}")
        except Exception as e:                              # beklenmedik hata da başarısızlıktır
            basarisiz += 1
            print(f"FAIL {ad}  --  beklenmedik {type(e).__name__}: {e}")
        else:
            gecen += 1
            print(f"OK   {ad}")
    print(f"\n{gecen} test geçti" + (f", {basarisiz} test başarısız" if basarisiz else ""))
    if basarisiz:
        sys.exit(1)


if __name__ == "__main__":
    main()
