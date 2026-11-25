# THERMALIS DATA — Bulut kabulü ve merkezi katalog (Faz 04)

Durum: kod hazır, **varsayılan kapalı**. Uygulamanın etkin deposu `local` kalır. Bulut kabulü açıldığında
yalnız buluta yüklenen YENİ paketler kabul edilir; mevcut yerel kayıtlar (HD01 dahil) taşınmaz.

## 1. Mimari

```
İkinci bilgisayar                         R2                                   Bu bilgisayar (tek yayımlayıcı)
thermalis-upload upload <paket>  ──►  incoming/<paket>/...           ◄── izleyici: incoming/_ready/ (sayfalı, seyrek)
  (yalnız "upload" ortamı)            incoming/<paket>/THERMALIS_READY.json  talep: state/intake/<paket>/<özet>.json
                                       incoming/_ready/<paket>.json            indir + SHA-256 → mevcut kayıt iş akışı (QA)
                                                                                yayın → releases/.../_COMMIT.json (son)
                                       releases/<SEG>/<SEG>/<sürüm>/...  ◄──   katalog girdisi → catalog/commits/...
                                       catalog/commits/<SEG>/<SEG>/<sürüm>.json
                                                                                yerel yansı → data/_cloud/releases/... (türetilmiş)
                                                                                SQLite dizini (türetilmiş) ← yerel + bulut kayıtları
```

- **Yetkili kayıtlar:** bulut sürümleri için `releases/.../_COMMIT.json` ve `records/*.json`. Yerel yansı ve SQLite
  türetilmiştir; silinirse `pixi run storage cloud-scan` (artımlı) veya tam yeniden kurulum ile geri gelir.
- **Görünürlük:** yalnız commit işareti doğrulanmış sürümler katalogda görünür. Yarım, reddedilmiş veya işaretsiz
  yayınlar hiçbir koşulda "analize hazır" gösterilmez.
- **Yineleme yok:** aynı sürüm hem yerelde hem bulutta kaydedilmez. Yerel kabul, bulutta işaretli sürümü
  `ALREADY_REGISTERED` sayar; bulut kabulü yerelde kayıtlı sürümü `ALREADY_REGISTERED_LOCALLY` ile reddeder.
- **Bilimsel QA değişmedi:** bulut paketi yalıtılmış geçici bir veri kökünde aynı `ingest_path` iş akışından geçer.
- **Maliyet:** keşif yalnız `incoming/_ready/` (paket başına bir küçük nesne) ve `catalog/commits/` (sürüm başına bir)
  listeler; varsayılan aralık 300 sn. Değişmeyen girdiler için okuma isteği yapılmaz.
- **Disk:** önbellek sınırı 20 GB (ayrı); hazırlık alanı ayrı sayılır (varsayılan 200 GB sınır, paket başına en çok
  ~2x paket boyutu). Alan yetmezse paket ERTELENİR (reddedilmez). Windows yol sınırı aşılacaksa da ertelenir
  (`staging_path_too_long`): `settings.json → cloud_intake.staging_dir` için kısa bir klasör seçin (ör. `C:\TSTAGE`).

## 2. Cloudflare yapılandırması (önerilen, ek ücretli hizmet yok)

Normal bir R2 API anahtarı **önek (prefix) kısıtlaması yapamaz**: kovaya "Object Read & Write" verilen anahtar
`releases/` altına da yazabilir. İkinci bilgisayarın yayınlara dokunamaması için **ayrı kabul kovası** önerilir:

| Kova | Amaç | Erişim |
|---|---|---|
| `thermalis-data-dev` | yayınlar, katalog, talep/durum kayıtları | yalnız **publisher** anahtarı (bu bilgisayar) |
| `thermalis-intake-dev` (yeni) | ikinci bilgisayarın yüklediği paketler | **uploader** anahtarı (yalnız bu kova) + publisher |

1. R2 → *Create bucket* → `thermalis-intake-dev` (Standard, public access KAPALI).
2. *Manage R2 API Tokens* → yeni token "uploader": **Object Read & Write**, **yalnız** `thermalis-intake-dev`.
3. Publisher token'ı: **Object Read & Write**, kapsam `thermalis-data-dev` **ve** `thermalis-intake-dev`.
   - Talep (claim), kabul/red durumu, katalog girdisi ve yayınlar yalnız `thermalis-data-dev`'e yazılır; yayımlayıcı
     kabul kovasına hiçbir şey yazmaz (salt okunur sarmalayıcıyla doğrulandı). Ancak R2 token'ında izin düzeyi
     seçilen tüm kovalar için tektir ve uygulama rol başına tek kimlik bilgisi kullanır; bu yüzden publisher token'ı
     iki kovada da Read & Write olarak verilir (salt okunur ayrı bir token desteklenmez).
4. **Yaşam döngüsü (lifecycle) kuralları — dikkat:**
   - `thermalis-intake-dev` → `incoming/` için **yaşa göre silme kuralı KOYMAYIN.** Bulut kabulü kapalıyken,
     ertelenen (alan, yol sınırı) veya henüz işlenmemiş bir paket, kabul edilmeden önce sessizce silinebilirdi.
     R2 kurallarında yalnız önek + yaş vardır; "kabul edildi" durumuna göre ayrım yapılamaz. Kabul edilmiş paketlerin
     temizliği ileride ayrı ve onaylı bir işlemle yapılacaktır.
   - R2 yeni kovalarda **yarım çok parçalı yüklemeleri 7 gün sonra iptal eden** varsayılan bir kural oluşturur
     (panoda *Object lifecycle rules* altında kontrol edin). Bu kural tamamlanmış hiçbir nesneyi silmez; kesilmiş
     bir yüklemenin yalnız yarım parçalarını kaldırır. Yükleyici bunu algılar ve aynı komutla dosyayı baştan gönderir
     (doğrulandı; veri kaybı yok, yalnız yeniden aktarım). İsterseniz süreyi uzatabilir veya kaldırabilirsiniz.
   - `thermalis-data-dev` → yalnız `smoke-tests/` ve `e2e-tests/` önekleri için 1 gün sonra silme kuralı güvenlidir.
     `releases/`, `catalog/`, `state/` öneklerine **hiçbir silme kuralı koymayın.**
5. **Bucket Lock** kurulmadı; kalıcı saklama kuralı veri sahibinin onayıyla ayrıca denenmelidir.

Tek kova ile de çalışır (ayar boş bırakılırsa), ancak bu durumda yalıtım yazılım düzeyindedir, anahtar düzeyinde değildir.

Bu bilgisayarda (ayrı kova kullanılıyorsa):

```
pixi run storage configure-cloud --endpoint https://<HESAP>.r2.cloudflarestorage.com --bucket thermalis-data-dev --intake-bucket thermalis-intake-dev
pixi run storage credentials set --role publisher     # yalnız token değiştiyse (iki kovayı kapsayan yeni token)
pixi run storage cloud-intake on --interval 300
```

## 3. İkinci bilgisayar: kurulum ve yükleme

Gereken: Git, [pixi](https://pixi.sh). Tam uygulama, GDAL, Node gerekmez.

```
git clone https://github.com/alituncgeo-boop/THERMALIS.git C:\THERMALIS
cd C:\THERMALIS
pixi install --environment upload
scripts\thermalis-upload.cmd configure --endpoint https://<HESAP>.r2.cloudflarestorage.com --bucket thermalis-data-dev --intake-bucket thermalis-intake-dev
scripts\thermalis-upload.cmd credentials
scripts\thermalis-upload.cmd status
```

`credentials` komutu uploader anahtarını yankısız sorar ve Windows Credential Manager'a yazar.

Paket yükleme (herhangi bir klasörden; göreli yol çağrılan klasöre göre çözülür):

```
C:\THERMALIS\scripts\thermalis-upload.cmd upload "D:\Paketler\XYZ_ADMIN_BOUNDARIES_v1.0.0" --dry-run
C:\THERMALIS\scripts\thermalis-upload.cmd upload "D:\Paketler\XYZ_ADMIN_BOUNDARIES_v1.0.0"
```

- Çıktı: dosya sayısı, aktarılan hacim, hız, kalan bayt, tahmini süre.
- Kesilirse **aynı komut** yeniden çalıştırılır; doğrulanmış dosyalar yeniden gönderilmez, büyük dosyalar parçadan sürer.
- `ALREADY_UPLOADED`: aynı paket zaten yüklü (işlem yapılmadı). `package_id_conflict`: aynı kimlikle farklı içerik;
  düzeltilmiş paket **yeni bir paket kimliğiyle** (ör. `_r2` eki veya yeni sürüm) yüklenmelidir.
- Çıkış kodları: 0 tamam · 2 reddedildi (paketi düzeltin) · 3 geçici hata (yeniden deneyin).
- Kısıtlı (RESTRICTED/UNKNOWN) paket yalnız yetki kaydıyla yüklenir: `--authorization yetki.json` (bkz. §5).

## 4. Durumlar ve arayüz

DATA sayfası → *Veri kabul* şeridi bulut paketlerini "bulut" etiketiyle, indirme ilerlemesini ve red nedenlerini
gösterir. *Depolama* şeridi: bağlantı durumu, sağlayıcı/kova, etkin depo, bulut sürüm sayısı, işaretli hacim
(**katalogdan türetilmiş**; sağlayıcı fatura ölçümü değildir), önbellek kullanımı, son başarılı eşitleme,
kimlik bilgisi varlığı (değer asla gösterilmez). Katalog satırları ve ayrıntı paneli depolama yerini gösterir.

## 5. Kısıtlı veri yetkisi

Şablon: `scripts\thermalis-upload.cmd authorization-template "<paket>"` (manifest özeti dolu gelir) veya
`docs/templates/cloud-authorization.example.json`. Kayıt paketin erişim sınıfını **değiştirmez**; yalnız belirli
sağlayıcı + kovada saklanmaya veri sahibinin izin verdiğini belgeler ve pakete (MANIFEST SHA-256) bağlıdır.
`storage.bucket` kalıcı yayın kovasıdır (`thermalis-data-dev`). Kayıt yükleyicide ve kabulde ayrı ayrı doğrulanır ve
sürümün `records/THERMALIS_AUTHORIZATION.json` dosyası olarak saklanır.

## 6. Faz 05 — HD01 taşıma prosedürü (BU FAZDA YAPILMADI)

Kaynak: `C:\THERMALIS_TRANSFER\INCOMING\HD01_ADMIN_BOUNDARIES_v1.0.0-rc1` (RESTRICTED, QA_CONDITIONAL, MANIFEST SHA-256
`12162d6363bc0055d9e95a3e73af13bdd4119a4a0442e53643b4234ced22405b`). Faz 04'te yalnız salt okunur kuru çalıştırma yapıldı.

1. **Yetki:** veri sahibinden (İBB) imzalı izin; `authorization-template` çıktısı doldurulur
   (`storage.bucket = thermalis-data-dev`, `document_reference` = imzalı belge). Erişim sınıfı RESTRICTED kalır.
2. **Ön doğrulama:** `pixi run data verify THERMALIS_DATA/HD01_ADMIN_BOUNDARIES/ISTANBUL 1.0.0-rc1` → PASS;
   kayıt klasörü ve kaynak paket SHA-256 anlık görüntüsü alınır.
3. **Taşıma yöntemi:** HD01 yeniden kayıt edilmez (zaman damgalı kayıtlar değişirdi). Faz 05'te eklenecek
   `thermalis storage migrate-release <id> <sürüm> --authorization <dosya>` komutu mevcut değişmez sürüm klasörünü
   (payload + version/quality/integrity + rapor + `_derived/layer_facts.json` içerik kimlikleri) bayt bayt
   `publish_release` ile yayımlar; her nesne geri okunup doğrulanır; işaret son yazılır; katalog girdisi yazılır.
4. **Karşılaştırma:** işaretteki her payload özeti `integrity.json` ile birebir; sayımlar (39/979), 6 KIMLIKNO boşluğu,
   67 bulgu / 66 tekil kimlik, domainler aynı olmalı. Yerel kayıt dokunulmadan kalır; katalog yinelemeyi
   `DUPLICATE_ACROSS_BACKENDS` olarak gösterir ve yerel kaydı kullanır.
5. **Geçiş (ayrı onay):** etkin depo yalnız açık geçiş onayıyla buluta çevrilir; yerel kopya silinmez.

## 7. Canlı uçtan uca test (elle)

```
$env:THERMALIS_R2_LIVE_TEST = "1"; pixi run r2-intake-e2e; Remove-Item Env:THERMALIS_R2_LIVE_TEST
```

Benzersiz `e2e-tests/<çalıştırma>/` ad alanı, geçici katalog, yalnız sentetik paketler. Adımlar: yükleme, yineleme,
işaretsiz paket görünmezliği, kabul, tam SHA-256 geri okuma, katalog görünürlüğü, sessiz yeniden tarama,
işaretlerden tam yeniden kurulum, sürüm çakışması reddi, yalnız işaretli sürümün görünürlüğü.

## 8. Geri alma

- PR'ı geri alın. Yeni ayar alanları (`cloud_intake`, `cloud.intake_bucket`, `cloud.namespace`) eski sürümce yok sayılır.
- `pixi run storage cloud-intake off` bulut kabulünü durdurur; R2'deki hiçbir şey silinmez.
- `data/_cloud/` türetilmiş yansıdır; silinebilir (dizin yeniden kurulur, bulut sürümleri katalogdan çıkar).
- Hazırlık klasörü (`%LOCALAPPDATA%\THERMALIS\Cache\staging`) güvenle silinebilir; yarım kabul sonraki taramada baştan sürer.
