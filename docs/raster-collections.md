# Raster koleksiyonları (TAR / COG / GeoTIFF) — kayıt, doğrudan yayın ve aralıklı erişim

Bu belge, mevcut THERMALIS paket sözleşmesini (``PACKAGE/RELEASE_STATUS.json`` + File Geodatabase) izlemeyen,
büyük raster kaynak paketlerinin (ör. HD02 Landsat L2SP, 81 TAR, 62 GB) nasıl kaydedildiğini anlatır.
Mekanizma geneldir; ürüne özgü tek parça, ayrı bir metadata yorumlayıcısıdır.

## İlkeler

- **Kaynak paket değiştirilmez.** Çıkarma, yeniden adlandırma (ör. ``" (3).tar"`` son ekleri), yeniden örnekleme,
  sıcaklık dönüşümü yapılmaz. Paketin tüm dosyaları (81 TAR + belgeler) bayt bayt yayımlanır.
- **Yerel tam kopya yok.** Kaynak klasör yerinde okunur; yerelde yalnız küçük JSON kayıt/günlük dosyaları yazılır
  (çalışma klasörü; koleksiyon boyutundan bağımsız, sahne başına ~75 KB).
- **Değerler kaynakta olduğu gibi.** Ölçek/kaydırma yalnız kaynak metadata'da (MTL) açıkça yazılmışsa ve kaynak
  anahtarıyla kaydedilir; **uygulanmaz**. Yazılmamışsa ``null`` + ``not_declared_in_source_metadata``. Birim MTL'de
  beyan edilmez: ``null``.
- **Kalite yükseltilmez.** THERMALIS kalite ekseni paketin kaynak QA durumundan türetilir (ör.
  ``SOURCE_QA_PASS_WITH_DOCUMENTED_SPATIAL_LIMITATIONS`` → ``QA_CONDITIONAL``); kaynak durum ve belgelenmiş sınırlamalar
  aynen ``quality.json``'da saklanır. Beyan kalite durumu belirleyemez.
- **Boyut sınırı yok.** 20 GB önbellek bir veri seti kabul sınırı değildir; yayın ve dizin önbelleği kullanmaz.

## Bileşenler

| Modül | Görev |
|---|---|
| `data/formats/tar_index.py` | Sıkıştırılmamış TAR: tek geçişte arşiv SHA-256 + üye konumu + üye SHA-256 (ustar/GNU/PAX; sınırlı bellek) |
| `data/formats/raster.py` | GDAL başlık incelemesi (sürücü, boyut, veri tipi, nodata, gömülü ölçek, CRS, çözünürlük, kapsam, COG düzeni) |
| `data/formats/preflight.py` | Yalnız dosya listesinden biçim ön denetimi (indirme yok) |
| `data/formats/landsat.py` | Landsat C2 L2 MTL/STAC **yorumlayıcısı** (genel bağdaştırıcıdan ayrı) |
| `data/declaration.py` | Paket dışı, manifest özetine bağlı kayıt beyanı |
| `data/collection.py` | Koleksiyon dizini + kayıtlar (`version.json`, `quality.json`, `integrity.json`, `collection.json`) |
| `cloud/direct.py` | Kaynak klasöründen doğrudan yayın (plan → talep → yayın → katalog) |
| `cloud/claims.py` | Yenilenebilir bulut talebi + kalp atışlı yerel kilit |
| `cloud/access.py` | `/vsisubfile/`+`/vsis3/` okuma yolu; tek üyenin aralıklı, doğrulanmış malzemeleştirilmesi |

## Paket dışı beyan

Beyan, eksik kayıt kararlarını (veri kimliği, sürüm, erişim sınıfı, kaynak atfı, QA durumunun paketteki yeri)
verir ve ``PACKAGE/MANIFEST.json``'un SHA-256'sına bağlanır. Konum: ``<veri kökü>/data/_declarations/<paket>.json``.
Paket değişirse beyan geçersizdir (``declaration_does_not_match_package``). Beyan yalnız-oluştur yazılır.

Örnek alanlar: [`templates/HD02_LANDSAT_LST.declaration-spec.json`](templates/HD02_LANDSAT_LST.declaration-spec.json).

## Doğrudan yayın akışı

1. **Plan (ağsız):** manifest (varlık + boyut; listede olmayan dosya reddedilir) → beyan bağı → ön denetim →
   her dosya bir kez akışla okunur (SHA-256 manifestle; TAR'da aynı geçişte üye dizini) → raster başlıkları
   ``/vsisubfile/`` ile → paket sahne kataloğu ile çapraz denetim (fark **raporlanır**, düzeltilmez).
   Sonuçlar dosya başına günlüğe yazılır; yeniden başlatmada değişmemiş dosya (boyut + zaman) yeniden okunmaz.
   Kayıtlar çalışma klasöründe **sabitlenir**: yeniden başlatmada aynı baytlar yayımlanır.
2. **Talep:** ``state/publish/<SEG>/<SEG>/<sürüm>.json`` (yalnız-oluştur). Kalp atışı (`heartbeat_at`) 5 dakikada bir
   koşullu güncellenir; süre aşımı son kalp atışından ölçülür. Aynı bilgisayarda ikinci süreç yerel kilitle durur.
3. **Yayın:** zaten aynı işlemle yazılmış nesne yeniden yüklenmez; çok parçalı yüklemeler parça düzeyinde sürer;
   her nesne R2'den baştan sona okunup SHA-256 ile doğrulanır ve doğrulama günlüğe yazılır (yeniden başlatmada
   doğrulanmış nesneler yeniden okunmaz). ``_COMMIT.json`` en son yazılır.
4. **Katalog:** ``catalog/commits/...`` girdisi, sürüm işaretçisi, talep sonucu (ACCEPTED), yerel katalog eşitlemesi
   (yalnız kayıtlar; veri indirilmez).

Kesinti: aynı komut yeniden çalıştırılır (belirlenimci işlem kimliği ``col-<özet>``). Yarım yayın görünmez.

## Dizin ve sözleşme

SQLite dizini (şema 2.3): ``collection_items`` (sahne: tarih, ürün kimliği, uydu/sensör, path/row, bulut, işlem düzeyi,
kapsam, paket QA durumu, sorunlar) ve ``collection_assets`` (üye: konum, boyut, SHA-256, rol, bant kodu, veri tipi,
boyutlar, EPSG, çözünürlük, kapsam, nodata, gömülü ölçek, kaynak beyanlı ölçek/kaydırma ve kaynağı, birim durumu,
sıkıştırma, COG, genel bakış sayısı, QA bit alanı varlığı).

- ``GET /api/data/v1/collection?id=<katman>&version=&start=&end=&platform=&max_cloud=``
- ``GET /api/data/v1/collection/item?id=<katman>&item=<ürün kimliği>`` — varlıklar ve okuma yolu
- ``POST /api/data/v1/materialize {"id", "version", "item", "asset"}`` — tek üyenin doğrulanmış önbellek kopyası

Katman kimliği: ``<dataset_id>/<layer_name>`` (HD02: ``THERMALIS_DATA/HD02_LANDSAT_LST/ISTANBUL_180031/SCENES``).

## Aralıklı (range) erişim

Arşiv içindeki COG, arşiv indirilmeden okunur:

    /vsisubfile/<üye_konumu>_<üye_boyutu>,/vsis3/<kova>/<ad_alanı>releases/.../payload/SOURCE/<yıl>/<arşiv>.tar

GDAL kimlik bilgisi yalnız bu kova yoluna özel ve bellekte verilir (``remote_raster_session``; ortam değişkeni yok).
Tek üyenin yerel kopyası gerekiyorsa yalnız ``[konum, konum+boyut)`` aralığı ``Range`` istekleriyle indirilir ve üye
SHA-256'sı kayıtla doğrulanmadan önbelleğe girmez.

## Bulut kabulü ve izleyici

- Bulut kabulü paketi **indirmeden ve talep almadan önce** işaretteki dosya listesiyle ön denetler. TAR/GeoTIFF
  koleksiyonu ``raster_collection_direct_publication``, desteklenmeyen biçim (netCDF/HDF5 sürücüsü yok, sıkıştırılmış
  TAR, ZIP, Zarr…) ``adapter_required`` olarak **ertelenir** (``ADAPTER_REQUIRED``; kalıcı RED yok, paket bulutta kalır).
  Mevcut kayıt iş akışındaki biçim kapısı (S04) da artık ertelemedir.
- Tarama kilidi kalp atışlıdır (30 dk sessizlikten sonra çökmüş sayılır); bulut talepleri yenilenebilir.
- İzleyici iki iş parçacığıdır: bulut taraması (uzun indirme/yayın) yerel klasör taramasını bloklamaz.
- Yerel gelen klasördeki manifestli TAR koleksiyonu ``DIRECT_PUBLICATION`` (``ADAPTER_REQUIRED``) olarak görünür;
  beyanın olup olmadığı ayrıntıda yazar. Otomatik kayıt yapılmaz.

## Komutlar

    pixi run storage declare-collection --source <paket> --spec <beyan-alanları.json>
    pixi run storage publish-collection --source <paket>                # kuru çalıştırma (plan + tam SHA-256 dizini)
    pixi run storage publish-collection --source <paket> --execute      # yayın (kesilirse aynı komutla sürer)
    THERMALIS_R2_LIVE_TEST=1 pixi run storage collection-acceptance --source <paket> --spec <...> \
        --archive SOURCE/<yıl>/<arşiv>.tar --confirm-live              # canlı kabul (e2e-tests/ ad alanı)
