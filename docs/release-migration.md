# THERMALIS DATA — Yerel sürümlerin buluta taşınması (Faz 05 ön koşulları)

Durum: mekanizma hazır ve sentetik verilerle canlı provası yapıldı. **Gerçek veri taşınmadı.** Etkin depo `local`.

## 1. Ne yapar

`thermalis storage migrate-release <veri kimliği> <sürüm>` yerel, kayıtlı ve değişmez bir sürümü **yeniden kaydetmeden**,
bayt bayt `releases/<SEG>/<SEG>/<sürüm>/` altına yayımlar. Genel bir mekanizmadır; veri setine özel kod yoktur.

| Yayımlanan | Kaynak | Not |
|---|---|---|
| `payload/…` | yerel sürüm klasörü | `integrity.json`'daki tüm dosyalar + özetlenmeyen paket dosyaları |
| `records/version.json`, `quality.json`, `integrity.json`, `*.md` | yerel kayıt | bayt bayt aynı |
| `records/layer_facts.json` | sürüm kaydı; yoksa yerel GDB'den hesaplanır | türetilmiş önbellekle karşılaştırılır; uyuşmazsa taşıma durur |
| `records/quality_identifiers.json` | yerel GDB | belgelenmiş özel kayıtların GLOBALID / TANITIMKODU değerleri |
| `records/migration.json` | taşıma | kaynak, yerel tam SHA-256 doğrulaması, o anki kayıt sorunları, içerik özeti |
| `records/audits/<kimlik>/…` | `data/_audits/` | bu sürüme ait bağımsız denetim kayıtları (dosyalar olduğu gibi) |
| `records/semantic_verifications.jsonl` | yerel doğrulama günlüğü | bu veri setinin katmanlarına ait satırlar (zincir alanlarıyla; arşiv kopyası) |
| `records/reverification_history.jsonl` | `_ingest/verifications.jsonl` | yerel yeniden doğrulama geçmişi |
| `records/THERMALIS_AUTHORIZATION.json` | yetki kaydı | RESTRICTED / UNKNOWN için zorunlu |

- Erişim sınıfı **değişmez** (`migration.json → access_class_changed: false`).
- İçerik belirlenimcidir: aynı sürüm için her çalıştırmada aynı baytlar ve aynı işlem kimliği (`mig-…`). Kesilen taşıma aynı
  komutla güvenle sürer; ikinci çalıştırma `ALREADY_MIGRATED` döner. Bulutta farklı içerikli aynı sürüm varsa hiçbir şeyin
  üzerine yazılmaz (`version_already_in_cloud`).
- Commit işareti ancak tüm nesneler geri okunup SHA-256 ile doğrulandıktan sonra yazılır; ardından katalog girdisi.
- Yerel kayıt **yalnız okunur**; hiçbir dosyası değiştirilmez, taşınmaz, silinmez.

## 2. Geçiş öncesi dönem (çift kayıt)

Taşımadan sonra katalog yerel kaydı kullanmaya devam eder. Bulut kopyası `cloud_copies` tablosunda "doğrulanmış bulut kopyası"
olarak görünür (sorun değildir). Aynı sürüm farklı içerikle bulutta ise `DUPLICATE_ACROSS_BACKENDS` kayıt sorunu oluşur.

**Geçiş (sürüm başına, genel arka uç değişmez):** `thermalis storage set-authority <id> <sürüm> --source r2 --by … --reason …`
denklik kontrollerini (kayıt baytları, yük nesneleri ↔ yerel bütünlük, parmak izi, içerik kimlikleri, tam SHA-256 geri okuma)
geçerse bulut sürümünü yetkili yapar; yerel klasör `release_copies`'te ikincil kopya olarak kalır ve **değişmez**. Geri alma:
`--source local`. Kararlar bulut defterindedir (`state/ledger/authority/`). Ayrıntı: [data-hardening.md](data-hardening.md).

Bulut yansısında veri dosyası olmadığı için:
- içerik kimlikleri `records/layer_facts.json`'dan,
- özel kayıt kimlikleri (GLOBALID) `records/quality_identifiers.json`'dan okunur;
- analiz bileşenleri veriyi `POST /api/data/v1/materialize` ile **doğrulanmış önbellek kopyası** olarak alır
  (yalnız istenen varlık indirilir; her nesne commit'teki SHA-256 ile doğrulanır; disk boşluğu önceden denetlenir).
  Kısıtlı veride yalnız yerel bileşen anahtarıyla. Yerel sürümde kopya yapılmaz, yerel yol döner.
- Kullanıcı anlamsal doğrulamalarının yetkili kaydı yerel zincirli günlüktür ve içerik kimliğine bağlıdır; içerik kimlikleri
  korunduğu için geçişten sonra da geçerlidir. Yayındaki kopya arşiv amaçlıdır.

## 3. Komutlar

```
pixi run storage migrate-release <veri kimliği> <sürüm>                                   # kuru çalıştırma (ağ yok)
pixi run storage migrate-release <veri kimliği> <sürüm> --authorization yetki.json         # kısıtlı veri: yetki doğrulanır
pixi run storage migrate-release <veri kimliği> <sürüm> --authorization yetki.json --execute   # GERÇEK taşıma
pixi run storage compare-release <veri kimliği> <sürüm>                                   # bilimsel karşılaştırma raporu
```

`compare-release` bulut kopyasını yerel katalogdan bağımsız geçici bir köke yansıtır ve her katman için sürüm, içerik kimliği,
paket, kalite durumu, erişim, CRS, şema, zaman, katman, geometri, bulgular, özel kayıtlar (GLOBALID dahil), kapılar, anlamsal
sözlük, domain kodları ve kayıt sorunlarını karşılaştırır. Beklenen farklar ayrıca listelenir: depolama yeri, içerik kimliği
kaynağı, `RECORD_FILES_WRITABLE` (eski yerel kayıt yazılabilir; bulut yansısı salt okunur).

Canlı prova (yalnız sentetik, yalıtılmış `e2e-tests/<çalıştırma>/` ad alanı, geçici katalog):

```
$env:THERMALIS_R2_LIVE_TEST = "1"; pixi run r2-migration-rehearsal; Remove-Item Env:THERMALIS_R2_LIVE_TEST
```

## 4. HD01 için sıra (onay gerektirir; yapılmadı)

1. Veri sahibinden (İBB) imzalı izin → `docs/templates/cloud-authorization.example.json` biçiminde kayıt
   (`package_id = HD01_ADMIN_BOUNDARIES_v1.0.0-rc1`, `manifest_sha256 = 12162d6363bc0055d9e95a3e73af13bdd4119a4a0442e53643b4234ced22405b`,
   `access_class = RESTRICTED`, `storage.bucket = thermalis-data-dev`, kova konumu/yargı alanı izinde belirtilmeli).
2. `pixi run data verify THERMALIS_DATA/HD01_ADMIN_BOUNDARIES/ISTANBUL 1.0.0-rc1` → PASS; korunan klasörlerin SHA-256 anlık görüntüsü.
3. `migrate-release … --authorization …` (kuru) → planı inceleyin; ardından `--execute`.
4. `compare-release …` → `PASS`; beklenen sayılar: 39/979 kayıt, 6 boş KIMLIKNO (6 GLOBALID), 67 bulgu / 66 tekil kimlik.
5. Yerel sürüm klasörü ve gelen paket bayt bayt aynı mı (anlık görüntü karşılaştırması).
6. Geçiş (etkin deponun buluta çevrilmesi) ayrı bir onaydır; yerel kopya silinmez. Bucket Lock ayrı onaydır.

Bugünkü kuru çalıştırma sonucu: `REDDEDİLDİ authorization_required RESTRICTED` (beklenen; yetki kaydı yok).
