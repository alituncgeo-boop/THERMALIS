# THERMALIS DATA — bilimsel ve işletimsel sağlamlaştırma

Bu belge, değişmez yayınları (commit'li sürümler) **hiç değiştirmeden** onların üzerine eklenen kalıcı, denetlenebilir
kayıtları anlatır. Hiçbir yük (payload) yeniden yüklenmez; hiçbir commit'li kayıt üzerine yazılmaz.

## 1. Bulut defteri (`state/ledger/`)

| Akış | İçerik |
| --- | --- |
| `metadata_revisions` | metadata düzeltmeleri (gerekçe + kanıt + önceki değer) |
| `authorizations` | operatör yetkileri ve geri almaları |
| `authority` | sürüm başına yetkili kaynak kararları (r2 / local; geri alma dahil) |
| `semantic_verifications` | anlamsal doğrulama zinciri (yerel `_semantic/verifications.jsonl` satırları aynen) |
| `reverifications` | yeniden doğrulama geçmişi (`_ingest/verifications.jsonl`) |

- Her girdi **yalnız-oluştur** yazılır; kimlik içerikten türetilir (SHA-256). Aynı kimlik + farklı içerik = çakışma,
  üzerine yazılmaz. İçeriği sonradan değiştirilmiş yerel girdi (kimlik ≠ içerik özeti) uygulanmaz.
- Yerel yansı: `<veri kökü>/data/_cloud/ledger/<akış>/` (türetilmiş). Çevrimdışı yazılanlar `_pending/` kuyruğunda bekler.
- `storage cloud-scan` / katalog eşitlemesi defteri de eşitler: yerel günlükleri deftere aktarır, bekleyenleri iter,
  buluttakileri çeker, eksik yerel günlük satırlarını geri kurar. Anlamsal zincir yalnız yerel zincir defterin öneki ise
  ve her girdinin `prev_hash`/`entry_hash` denetimi geçerse eklenir; ayrışma raporlanır ve yerel günlüğe dokunulmaz.
- `storage ledger-sync` aynı işi tek başına yapar.

Böylece boş bir veri kökü yalnız `cloud-scan` (tam eşitleme) ile kataloğu, içerik kimliklerini, GLOBALID kayıtlarını,
metadata düzeltmelerini, yetkileri, yetkili kaynak kararlarını ve doğrulama geçmişini **yük indirmeden** yeniden kurar.

## 2. Operatör yetkisi (`storage grant`)

```
thermalis storage grant --spec <yetki.json>     # bir kez; sonraki yayın/taşıma/kabul yeniden onay istemez
thermalis storage grants                        # durumlar: active / revoked / expired / tampered
thermalis storage revoke <grant-id> --by … --reason …
```

- Kapsam **açıktır**: her sürüm `dataset_id` + `version` (+ isteğe bağlı `manifest_sha256`, sürüme özgü `conditions`). Joker yok.
  Kapsamı genişletmek yeni bir yetki kaydıdır; mevcut kayıt değiştirilemez (kimlik içerikten türetilir).
- İşlemler: `cloud_storage`, `transfer`, `thermalis_data_registration`, `scientific_analysis_internal`; hedef: sağlayıcı + kova.
- Kanıt türü: `operator_attestation` (belge referansı gerekmez) veya `institutional_document` (referans zorunlu).
- Yetki erişim sınıfını **değiştirmez** (RESTRICTED kalır). Kovalar özel kalır. Kamuya açma veya yeniden dağıtım kapsamı yoktur.
- Yayın, taşıma ve bulut kabulü açık `--authorization` dosyası verilmezse kapsayan etkin yetkiyi kullanır ve sürüme bağlı
  `records/THERMALIS_AUTHORIZATION.json`'u ondan türetir (`grant_id`, `evidence_type` ile). Eski kayıtlar aynen geçerlidir.

## 3. Metadata düzeltmesi (`storage revise-metadata`)

```
thermalis storage revise-metadata <id> <sürüm> --spec <düzeltme.json> [--target auto|r2|local]
{"changes": [{"record": "version.json", "path": "layers.X.raster.vertical_reference", "op": "set", "value": {...}}],
 "reason": "...", "evidence": [{"source": "package/…", "detail": "…"}], "created_by": "…"}
```

- Temel kayda (commit işlem kimliği veya kayıt kimliği) bağlıdır; başka temele uygulanmaz.
- Korunan yollar değiştirilemez: `dataset_id`, `version`, `registration`, `source.package_fingerprint`, `access.classification`.
- Katalog dizini "etkin kayıt = commit'li kayıt + düzeltmeler" ile kurulur; API'ler (`descriptor.metadata_revisions`,
  `dataset` görünümü) uygulanan düzeltmeleri listeler.

## 4. Sürüm başına yetkili kaynak (`storage set-authority`)

Genel depo arka ucu (`storage_backend`) değişmez. Bir sürüm için `--source r2` ancak şu denklik kontrolleri geçerse kaydedilir:

| Kontrol | İçerik |
| --- | --- |
| EQ-01/02 | yerel kayıt ve bulut yansısı mevcut, ikisi de REGISTERED |
| EQ-03 | `version.json`, `quality.json`, `integrity.json` bayt bayt aynı |
| EQ-04 | paket parmak izi (MANIFEST SHA-256) aynı |
| EQ-05 | işaretteki her yük nesnesi = yerel bütünlük listesi (yol + boyut + SHA-256); paket denetim dosyaları yerel dosyayla aynı |
| EQ-06 | işaret `full_sha256_read_back` ile yazılmış |
| EQ-07 | katman içerik kimlikleri (bulut `layer_facts.json`) = yerel kopyadan türetilenler |
| EQ-08 | buluttaki her nesne baştan sona yeniden okunur (tam SHA-256) |

Yerel klasör silinmez, taşınmaz, değiştirilmez; katalogda `release_copies` içinde ikincil kopya olarak görünür. Yansı eksik
veya farklıysa katalog yerel kayda döner ve `AUTHORITY_SOURCE_UNAVAILABLE` kayıt sorunu oluşur. Geri alma `--source local`.

## 5. Zaman metadata modeli

Her tarih anlamıyla ayrı bir girdidir (`temporal_entries`): `observation_period`, `acquisition_date`, `acquisition_span`,
`source_reference_date`, `demographic_reference_year`, `administrative_geography_vintage`, `survey_date`,
`product_generation_date`, `publication_date`, `validity_period`. Süreklilik: `continuous`, `discontinuous`,
`selected_acquisitions`, `single_date`, `unknown`. Durum: `SOURCE_DATA`, `SOURCE_DECLARED`, `INFERRED_CORROBORATED`, `UNKNOWN`.
Kayıt tarihi asla kaynak tarihi yerine konmaz. Eski `temporal_coverage` beyanları ve HD02 koleksiyonları (`first_acquisition`
→ `acquisition_date`, `selected_acquisitions`) otomatik eşlenir. Katalog kartı, ayrıntı görünümü, `descriptor.temporal.entries`
ve `access.temporal` bunları gösterir.

## 6. GDB domainleri

Kaynak GDB'den: kodlu / aralık / desen domainleri, alan türü, aralık sınırları, bölme/birleştirme politikaları, alan varsayılanı
ve NULL olabilirlik. Kodlu domainli alanlarda tanımsız kodlar **sayılır ve raporlanır, anlam verilmez**
(`coded_value_assessment`). `semantic_status = EXTRACTED_NOT_SCIENTIFICALLY_VERIFIED`: kaynaktan çıkarılmış tanım, bilimsel
olarak doğrulanmış anlam değildir. HD01 tipi paket beyanları (`CODED_DOMAINS.json`, GDB ile birebir: DOM-01) uyumlu kalır.

## 7. Düşey referans ve gözlem kapsamı

- Raster yükseklik ürünlerinde `vertical_reference`: `EMBEDDED` (dosyada gömülü), `INHERITED_DOCUMENTED` (işlem kayıtlarıyla
  belgelenmiş devralma), `UNKNOWN`. Datum, EPSG, yükseklik türü, birim, yüzey modeli (DSM/DTM) ve bağımsız doğruluk durumu ayrı
  tutulur. Varsayılan datum atanmaz.
- Katmanlarda `data_availability`: referans coğrafya, birleştirme anahtarı, kapsam sayımları, `missing_semantics =
  NULL_NO_DATA_NOT_ZERO`, `zero_semantics = REAL_OBSERVED_ZERO` ve kapsam sınırlaması. Eksik gözlem sıfır değildir.
