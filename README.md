# VidNote (Linux ve macOS)

Bir videodaki bilginin tamamını, yapay zekânın değerlendirebileceği metne ve önemli karelere çevirir.
Özet üretmez; özetleme ya da başka bir işleme için dökümü istediğin modele verirsin.

Bir videodan çıkan döküm (`transkript.md`):

| Bölüm | İçeriği |
|---|---|
| Açıklama | Gönderinin altındaki metin (linkten işlenirse) |
| Zaman akışı | Videonun zaman aralıklarına bölünmüş hâli. Her aralıkta: o andaki konuşma, ekranda ilk kez görünen yazılar, ilgili kare numaraları ve görüntü sürekli hareket ediyorsa bunun notu |
| Yorumlar | Gönderinin altındaki yorumlar (tarayıcı eklentisiyle ya da ayar açıksa linkten) |
| Görseller | Seçilen karelerin numaralı, zaman damgalı sayfaları |

Aynı bilgi iki kez yazılmaz. Konuşma, ekrandaki altyazılarla kelime kelime karşılaştırılır: altyazı net okunmuşsa
onun yazımı alınır ("Cloud'un Houtus'u" yerine "Claude'un How-to'su"), okunaksızsa konuşma dökümü kalır.
Ham konuşma dökümü, altyazı akışı ve tüm ekran yazıları `pack.json` içinde ayrıca saklanır.

## Kurulum

```
unzip vidnote.zip && cd vidnote
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
vidnote "https://www.instagram.com/p/..."          # görsel ya da çoklu görsel gönderi: açıklama + görsellerdeki yazılar
vidnote "https://www.threads.com/@kul/post/..."    # Threads gönderisi (video/görsel/carousel/metin); çerez gerekir
vidnote video.mp4 --description "gönderi metni"    # dosyadan; açıklamayı elle ver
vidnote ekran_goruntusu.png                        # tek görsel de olur
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

## Hız ve veri kullanımı

- **YouTube'da hazır altyazı:** videonun altyazısı varsa (elle yazılmış ya da otomatik) o kullanılır; ses indirilmez ve
  konuşma baştan çözülmez. Uzun videolarda en büyük kazanç budur. Kapatmak için `SITE_CAPTIONS=0`.
- **Çözünürlük:** `MAX_HEIGHT=360` indirilen veriyi ve disk kullanımını belirgin azaltır; karşılığında ekrandaki küçük
  yazılar okunmayabilir. Video işlem bitince silinir, diskte yalnızca seçilen kareler kalır.
- **Yazı taraması:** en yavaş adımdır. `TEXT_SCAN=2` yarıya indirir, `TEXT_SCAN=0` kapatır (altyazıyla konuşma
  düzeltmesi yalnızca seçilen karelerle yapılır).
- Yazı okuma çekirdek sayısına göre paralel çalışır; konuşma çözme ile görüntü tarafı aynı anda yürür.

Videoyu hiç indirmeden, tarayıcıda oynarken kare alıp YouTube'un altyazısını okuyan bir eklenti yolu da mümkün,
ama henüz yazılmadı.

## Görsel gönderiler

Instagram'da `/p/` ile başlayan gönderiler tek görsel, çoklu görsel ya da video olabilir. Bunlar gallery-dl ile
indirilir (video indiricisi yt-dlp görsel indirmez). Çıkan dökümde açıklama, her görselin üstündeki yazılar
("Görsel 1: …") ve görsellerin numaralı kontak baskısı bulunur. Gönderide video da varsa ilk video normal
biçimde işlenir, görseller "Gönderideki görseller" bölümüne eklenir.
Görselde *ne olduğu* (yazı dışındaki içerik) metne çevrilmez; onu görsel dosyasını modele vererek alırsın.

## Threads gönderileri

`threads.com` ve `threads.net` linkleri desteklenir: video, tek görsel, çoklu görsel (carousel) ve yalnızca metin
gönderileri. Döküm, gönderi türüne göre video dökümü, görsel dökümü ya da (metin gönderisinde) yalnızca açıklama olur.
Bağlantı biçimleri: `/@kullanici/post/<kod>`, `/t/<kod>` ve yönlendirmeli `/share/<kod>` (son adresten kod çözülür).

**Çerez zorunlu.** Threads gönderi verisini yalnızca giriş yapmış oturuma gömüyor; girişsiz istekler (hangi
User-Agent'la olursa olsun) yalnızca giriş duvarını döndürür. Bu yüzden `ayarlar.env` içinde `COOKIES_BROWSER`
(Threads'e giriş yaptığın tarayıcı) ya da `COOKIES_FILE` (dışa aktardığın cookies.txt) ayarlanmış olmalı —
Instagram'daki ile aynı yol. Çerez yoksa VidNote neyi ayarlaman gerektiğini söyleyen bir hata verir.

Threads için yt-dlp/gallery-dl kullanılmaz; sayfaya gömülü JSON (`data-sjs`) doğrudan ayrıştırılır. Threads bu gömülü
verinin biçimini zaman zaman değiştirdiği için bu yol kırılgandır; biçim değişirse ayrıştırma "gönderi verisi
bulunamadı" hatası verebilir.

## Güncelleme

Yeni paketi eski klasörün üstüne aç ve `./kur.sh` komutunu yeniden çalıştır (eksik parçaları kurar).
`ayarlar.env` dosyan ve `~/VidNote` altındaki dökümlerin yerinde kalır.

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
| `MAX_HEIGHT` | İndirilecek videonun en fazla yüksekliği (varsayılan 720) |
| `SITE_CAPTIONS` | YouTube'un hazır altyazısını kullan (varsayılan 1) |
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
- görsel gönderiler, sentetik görsellerle: üç görselli paket, tek görsel dosyası, video + görsel karışık paket
- indirme ilerlemesi, bilerek yavaşlatılmış yerel bir indirmeyle (yüzde ve çubuk sayfada görünüyor)
- altyazıyla konuşma düzeltmesi ve zaman akışı: gerçek Türkçe videoda ve sentetik videolarda

Denenmedi:
- macOS'ta geliştirme sırasında hiçbir şey çalıştırılamadı. Kullanıcı Mac'te kurup çalıştırdı: YouTube ve Instagram
  Reels indirmesi çalıştı; kare çıkarma adımı yeni ffmpeg'in kaldırdığı bir seçenek yüzünden hata verdi ve düzeltildi.
  Düzeltme Linux'ta (ffmpeg 6.1) denendi, Mac'teki ffmpeg sürümünde denenmedi.
- YouTube'un hazır altyazısını alma ve yalnızca görüntü indirme (ağ yoktu; altyazı ayrıştırıcısı örnek veriyle denendi)
- paralel yazı okumanın ve eşzamanlı çalışmanın gerçek hız kazancı (test makinesi 2 çekirdekli; Mac'te ölçülmedi)
- gallery-dl ile gerçek Instagram'dan görsel gönderi indirme (ağ yoktu; indirilen klasörün okunması sahte veriyle denendi)
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
- Altyazıyla düzeltme kusursuz değil: altyazı yanlış okunmuş ama "net" görünmüşse hata konuşmaya taşınabilir.
- Ekran kaydı gibi yazı dolu görüntülerde her aralık için yalnızca en büyük yazılar listelenir; geri kalanı karelerdedir.
- Görüntüde *ne olduğu* (yazı dışındaki öğeler) metne çevrilmez; bunun için kare sayfalarını modele vermek gerekir.
- Eklenti çoklu gönderilerde (kaydırmalı) yalnızca ilk videoyu alır; fotoğraf gönderilerini paketlemez.
  Görsel gönderiler şimdilik yalnızca link yapıştırarak işlenir ve bu yolda yorumlar alınmaz.
- Çoklu gönderide birden fazla video varsa yalnızca ilki işlenir.
- Eklenti Safari ve Firefox'ta çalışmaz.
- Konuşan kişinin farklı planları birkaç kez seçiliyor.
- OCR süslü ya da çok küçük yazıda hata yapıyor; kıyafetteki marka yazısını da ekran yazısı sayıyor.
- Kareler arasındaki hareket dökümde yok.
- Uzun YouTube videolarında hazır altyazı kullanılmıyor; konuşma her zaman baştan çözülüyor.
