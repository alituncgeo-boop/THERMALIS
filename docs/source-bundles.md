# ZIP kaynak paketleri (source bundles) — kayıt, doğrudan yayın ve arşiv içi erişim

Bazı teslimler (HD06, HD08, HD10, HD12, HD14, HD26) THERMALIS paket sözleşmesi yerine bir **ZIP aktarım paketi**
olarak gelir:

    <paket>/<paket>.zip              özgün arşiv (deflate/stored); içinde MANIFEST.json (thermalis-transfer-manifest-v1),
                                     RELEASE_STATUS.json, PROVENANCE.json, LICENSE_AND_ACCESS.md, kaynak klasörleri
    <paket>/<paket>_ZIP_SHA256.txt   arşivin dış SHA-256'sı
    <paket>/*.md|*.json|*.txt|*.py   teslim belgeleri

Bağdaştırıcı geneldir (`data/bundle.py` + `data/formats/{zip_index,tabular}.py`); bilimsel anlam paket dışı beyandan
(`bundle` bölümü) ve paketin kendi kayıtlarından gelir.

## İlkeler

- Arşiv **çıkarılmadan** ve **bayt bayt** yayımlanır; teslim klasöründeki her dosya yayındadır. Kaynak değiştirilmez.
- Bütünlük: dış ZIP SHA-256 = yan dosya (akışla); her iç üye akışla açılıp iç manifestle (boyut + SHA-256) karşılaştırılır;
  manifestte olmayan üye reddedilir. Dış belgelerin özeti kayıt sırasında hesaplanır ve `checksum_origin:
  computed_at_registration` olarak etiketlenir (paketin kendi özeti olmadığı için).
- Beyan, **iç `MANIFEST.json`'un SHA-256'sına** bağlıdır (paket parmak izi).
- Keşif `/vsizip/` ile yapılır: File Geodatabase katmanları (geometri, sayım, CRS, kapsam, alan şeması, boş değer sayıları,
  domainler, içerik kimliği), GeoTIFF'ler (boyut, veri tipi, nodata, çözünürlük, gömülü birim/ölçek, yatay ve CRS'de
  gömülüyse dikey referans), beyan edilen tablolar (CSV profili). İç içe arşivler (ör. özgün indirme ZIP'leri) açılmaz;
  üye olarak doğrulanır ve kaydedilir.
- CSV profili değer DÖNÜŞTÜRMEZ: ayırıcı, BOM, bölümler (boş satırla ayrılmış metadata/veri blokları), başlıkta beyanlı
  birim (yalnız kısa birim biçimli parantez içi ifade; etiketler birim sayılmaz), sayısal aralık, ISO 8601 zaman
  etiketlerinin ilk/son/ayırt edici/yinelenen sayıları — etiketler dosyadaki gibi, saat dilimi eklenmeden.
- Zaman kapsamı **anlamıyla** kaydedilir (`observation_period`, `source_acquisition_span`, `reference_period`,
  `validity_period`, `unknown`); pasaport geçerlilik tarihleriyle karıştırılmaz. Kaynakta tarih yoksa `unknown`.
- Kalite: kaynak QA durumu (`RELEASE_STATUS.json`) olduğu gibi korunur; THERMALIS ekseni türetilir, yükseltilmez.
- Paketin kökteki küçük belgeleri (`*.json|*.md|*.csv|*.txt`, ≤ 2 MB) `records/package/` altına bayt bayt kopyalanır:
  bulut yansısında okunabilir ve katalog yalnız kayıtlardan yeniden kurulabilir.

## Katalog

Her GDB katmanı, raster ve tablo ayrı bir katman (veri seti) olarak görünür; kapsayıcı varlık `<arşiv>/<üye>` yoludur
(`storage.containers`). Tanımlayıcıda `scientific` bloğu raster/tablo özelliklerini, alan birimlerini ve zaman kapsamını
verir. Bulutta okuma yolu: `/vsizip/{/vsis3/<kova>/<…>/payload/<arşiv>.zip}/<üye>`.

## Erişim (malzemeleştirme)

`POST /api/data/v1/materialize {id, version}`: kapsayıcı (ör. `.gdb` klasörü veya tek bir `.tif`) için yalnız ilgili
üyelerin **sıkıştırılmış veri aralığı** (`data_offset`, `compressed_size`) R2'den okunur, yerelde açılır (sınırlı bellek)
ve açılmış içerik iç manifestle doğrulanmış SHA-256 ile karşılaştırılmadan önbelleğe girmez.

## Erişim sınıfı ve yetki

Beyan erişim sınıfını kaydeder ama yetki VERMEZ. `RESTRICTED`/`UNKNOWN` veri için bulut yayını, mevcut kural gereği
geçerli bir yetki ister: ya açık `THERMALIS_AUTHORIZATION.json` dosyası (`--authorization`) ya da sürümü açıkça kapsayan
merkezî operatör yetkisi (`storage grant`; bkz. [data-hardening.md](data-hardening.md)). Belge referansı isteğe bağlıdır;
operatör beyanı geçerli bir kanıt türüdür.
Paket içindeki "USER_ATTESTED_AUTHORIZATION" beyanları kontrollü iç aktarımı kapsar; bulut yetkisi yerine geçmez.
