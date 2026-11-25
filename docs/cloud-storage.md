# THERMALIS DATA — Bulut depolama temeli (Faz 02 + Faz 03)

Durum: **uygulama yerel depoyla çalışır** (`storage_backend = "local"`). R2 arka ucu kodda hazırdır,
ancak açık geçiş onayına kadar uygulamanın veri deposu olarak kullanılmaz. Mevcut veri taşınmaz.

## 1. Mimari

```
src/thermalis/objectstore/
  base.py         ObjectStore arayüzü, ObjectInfo, Capabilities, CancelToken, doğrulama (metadata ≠ tam)
  keys.py         anahtar doğrulama (yol geçişi, Windows ayrılmış adları, sürücü harfi, denetim karakteri)
  errors.py       yapılandırılmış hatalar (gizli bilgi içermez)
  local.py        LocalBackend — varsayılan; mevcut veri kökünü olduğu gibi okur
  s3.py           S3Backend — R2 (koşullu yazım, çok parçalı, sürdürme, belirsiz yazım uzlaştırması)
  retry.py        sınırlı yeniden deneme: üstel geri çekilme + tam jitter, iptal edilebilir
  multipart.py    kalıcı çok parçalı yükleme durumu (yapılandırma klasöründe; gizli bilgi yok)
  cache.py        SHA-256 adresli, sınırlı (varsayılan 20 GB), LRU, kiralama korumalı önbellek
  credentials.py  OS kimlik deposu (Windows Credential Manager, keyring), maskeleme
  publication.py  değişmez sürüm yayın protokolü (_COMMIT.json yetkili kayıttır)
  factory.py      ayarlardan bulut deposu/önbellek üretimi
  smoke.py        elle çalıştırılan canlı R2 duman testi (sentetik)
```

Arayüz bir dosya sistemi taklidi değildir: klasör, yeniden adlandırma, sona ekleme ve **silme yoktur**.
Her yazım ya yalnız-oluştur (`If-None-Match: *`) ya da koşullu güncellemedir (`If-Match: <etag>`);
koşulsuz yazım reddedilir. ETag yalnız sürüm doğrulayıcısıdır; içerik bütünlüğü SHA-256 ile gösterilir.

Faz 02'de depo arayüzünü kullanan mevcut yollar (davranış birebir aynı):
- kayıtlı sürümün yeniden doğrulaması (`data verify`, `/api/data/verify`) → `integrity.verify_store`
- sürüm kayıtlarının keşfi (katalog listesi ve dizin kurulumu) → `catalog.version_record_folders`

Kayıt (staging, GDAL incelemesi, tek adımda yayın) bilinçli olarak yerel kaldı: bunlar yerel geçici
dosya sistemi gerektirir ve buluta yayın Faz 04'te yeni protokolle yapılacaktır.

## 2. Yayın protokolü (bulut, Faz 04'te etkinleşecek)

```
releases/<SEG>/<SEG>/<sürüm>/payload/...            yalnız-oluştur
releases/<SEG>/<SEG>/<sürüm>/records/{version,quality,integrity}.json
releases/<SEG>/<SEG>/<sürüm>/_COMMIT.json           yalnız-oluştur, en son
state/datasets/<SEG>/<SEG>/releases.json            türetilmiş işaretçi (yetkili değil)
```

- Yetkili kayıt `_COMMIT.json`'dır; her nesnenin anahtarını, boyutunu, SHA-256'sını listeler.
- İşaret ancak tüm nesneler **baştan sona okunarak** doğrulandıktan sonra yazılır.
- İşareti olmayan önek bir sürüm değildir (yarım yayın görünmez).
- Kurtarma: aynı `operation_id` ile yeniden çalıştırma. Başka işlem kimliği → çakışma (üzerine yazma yok).
- Dizin yeniden kurulumu: `committed_releases()` → işaretli sürümlerin `records/*.json` dosyaları.
- Varsayılan politika yayını kapalı tutar; açıkken yalnız `SYNTHETIC` erişim sınıfı kabul edilir.
  `RESTRICTED` veri (HD01 dahil) bu kovaya hiçbir koşulda yazılmaz.

## 3. Kimlik bilgisi kurulumu (canlı test onaylandığında)

Ekran görüntüsünde görünen geliştirme anahtarı **ele geçirilmiş sayılır**; kullanılmamalı ve Cloudflare'de iptal edilmelidir.

1. Cloudflare panosu → R2 → *Manage R2 API Tokens* → eski anahtarı **Revoke**.
2. Yeni token: izin *Object Read & Write*, kapsam **yalnız** `thermalis-data-dev` kovası, mümkünse kısa süreli (TTL).
   Not: bu tür token önek (prefix) kısıtlaması sağlamaz; `uploader` ve `publisher` ayrımı ileride ayrı kovalar
   veya öneke kısıtlı geçici kimlik bilgileriyle yapılacaktır.
3. Uç noktayı ve kovayı kaydedin (gizli değer değildir; ayar dosyasına yazılır):

   ```
   pixi run storage configure-cloud --endpoint https://<HESAP_KIMLIGI>.r2.cloudflarestorage.com --bucket thermalis-data-dev
   ```

4. Anahtarı **Windows Credential Manager**'a girin. Değerler yankısız sorulur; komut satırında görünmez:

   ```
   pixi run storage credentials set --role publisher
   ```

5. Kontrol (değerler gösterilmez): `pixi run storage status`
6. Kaldırma: `pixi run storage credentials clear --role publisher`
   (Windows: *Credential Manager → Windows Credentials → THERMALIS-ObjectStore* altında da görünür.)

Gizli değerleri sohbete, ayar dosyasına, ortam değişkenine veya komut satırı argümanına yazmayın.

## 4. Canlı R2 duman testi (elle)

Yalnız sentetik rastgele baytlar, benzersiz `smoke-tests/<zaman>-<rastgele>/` öneki altında.
Uygulama silme sunmadığı için test nesneleri kalır; panodan `smoke-tests/` önekine 1 günlük bir
yaşam döngüsü (Object lifecycle → delete after 1 day) kuralı eklemeniz önerilir.

PowerShell:

```
$env:THERMALIS_R2_LIVE_TEST = "1"; pixi run r2-smoke; Remove-Item Env:THERMALIS_R2_LIVE_TEST
```

Denetlenen adımlar: yalnız-oluştur yazım, 412 çakışması, belirsiz yazım uzlaştırması, If-Match güncellemesi
ve eski ETag'in reddi, akış + aralık okuma, sayfalı listeleme, çok parçalı yükleme + tam SHA-256 geri okuma,
çok parçalı tamamlama çakışması (412), yayın protokolü (işaretli sürüm görünür, işaretsiz görünmez).

## 5. Önbellek

Varsayılan konum kullanıcı önbellek klasörü (`%LOCALAPPDATA%\THERMALIS\Cache\objects`), sınır 20 GB.
Veri kökü veya gelen paket klasörüyle çakışan önbellek konumu reddedilir. Önbellek her zaman silinebilir;
içerik depodan yeniden getirilir. Ayarlar: `settings.json → cache.cache_dir`, `cache.max_bytes`.

## 6. Geri alma

- Kod: PR'ı geri alın (`git revert <birleştirme commit'i>`). Veri kökü ve kayıt biçimleri değişmedi.
- Ayarlar: yeni alanlar (`storage_backend`, `cloud`, `cache`) eski sürüm tarafından yok sayılır.
- Kimlik bilgisi: `pixi run storage credentials clear --role <rol>`.
- Önbellek ve yükleme durumu silinebilir: önbellek klasörü ve `%LOCALAPPDATA%\THERMALIS\upload-state`.

## 7. Bilinen sınırlar

- Otomatik testler R2'yi taklit eden bir istemciyle ve gerçek botocore istek serileştirmesiyle çalışır;
  gerçek R2 davranışını yalnız canlı duman testi gösterir (henüz çalıştırılmadı).
- Uzun süreli Bucket Lock kuralı kurulmadı (sahip onayı gerekir); saklama kuralları ayrıca denenmelidir.
- `uploader`/`publisher` ayrımı bugün yalnız kimlik deposu girdisi düzeyindedir; normal R2 token'ı
  önek kısıtlaması sağlamaz.
- GDAL `/vsis3/` erişimi (`gdal_vsis3_session`) canlı ortamda denenmedi.
