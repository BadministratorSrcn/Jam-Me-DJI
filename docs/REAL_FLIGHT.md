# Gerçek Mini 4 Pro Uçuşu — RC-N2 Kontrol Listesi

Bu belge, simülatörde doğrulanmış PC görsel çekirdeğini gerçek bir
**DJI Mini 4 Pro + RC-N2 + Android telefon** üzerinde çalıştırmak içindir.

> İlk uçuşta GPS'i kapatmayın. DJI'nin kendi GPS'i sadece drone'un kendi
> stabilizasyonu için çalışır; PC'nin seyrüseferi **yalnızca karo tabanlı
> görsel fix** kullanır. GPS'siz uçuş, sistem bu modda doğrulandıktan sonra
> ayrı bir testtir.

---

## 0. Güncel çalışan akış (Kasım 2026)

Bu, sahada doğrulanan kurulumdur. Aşağıdaki eski RTMP bölümleri yedek
fallback olarak kalır; güncel akış **RTSP + yerel MJPEG + Google Maps
JavaScript (WebView2)** kullanır.

### Telefon (Tactical Pilot)

1. RC-N2 + Mini 4 Pro bağlı, DJI Fly kapalı.
2. Uygulamada `MSDK REGISTERED` ve `product CONNECTED` görünmeli.
3. **RTSP STREAM (PHONE SERVER)** düğmesine **bir kez** bas. Tekrar
   basmayın; `LIVE_STREAM_ALREADY_STARTED` yayının zaten çalıştığını
   gösterir.
4. Uygulama logunda gerçek RTSP URL'si çıkar:
   `rtsp://jamme:jamme@<telefon-ip>:8554/streaming/live/1`
5. Kamera **-90°** (uygulamadaki CAMERA düğmesi).

### PC — tek komut

```powershell
$env:GOOGLE_MAPS_API_KEY="<google-api-key>"
cd C:\Users\LNx\Desktop\Opencode\Jam-Me-DJI
.\pc_bridge\run_real.ps1 `
  -TilesDir C:\Users\LNx\Desktop\Opencode\JAMME\sim_tiles_diyarbakir `
  -Source "rtsp://jamme:jamme@192.168.1.104:8554/streaming/live/1" `
  -MapProvider google
```

Bu komut:
- bridge'i RTSP kaynağıyla başlatır (mediamtx gerekmez),
- Tkinter kontrol konsolunu (canlı video + telemetri) açar,
- Google Maps JavaScript penceresini (WebView2) açar.

### Google Maps anahtarı

- Anahtar **Maps JavaScript API** için açık olmalı; bu kurulumda çalışıyor.
- Anahtar repoya yazılmaz; `GOOGLE_MAPS_API_KEY` ortam değişkeninden okunur.
- Map Tiles API / Static Maps API gerekmez. (Sunucu tarafı karo katmanı
  `--map-provider google` ile istenirse ayrıca Map Tiles API açılmalı.)
- Anahtarı Google Cloud'da API ve IP kısıtlamasıyla koru; sohbette
  göründüyse rotate et.

### Video yolu

```
DJI kamera → telefon RTSP (31 fps, 720p) → bridge ffmpeg
          → 640×480 BGR → /stream.mjpg (MJPEG)
          → Tkinter LIVE DOWNLINK + Google Maps penceresinde kamera PiP
```

- GUI kamera görüntüsü `--rtsp-url http://127.0.0.1:8080/stream.mjpg` ile
  doğrudan MJPEG'den gelir; snapshot/JPEG istek gecikmesi yok.
- Google Maps penceresi `http://127.0.0.1:8080/map` sayfasını yükler;
  kamera PiP'i aynı sayfada `/stream.mjpg`'yi canvas'a çizer.
- Kaynak 31 fps olduğu için 60 fps mümkün değil; ~27–30 fps hedeflenir.

### Görsel fix ve 100 m

- Karo çözünürlüğü: 4 px/m = 0.25 m/px.
- 100 m'de kamera GSD'si ~0.24 m/px; karo ile neredeyse 1:1.
- Ölçek testi: 0.125–0.5 m/px aralığında eşleşme; 0.25 m/px'te
  143 match / conf 0.91.
- İlk uçuş: 50–100 m, kamera -90°, ışık ve dokulu zemin.
- Fix yoksa/gecikirse bridge hızları sıfırlar; kör uçuş yok.

### Google kontrol karoları

ESRI görüntüsü eskiyse, **görsel fix'in kullandığı** karoları Google'dan
aynı grid/georeferansla yeniden üret:

```powershell
cd C:\Users\LNx\Desktop\Opencode\JAMME
python download_google_vt.py `
  --tile-dir sim_tiles_diyarbakir `
  --output sim_tiles_diyarbakir_google `
  --zoom 19
```

Sonra bridge ve GUI'yi **aynı Google karo diziniyle** başlat:

```powershell
python pc_bridge\bridge_server.py --source "rtsp://..." `
  --tiles-dir C:\Users\LNx\Desktop\Opencode\JAMME\sim_tiles_diyarbakir_google
python pc_bridge\control_gui.py `
  --tiles-dir C:\Users\LNx\Desktop\Opencode\JAMME\sim_tiles_diyarbakir_google `
  --port 8080 --rtsp-url http://127.0.0.1:8080/stream.mjpg --map-provider local
```

`--map-provider local` Tkinter haritasını da yerel Google mozaiğinden çizer;
Google Maps penceresi ayrıca canlı Google uydu haritasını gösterir.

> **Uyarı:** `download_google_vt.py`, Google'ın genel `mt*.google.com/vt`
> karo uç noktasını kullanır; resmî **Map Tiles API** değildir. Google
> şartları açısından önbellekleme riski vardır. Lisanslı kullanım için
> Map Tiles API'yi açıp `download_google_tiles.py` kullanın.

### Drone kapalıyken

Video, `/snapshot` ve `/stream.mjpg` boş kalır; bu normaldir. Drone açılıp
RTSP yayını başlayınca köprü kareleri alır, fix ve kamera PiP döner.

---

## 1. Donanım ve ağ

- [ ] Mini 4 Pro + RC-N2 + USB-C kablo
- [ ] RC-N2 ve Mini 4 Pro firmware'i güncel (önce DJI Fly ile kontrol edin)
- [ ] Android telefon (Android 9+, USB host destekli)
- [ ] Telefon ve PC **aynı Wi-Fi ağında** (veya PC hotspot'unda)
- [ ] PC IP: `ipconfig` ile doğrula — bu kurulumda `192.168.1.107`
- [ ] Windows Güvenlik Duvarı'nda **TCP 8080** (bridge) ve **TCP 1935**
      (RTMP) gelen bağlantılara açık
- [ ] DJI Fly kapalı (MSDK ile aynı RC'yi paylaşamaz)

Telefon ile PC aynı ağda değilse RTMP ve `/status` çalışmaz. En kolay yol:
PC'de mobil hotspot açın, telefonu ona bağlayın; PC IP'sini hotspot
arayüzünün IP'si olarak girin.

## 2. Uçuş alanının karo haritası

Görsel fix, ancak uçuş alanının uydu karoları yüklüyse çalışır. Alan
`tile_config.json` dikdörtgeninin içinde olmalıdır.

Mevcut Diyarbakır test alanı:

```
C:\Users\LNx\Desktop\Opencode\JAMME\sim_tiles_diyarbakir
```

Yeni bir alan için (kuzeybatı ve güneydoğu köşeleri):

```powershell
cd C:\Users\LNx\Desktop\Opencode\JAMME
python prepare_tiles.py --generate-grid `
  --top-left 37.9200,40.2200 `
  --bottom-right 37.9100,40.2350 `
  --tile-size 300 --output tiles_flight --kml
python tiles_flight\download_tiles.py
```

`--tile-size` metre cinsindendir. İlk gerçek test için 200–300 m'lik,
16–25 karoluk bir alan yeterlidir. İndirme tamamlanınca
`tiles_flight\tile_config.json` dosyasının varlığını kontrol edin.

## 3. PC yığınını başlat

### Seçenek A — tek komut

```powershell
cd C:\Users\LNx\Desktop\Opencode\Jam-Me-DJI
.\pc_bridge\run_real.ps1 -TilesDir C:\Users\LNx\Desktop\Opencode\JAMME\sim_tiles_diyarbakir
```

Script mediamtx, bridge ve GUI'yi ayrı pencerelerde açar. PowerShell
execution policy engellerse:

```powershell
powershell -ExecutionPolicy Bypass -File .\pc_bridge\run_real.ps1 `
  -TilesDir C:\Users\LNx\Desktop\Opencode\JAMME\sim_tiles_diyarbakir
```

### Seçenek B — elle

```powershell
# 1) RTMP sunucusu (mediamtx)
C:\Users\LNx\AppData\Local\JamMe\mediamtx\mediamtx.exe

# 2) Bridge — RTMP'yi PC'de 127.0.0.1'den okur
cd C:\Users\LNx\Desktop\Opencode\Jam-Me-DJI
python pc_bridge\bridge_server.py `
  --source rtmp://127.0.0.1:1935/live `
  --tiles-dir C:\Users\LNx\Desktop\Opencode\JAMME\sim_tiles_diyarbakir `
  --port 8080

# 3) GUI
python pc_bridge\control_gui.py `
  --tiles-dir C:\Users\LNx\Desktop\Opencode\JAMME\sim_tiles_diyarbakir `
  --port 8080
```

Bridge logunda `RTMP` bağlantısı ve karo yüklemesi görünmelidir.

## 4. Telefona APK kurulumu

APK:

```
C:\Users\LNx\Desktop\Opencode\Jam-Me-DJI\app\app\build\outputs\apk\debug\app-debug.apk
```

Kurulum yolları:

1. **USB ile (önerilen):** Telefonda Geliştirici Seçenekleri → USB hata
   ayıklama açık; telefonu PC'ye bağlayın:
   ```powershell
   .\pc_bridge\install_apk.ps1
   ```
   veya elle:
   ```powershell
   & "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe" install -r `
     "C:\Users\LNx\Desktop\Opencode\Jam-Me-DJI\app\app\build\outputs\apk\debug\app-debug.apk"
   ```
2. **Dosya ile:** APK'yı telefona kopyalayın (USB / Drive / Telegram) ve
   dosya yöneticisinden kurun. "Bilinmeyen kaynaklar" iznini verin.

İlk açılışta MSDK internet üzerinden kayıt olur. Telefonun interneti
olmalıdır; kayıt başarısızsa App Key / paket adı eşleşmesini kontrol edin
(`com.tactical.pilot`, App Key `dff381b608c20a4e62589660`).

## 5. RC-N2 + telefon bağlantısı

- [ ] RC-N2 kapalıyken telefonu USB-C ile bağlayın, sonra RC'yi açın.
- [ ] Android "Bu USB aksesuarı açılsın mı?" derse **Tactical Pilot** seçin
      (isterseniz "Her zaman" işaretleyin).
- [ ] DJI Fly'ı zorla durdurun; arka planda açık olmasın. Aynı RC'yi iki
      uygulama paylaşamaz.
- [ ] Uygulamada üst satırda `MSDK REGISTERED` ve `product CONNECTED`
      görünmelidir.
- [ ] Görünmüyorsa: kabloyu değiştirin, RC firmware'ini güncelleyin,
      uygulamayı yeniden başlatın.
- [ ] İlk kayıt için telefonun interneti gerekir. Wi-Fi'ye bağlanıp
      **RETRY MSDK REGISTER** düğmesine basabilirsiniz; kayıt bir kez
      başarılı olduktan sonra PC ağına geçebilirsiniz.

> Alt modül (görsel çekirdek) eski bir commit'teyse köprü ilk fix'te
> `reacq_min_matches` hatası verir. Repo kökünde:
> `git submodule update --init --recursive`

## 6. Video ve kamera

1. Uygulamada RTMP adresi: `rtmp://192.168.1.107:1935/live`
2. **RTMP STREAM: START** → `RUNNING` olmalı.
3. Bridge logunda video akışı başlamalı; GUI'de canlı görüntü görünmeli.
4. **CAMERA: POINT DOWN (-90°)** ile gimbalı aşağı çevirin.
5. Görüntü dikey aşağı bakmıyorsa gimbal komutunu tekrar deneyin; fiziksel
   montajı da kontrol edin.

Video gelmiyorsa sırasıyla: telefon-PC ağ bağlantısı, Windows firewall
1935, mediamtx penceresi, `RTMP start` hatası.

## 7. İlk test — pervaneler SÖKÜLÜ, drone yerde

Bu adım motorları çalıştırmadan komut zincirini doğrular.

- [ ] RC ve drone açık, uygulama bağlı.
- [ ] GUI'de HOLD seçin.
- [ ] Uygulamada **PC CONTROL: ARM** → `ARMED`, telemetride `VS=ON`.
- [ ] GUI'de küçük bir hedef seçip GO verin; uygulamada `vf/vr`
      değerlerinin değiştiğini görün.
- [ ] GUI STOP → `vf=0 vr=0`.
- [ ] Uygulamada tekrar ARM'e basıp kontrolü bırakın (DISARM).

Virtual stick enable başarısız olursa hata kodunu not edin; drone'un
GPS/IMU durumu ve RC modu önemlidir. Bazı firmware'ler yerde virtual
stick'i açmaz; bu durumda testi hover'da tekrarlayın.

## 8. İlk gerçek uçuş

> İlk uçuşta hızı **0.8–1.2 m/s**, durma yarıçapını **2–3 m** yapın.
> RTMP görüntüsü 1–2 s gecikmeli olduğu için yüksek hızda hedefi aşabilir.

1. Drone'u açık, GPS kilitlenmiş ve yeterli uyduyla kaldırın.
2. 5–10 m yükseklikte hover'a alın.
3. GUI'de **HOLD** → uygulamada **ARM**. `VS=ON` görün.
4. GUI'de hız limitini 1.0 m/s yapın; hedefi 3–5 m ileriye koyup **GO**.
5. Drone hedefe yaklaşırken durmalı; `dist_m` azalmalı.
6. **STOP** her zaman sıfır hız gönderir.
7. RC çubuğuna dokunursanız DJI kontrolü geri alır; uygulama bunu
   `authority lost` olarak loglar ve döngüyü durdurur.

> Yön testi: hedef drone'un **önündeyse** drone ileri gitmeli. Ters yöne
> gidiyorsa hemen STOP/RC ile durdurun ve `VirtualStickRunner.kt` içindeki
> `pitchSign` (ileri/geri) veya `rollSign` (sağ/sol) işaretini çevirin.

İlk uçuşta GPS'i kapatmayın. Görsel fix ile DJI GPS arasındaki farkı GUI'de
izleyin; fark sürekli büyüyorsa görsel eşleme ölçeği/karoları kontrol edin.

**Ölçek notu:** Karo tabanlı eşleme, 0.125–0.5 m/px aralığında (yaklaşık
30–200 m irtifa) Diyarbakır karolarıyla test edildi ve eşleşme bulundu.
İlk uçuş için 50–100 m irtifa ve dik aşağı bakan gimbal en güvenilir
aralıktır. Çok alçakta (ör. 10 m) kare küçük bir alanı kapsar; doku azsa
`INVALID` fix beklenir ve hızlar sıfırlanır.

## 9. GPS'siz test (ikinci aşama)

Ancak ilk uçuş ve görsel fix doğrulandıktan sonra:

- [ ] Drone'u GPS almayan bir alana/odaya alın (veya GPS'i ekranlama).
- [ ] Yeterli ışık ve dokulu zemin sağlayın (halı, çim, beton deseni).
- [ ] 2–3 m yükseklikte hover; görsel fix `VALID` olmalı.
- [ ] ARM + HOLD; drone görsel ankrajda kalmalı.
- [ ] Küçük GO hedefleriyle ilerleyin.
- [ ] Fix `INVALID` olursa hızlar sıfırlanır; drone DJI stabilizasyonuna
      bırakılır. Bu beklenen ve güvenli davranıştır.

DJI firmware'i GPS'siz durumda virtual stick'i reddederse sistem uçuş
kontrolü yapmaz; monitör/hold modunda kalır ve sapma loglanır. Bu, DJI
tarafındaki bir kısıttır; firmware zorlanmaz.

## 10. Hızlı sorun giderme

| Belirti | Kontrol |
|---|---|
| `MSDK NOT REGISTERED` | Telefon interneti, App Key, paket adı |
| `product —` | USB kablosu **veri** destekli mi, RC firmware, DJI Fly kapalı mı |
| RTMP `START` olmuyor | PC IP, 1935 firewall, mediamtx çalışıyor mu |
| GUI'de video yok | Bridge `--source` doğru mu, ffmpeg PATH'te mi |
| `VISUAL no fix` | Gimbal aşağı baksın (sistem 2 sn'de bir ilk konumu yeniden arar); karolar alanı kapsıyor mu, ışık/doku yeterli mi |
| `fix INVALID` | Kamera aşağı bakıyor mu, görüntü bulanık/karanlık mı |
| ARM `FAILED` | Drone bağlı mı, GPS/IMU hazır mı, virtual stick destekli mi |
| Drone komutları ters | `VirtualStickRunner.kt` içindeki `pitchSign` / `rollSign` |

---

### Güvenlik kuralları

- Pervaneler takılıyken ilk ARM testini yapmayın.
- İlk uçuşta GPS'i emniyet ağı olarak açık tutun.
- Görsel fix geçersizken sistem sıfır hız gönderir; drone'u kör uçurmaya
  çalışmayın.
- RC çubukları her zaman önceliklidir; panik anında çubuklara dokunun.
- Her uçuştan önce pil, pervane ve RC bağlantısını kontrol edin.
