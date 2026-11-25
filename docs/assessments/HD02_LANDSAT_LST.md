# HD02_LANDSAT_LST — Mevcut uygulamanın uygunluk değerlendirmesi (salt okunur)

Kapsam: ~58 GB ZIP olarak teslim edilecek Landsat yer yüzeyi sıcaklığı (LST) paketi. Paket bu bilgisayarda yok; değerlendirme
koddan (main `d0cc0a2` + Faz 05 dalı) ve bu ortamın ölçümlerinden yapıldı. Kod değiştirilmedi. **[D]** = kodda/ortamda doğrulandı,
**[K]** = sağlayıcı bilgisi, panodan/belgeden teyit edilmeli, **[Ö]** = öneri.

Önemli ilke: 20 GB önbellek bir veri seti boyut sınırı değildir; yerelde aynı anda tutulan çalışma kümesinin sınırıdır.

## 1. Bulgular

### Arşiv (ZIP) desteği
- **[D]** ZIP desteklenmiyor. Gelen klasörde tek başına `.zip` → `UNSUPPORTED_INPUT` (açılmaz, taşınmaz, silinmez).
  Paket içindeki `.zip` bir veri dosyası olarak hiçbir adaptörce tanınmaz → kayıt `ADAPTER_REQUIRED` (`format_not_supported`).
- **[D]** Yükleyici paket KLASÖRÜ bekler (`PACKAGE/MANIFEST.json` …). 58 GB ZIP'in ikinci bilgisayarda açılması gerekir:
  ZIP + açılmış kopya için en az ~58 GB + açılmış boyut kadar yer (Landsat GeoTIFF'leri zaten sıkıştırılmış olabilir;
  açılmış boyut paket belgesinden doğrulanmalı).
- **[D]** GDAL `/vsizip/` mevcut; teoride ZIP içindeki GeoTIFF'ler açmadan okunabilir, ancak bu yol uygulamada kullanılmıyor.

### Bilimsel format adaptörleri
- **[D]** Tek kayıtlı adaptör: Esri File Geodatabase (vektör). Raster (GeoTIFF/COG), NetCDF, HDF, Zarr adaptörü yok.
- **[D]** Bu ortamdaki GDAL 3.13.3: GTiff ✓, COG ✓, `/vsizip/` ✓, **netCDF ✗, HDF5 ✗**. LST ürünü NetCDF/HDF ise ek sürücü gerekir.
- **[D]** İçerik kimliği (`lc1-sha256-multiset`) yalnız vektör katmanları için tanımlı; raster için içerik kimliği yok.
- **[D]** Anlamsal sözlükte `scale`, `offset`, `valid_min/max` alanları var, ancak raster bant semantiği (ör. ST_B10 ölçek
  0.00341802, ofset 149.0 K — **[K]** USGS L2 ürün belgesinden doğrulanmalı), QA bantları, bulut maskesi tanımlı değil.

### Yükleme (ikinci bilgisayar)
- **[D]** Çok parçalı yükleme: 64 MiB parça, en çok 10 000 parça; büyük dosyada parça boyutu kendiliğinden büyür (tek dosya
  sınırı pratikte R2'nin nesne sınırı: **[K]** ~5 TiB). Kesilen dosya parçadan sürer; tamamlanmış dosyalar yeniden gönderilmez.
- **[D]** Aktarım tek akışlı (paralel parça yok): hız tek bağlantıyla sınırlı. 58 GB ≈ 1,3 sa (100 Mbit/s), ≈ 6,5 sa (20 Mbit/s).
- **[D]** Yerel okuma: önce tam SHA-256 (1 okuma), sonra yükleme (1 okuma).
- **[D]** Yükleyici format ön denetimi yapmıyor: desteklenmeyen biçimli 58 GB paket yüklenir, kabulde başarısız olur.

### Bulut kabulü (bu bilgisayar)
- **[D] Kritik:** kabul, paketin TAMAMINI hazırlık alanına indirdikten sonra mevcut kayıt iş akışını çalıştırır. Biçim
  desteklenmiyorsa (ADAPTER_REQUIRED) bu, 58 GB indirmeden sonra anlaşılır ve R2'de **kalıcı RED** yazılır. Adaptör sonradan
  eklense bile aynı yükleme yeniden işlenmez; paketin yeni bir kimlikle yeniden yüklenmesi gerekir.
- **[D]** Hazırlık alanı: en çok ~2 × paket boyutu + 256 MiB (indirilen kopya + kayıt kopyası). Varsayılan sınır 200 GiB.
  Bu bilgisayarda tek sürücü var: **C: 150,5 GB boş**. Açılmış paket ~75 GB'ı aşarsa kabul ERTELENİR (reddedilmez).
- **[D]** Ağ trafiği yaklaşık 3 × paket: indirme (kabul kovasından) + yayın yüklemesi + tam geri okuma doğrulaması.
  100 Mbit/s'de 58 GB için ~4 saat × 3 ≈ 12 saat.
- **[D]** Talep süresi ve yerel tarama kilidinin "eski" sayılma eşiği 12 saat (`claim_ttl_hours`). ~12 saati aşan bir kabul,
  ikinci bir yerel süreç (ör. komut satırı) tarafından eski sayılıp kilidi kaldırılabilir. Kalp atışı (heartbeat) yok.
- **[D]** Bulut taraması yerel klasör izleyicisiyle aynı iş parçacığında: uzun bir kabul süresince yerel paket taraması bekler.
- **[D]** Windows yol sınırı: çok uzun Landsat dosya adları + derin klasörler hazırlıkta 250 karakteri aşarsa kabul ertelenir
  (`staging_path_too_long`); kısa bir `cloud_intake.staging_dir` gerekir.

### Önbellek ve yerel erişim
- **[D]** Önbellek nesne başına çalışır: tek dosya 20 GB'tan büyükse önbelleğe alınamaz (`cache_capacity_exceeded`).
- **[D]** `materialize` bir varlığın TÜM dosyalarını indirir; varlık 20 GB'ı aşarsa reddedilir (tasarım gereği).
  Büyük rasterlar için doğru yol aralıklı okumadır (COG + `/vsis3/`); `gdal_vsis3_session` kodda var ama erişim
  sözleşmesine bağlı değil ve canlı denenmedi.
- **[D]** Disk boşluğu malzemeleştirmeden önce denetleniyor.

## 2. Öneriler (uygulanmadı; ayrı onayla)

1. **[Ö]** Kabulde **biçim ön denetimi**: işaretteki dosya listesi `adapters.classify_paths` ile indirmeden önce sınıflansın;
   desteklenmiyorsa indirme yapılmadan `ADAPTER_REQUIRED` olarak **ertelensin** (kalıcı RED değil). Yükleyici de aynı denetimi
   yapıp uyarsın.
2. **[Ö]** HD02 için ZIP yerine açılmış paket klasörü teslimi; mümkünse GeoTIFF'lerin önceden COG'a dönüştürülmüş hâli
   (bilimsel değer korunarak; dönüşüm ayrı ve belgelenmiş bir veri hazırlama adımıdır).
3. **[Ö]** Raster (GeoTIFF/COG) adaptörü: salt okunur açılış, bant/veri tipi/NoData/ölçek-ofset, CRS, kapsam, piksel boyutu,
   raster içerik kimliği (blok bazlı akış özeti), QA bantları. Gerçek HD02 verisiyle doğrulanmadan listeye girmemeli.
4. **[Ö]** Büyük paket kabulü: hazırlık alanını geniş bir sürücüye almak; kayıt kopyasında sabit bağlantı (aynı birimde ikinci
   kopya yerine); yayında commit'teki SHA-256 ile sunucu tarafı kopyalama ve tek doğrulama; talep/kilit için kalp atışı;
   bulut taramasını ayrı iş parçacığına almak.
5. **[Ö]** Paralel parça yüklemesi (ör. 4 eşzamanlı parça) ve yükleyicide biçim/alan ön denetimi.
6. **[Ö]** Analiz erişimi: COG için `/vsis3/` aralıklı okuma sözleşmesi (kimlik bilgisi yalnız bellekte); 20 GB önbellek yalnız
   sık kullanılan parçalar için.
7. **[Ö]** NetCDF/HDF gerekiyorsa GDAL netCDF/HDF5 sürücülerinin ortama eklenmesi (sürüm sabitlenerek).

## 3. Bugünkü cevap

Mevcut uygulama HD02'yi **olduğu gibi kabul edemez**: ZIP ve raster biçimleri desteklenmiyor ve kabul, desteklenmeyen paketi
58 GB indirdikten sonra kalıcı olarak reddeder. Depolama ve yükleme katmanı (çok parçalı, sürdürülebilir, SHA-256) bu boyutu
taşıyabilir; eksik olan, biçim ön denetimi, raster adaptörü ve büyük paket kabulünün disk/süre davranışıdır.
