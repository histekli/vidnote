# VidNote (Linux ve macOS)

Bir videodaki bilginin tamamını, yapay zekânın değerlendirebileceği metne ve önemli karelere çevirir.
Özet üretmez; özetleme ya da başka bir işleme için dökümü istediğin modele verirsin.

Bir videodan çıkanlar:

| Bölüm | Kaynağı |
|---|---|
| Açıklama | Gönderinin altındaki metin (linkten işlenirse) |
| Konuşma | Sesin zaman damgalı yazıya dökümü |
| Ekrandaki yazılar | Başlık, liste, fiyat, kapak yazısı gibi gömülü metinler; her biri bir kez |
| Ekrandaki altyazılar | Videoyu yapanın ekrana yazdığı altyazıların akışı |
| Kareler | Görüntünün değiştiği anlardan seçilmiş, zaman damgalı kareler |
| Yorumlar | Gönderinin altındaki yorumlar (tarayıcı eklentisiyle ya da ayar açıksa linkten) |

## Kurulum

```
git clone https://github.com/histekli/vidnote.git && cd vidnote
./kur.sh
```

`kur.sh` eksikse ffmpeg ve tesseract'ı (Türkçe paketiyle) kurar, `.venv` içinde Python ortamını hazırlar,
`ayarlar.env` dosyasını oluşturur ve `vidnote` komutunu `~/.local/bin` altına bağlar.
İlk çalıştırmada konuşma modeli (yaklaşık 1,6 GB) bir kez indirilir.

**macOS:** Önce [Homebrew](https://brew.sh) kurulu olmalı. Apple Silicon'da hızlı konuşma tanıma (mlx-whisper) da kurulur.
Kurulumdan sonra Finder'da `VidNote.command` dosyasına çift tıklamak sayfayı açar. İlk açılışta macOS
"tanımlanamayan geliştirici" uyarısı verirse dosyaya sağ tıklayıp "Aç" de.
Mac'te Instagram linkleri için en kolay yol `ayarlar.env` içinde `COOKIES_BROWSER=chrome` yazmaktır
(Safari için Terminal'e Tam Disk Erişimi vermek gerekir).

## Kullanım

**Terminalden**

```
vidnote "https://www.youtube.com/watch?v=..."     # dökümü ekrana yazar ve kaydeder
vidnote video.mp4 --description "gönderi metni"    # dosyadan; açıklamayı elle ver
vidnote instagram_hesap_KOD.zip                    # eklentinin ürettiği paket (video + açıklama + yorumlar)
vidnote "<link>" --json                            # başka bir programın okuması için
vidnote "<link>" --quiet                           # yalnızca döküm dosyasının yolunu yazar
vidnote list                                       # kayıtlı dökümler
vidnote show <kimlik>                              # kayıtlı dökümü yeniden yazdırır
```

Döküm standart çıktıya, ilerleme ve uyarılar standart hataya yazılır. Hata olursa çıkış kodu 1'dir.

**Tarayıcıdan**

```
vidnote serve
```

`http://localhost:8765` adresinde link yapıştırılır ya da dosya bırakılır; eski dökümler solda listelenir.
Terminalden çıkarılan dökümler de aynı listede görünür.

Sunucu başka bir makinedeyse sayfayı ağa açmak yerine SSH tüneli kullan:

```
ssh -L 8765:localhost:8765 kullanici@sunucu      # sonra kendi tarayıcında http://localhost:8765
```

Sayfa şifresizdir. `ayarlar.env` içinde `HOST=0.0.0.0` yaparsan aynı ağdaki herkes erişir.

Sürekli açık kalsın istersen (`systemd --user`):

```
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/vidnote.service <<EOF
[Unit]
Description=VidNote
[Service]
ExecStart=%h/.local/bin/vidnote serve --no-browser
Restart=on-failure
[Install]
WantedBy=default.target
EOF
systemctl --user enable --now vidnote
```

**Telefondan paylaşarak**

Instagram uygulamasında Paylaş → Telegram → kendi botun. Bot bu makinede çalışan VidNote'a (aşağıdaki
"Kendi Telegram botunla") ya da Hermes'e bağlıysa link bilgisayarda işlenir ve döküm sohbete geri gelir.
Bilgisayar kapalıysa Telegram mesajı 24 saat bekletir; açılınca işlenir.

**Tarayıcı eklentisiyle (video + açıklama + yorumlar tek pakette)**

`eklenti/` klasörü Chrome, Edge ve Brave için bir eklentidir. Kurulum: `chrome://extensions` → sağ üstte
"Geliştirici modu" → "Paketlenmemiş öğe yükle" → `eklenti` klasörünü seç. Sonra açık Instagram sekmelerini yenile.

Kullanım: Instagram'da bir Reels ya da gönderi açıkken araç çubuğundaki VidNote düğmesine bas. Yeni sekmede
paket hazırlanır: `video.mp4`, `paket.json`, `aciklama.txt`, `yorumlar.txt` tek bir `.zip` içinde.
VidNote çalışıyorsa paket kendiliğinden gönderilir; çalışmıyorsa "Paketi indir" ile kaydedip sonra
`vidnote paket.zip` ya da sayfaya sürükleyerek işlersin.

Eklenti veriyi sırayla üç yerden arar ve hangisinin işe yaradığını "Tanı" bölümünde gösterir:
sayfanın kendi yüklediği yanıtlar, sayfaya gömülü veriler, Instagram'ın gönderi ve yorum adresleri.
Yalnızca açtığın gönderiyi okur, senin oturumunla çalışır, gönderi başına en fazla 7 istek yapar.
Yorumlardaki kullanıcı adları varsayılan olarak pakete alınmaz (ayarlardan açılabilir); video sahibinin
yanıtları "video sahibi" diye işaretlenir.

Instagram'ın kullanım şartları otomatik veri toplamayı kısıtlar. Eklenti kişisel kullanım için
tasarlandı; mağazada yayımlamak ya da toplu veri çekmek için uygun değildir.

**Hermes Agent ile**

`hermes/vidnote/SKILL.md` dosyasını Hermes'in beceri klasörüne kopyala:

```
mkdir -p ~/.hermes/skills/vidnote && cp hermes/vidnote/SKILL.md ~/.hermes/skills/vidnote/
```

Beceri, Hermes'e bir video linki geldiğinde terminalde `vidnote "<link>" --json` çalıştırmasını ve
çıkan dökümle çalışmasını söyler. Hermes ile denenmedi; klasör yolu Hermes belgelerindeki düzene göre yazıldı.

**Kendi Telegram botunla (isteğe bağlı)**

`ayarlar.env` içine `TELEGRAM_TOKEN` yazıp `vidnote serve` çalıştırırsan bota gönderdiğin link ya da
20 MB'a kadar video işlenir, döküm sohbete geri gelir. Bota ilk mesajında kimlik numaranı söyler; onu
`TELEGRAM_USERS` satırına yazmadan kimseye cevap vermez. Hermes zaten Telegram'a bağlıysa buna gerek yok;
Hermes'in bot anahtarını buraya yazma, ikisi çakışır.

## Instagram ve giriş isteyen siteler

Instagram videoyu çoğunlukla yalnızca giriş yapmış kullanıcıya verir. Ekransız bir sunucuda giriş yapılmış
tarayıcı olmadığı için iki yol var:

1. Kendi bilgisayarındaki tarayıcıdan `cookies.txt` dışa aktar, sunucuya kopyala, `ayarlar.env` içinde
   `COOKIES_FILE=/yol/cookies.txt` yaz. Bu dosya hesabına giriş anahtarıdır; kimseyle paylaşma.
2. Videoyu indirip dosya olarak ver (`vidnote video.mp4 --description "..."` ya da tarayıcı sayfasına bırak).

Sunucu bir veri merkezindeyse Instagram ve YouTube o adresten gelen istekleri çerezle bile engelleyebilir.
Evdeki bir makinede bu sorun çok daha az görülür.

## Nasıl çalışıyor

1. **Kare seçimi.** Video tek geçişte taranır. Durağan içerikte (slayt, ekran kaydı) küçük bir yazı değişimi
   bile yakalanır ve kare görüntü durulunca alınır. Hareketli çekimde (konuşan kişi, elde kamera) kıpırtı
   sayılmaz; kadraja yeni bir görsel girdiğinde kare alınır. Aynı plana dönülürse kare tekrarlanmaz.
2. **Konuşma.** Whisper (large-v3-turbo) ile zaman damgalı döküm.
3. **Yazı taraması.** Video saniyede bir örneklenir, ekrandaki tüm yazılar okunur. Konuşmayla eşleşen ve
   aynı konumda akan satırlar altyazı sayılır, diğerleri ekran yazısı. Hiçbir karede bulunmayan kalıcı bir
   yazı görülürse o an için ek kare alınır.
4. **Döküm.** Hepsi `transkript.md` içinde birleşir; kareler `sheets/` altında zaman damgalı sayfalara dizilir.

Her döküm `~/VidNote/<kimlik>/` klasöründedir: `transkript.md`, `sheets/`, `frames/`, ayrıntı için `pack.md` ve `pack.json`.

## Ayarlar (`ayarlar.env`)

| Ayar | Anlamı |
|---|---|
| `LANGUAGE` | Konuşma dili; boşsa otomatik |
| `WHISPER_MODEL`, `WHISPER_DEVICE` | Konuşma modeli; NVIDIA kartın varsa `cuda` |
| `TEXT_SCAN` | Yazı tarama aralığı (sn); `0` kapatır ve işlemi belirgin hızlandırır |
| `NOTES_DIR` | Dökümlerin klasörü |
| `HOST`, `PORT` | Tarayıcı sayfasının adresi |
| `COOKIES_FILE`, `COOKIES_BROWSER` | Giriş isteyen siteler için çerezler |
| `LINK_COMMENTS` | Linkten işlerken alınacak en fazla yorum; `0` kapalı |
| `TELEGRAM_TOKEN`, `TELEGRAM_USERS` | İsteğe bağlı bot |

## Neler denendi, neler denenmedi

Denendi (Linux, 2 çekirdek):
- `kur.sh` içindeki Python ortamı kurulumu ve kurulan `vidnote` komutu
- terminal: dosyadan ve paketten döküm, `--json`, `--quiet`, `list`, `show`, hata kodları
- tarayıcı sayfası: link, dosya ve paket yükleme, sıra, döküm görünümü, hata mesajları, yabancı siteden gelen isteğin reddi
- linkten indirme yolu, yerel bir sunucudaki video dosyasıyla
- Telegram botu, sahte bir Telegram sunucusuna karşı
- eklenti, gerçek Chrome'da **sahte bir Instagram sitesine** karşı: verinin üç ayrı yoldan gelmesi, yorum
  sayfalama, yorum adresinin reddedilmesi, gönderi olmayan sayfa, paket indirme, VidNote'a gönderme ve dökümde yorumlar
- gerçek bir video (2:22, Türkçe, konuşan kişi + araya giren görseller + gömülü altyazı) ve iki sentetik test videosu

Denenmedi:
- **macOS'ta hiçbir şey**: `kur.sh`'ın Mac dalı, `VidNote.command`, mlx-whisper. Kod aynı, ama Mac'te çalıştırılmadı.
- **eklenti gerçek Instagram'da**. Sahte site, Instagram'ın yanıtlarının 2025'te bilinen biçimlerine göre yazıldı;
  Instagram bunları sık değiştiriyor. İlk denemede çalışmazsa paket sayfasındaki "Tanı bilgisini kopyala" çıktısı
  neyin değiştiğini gösterir.
- `kur.sh` içindeki sistem paketi kurulumu (test makinesinde paketler zaten vardı)
- faster-whisper ile konuşma tanıma (test ortamı model indirmeye izin vermedi; konuşma aynı Whisper modelinin
  başka bir çalıştırıcısıyla denendi)
- Instagram ve YouTube'dan gerçek indirme, çerezler, linkten yorum alma (`LINK_COMMENTS`)
- gerçek Telegram ve Hermes
- ekran kaydı, slayt, hızlı kurgu, 10 dakikadan uzun video

Bilinen eksikler:
- Eşikler tek bir gerçek videoya göre ayarlandı.
- Yazı taraması yavaş: 2:22'lik videoda 2 çekirdekte yaklaşık 90 saniye.
- Altyazı akışı saniyede bir örneklendiği için bir saniyeden kısa kalan parçalar atlanabilir; konuşma dökümü onları kapsar.
- Eklenti çoklu gönderilerde (kaydırmalı) yalnızca ilk videoyu alır; fotoğraf gönderilerini paketlemez.
- Eklenti Safari ve Firefox'ta çalışmaz.
- Konuşan kişinin farklı planları birkaç kez seçiliyor.
- OCR süslü ya da çok küçük yazıda hata yapıyor; kıyafetteki marka yazısını da ekran yazısı sayıyor.
- Kareler arasındaki hareket dökümde yok.
- Uzun YouTube videolarında hazır altyazı kullanılmıyor; konuşma her zaman baştan çözülüyor.
