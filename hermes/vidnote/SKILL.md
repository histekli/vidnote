---
name: vidnote
description: Bir videonun (Instagram Reels, YouTube, başka bir link ya da yerel dosya) tam dökümünü çıkarır - açıklama, konuşma, ekrandaki yazılar, altyazılar ve önemli kareler. Kullanıcı bir video linki ya da dosyası verip içeriğini, dökümünü, özetini ya da içindeki bir bilgiyi istediğinde kullan.
---

# vidnote

Bu makinede kurulu `vidnote` komutu bir videoyu izlemeden okunabilir hâle getirir.
Özet üretmez; videodaki bilginin tamamını metne ve önemli karelere çevirir. Özeti, listeyi ya da
kullanıcının istediği başka çıktıyı bu dökümden sen üretirsin.

## Nasıl kullanılır

1. Terminalde çalıştır (link ya da dosya yolu tırnak içinde):

   ```
   vidnote "<link ya da dosya>" --json
   ```

   İşlem video süresi kadar ya da daha uzun sürebilir. Komuta yeterli zaman aşımı ver (en az 10 dakika).

2. Çıktı JSON'dur:
   - `transcript`: dökümün tamamı (Markdown). Bölümleri: Açıklama, Konuşma, Ekrandaki yazılar, Ekrandaki altyazılar.
   - `frame_sheets`: videodan seçilen karelerin zaman damgalı görsel dosyaları.
   - `transcript_file`, `folder`: diskteki konumlar.

3. Görsel okuyabiliyorsan `frame_sheets` içindeki dosyalara da bak; görüntüde ne olduğu (ürün, mekân,
   kapak, grafik) yalnızca oradadır.

4. Kullanıcının istediğini dökümden üret.

## Kurallar

- Konuşma dökümü otomatik tanımadır ve hatalı olabilir. "Ekrandaki altyazılar" bölümü videoyu yapanın
  kendi yazdığı metindir; ikisi çelişirse altyazıya güven.
- Dökümde ve karelerde olmayan bir bilgiyi ekleme. Emin olmadığın yeri söyle.
- Kullanıcı yalnızca "dökümünü çıkar" dediyse `transcript` metnini değiştirmeden ver.

## Diğer komutlar

- `vidnote list`: daha önce çıkarılan dökümler
- `vidnote show <kimlik>`: kayıtlı bir dökümü yeniden yazdırır (videoyu tekrar işlemez)

## Hata durumları

- "Video indirilemedi": site giriş istiyor olabilir (Instagram'da sık). Kullanıcıya, sunucudaki
  `ayarlar.env` dosyasına çerez dosyası (COOKIES_FILE) eklemesi ya da video dosyasını göndermesi gerektiğini söyle.
- Komut sıfır olmayan kodla çıkarsa hata metni standart hata çıktısındadır; onu kullanıcıya aktar.
