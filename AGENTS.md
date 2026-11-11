# THERMALIS — Kodlama ajanı kuralları

Bu dosya Claude Code, Codex ve diğer kodlama ajanları için **tek kural kaynağıdır**.
Ürün vizyonu: *THERMALIS Master Technical Architecture v1.0*.

## Mimari (kilitli)

Beş bileşen, sınırları kesin:

1. **DATA** – tek mantıksal veri kataloğu, değişmez sürümler, veri kartları
2. **ANALYSIS LAB** – HEAT, DROUGHT, FLOOD, COAST, FIRE, EXTREMES
3. **RISK ENGINE** – mahalle/ilçe profilleri, tekil / birlikte görülme / öncelik indeksi
4. **3D CITY ENGINE** – bağımsız kent geometrisi + risk bağlama
5. **INTELLIGENCE** – yalnız okuma yapan, kanıta dayalı doğal dil sorgusu

Ara katmanlar: Metric Catalog (`contracts/`), Spatial Harmonization, Read API.

## Çalışma kuralları

- **Her görevde tek bileşen.** Kapsam dışına çıkma, yeni alt ürün icat etme.
- **Uydurma yok.** Veri, metrik, eşik, katsayı veya sonuç uydurma. Arayüzde sahte sayı
  gösterme; arkasında gerçek iş olmayan ekran dürüst bir boş durum gösterir.
  Belirsizlik `unknown`, `unverified`, `not_applicable`, `missing`, `pending_validation` ile temsil edilir.
  `not_applicable ≠ missing ≠ 0`.
- **Veri repoya girmez.** Veriler makine başına ayarlanan *veri kökü* klasöründe durur.
  Kod veri yolunu asla sabit yazmaz.
- **Kullanıcı dosyaları salt okunur.** Üzerine yazma, silme, taşıma kullanıcı onayı ister.
- **İki dil zorunlu (TR + EN).** Arayüzde sabit metin yok; tüm metinler
  `frontend/src/i18n/locales/{tr,en}.json` içinde. Anahtarlar iki dosyada birebir aynı olmalı (test kontrol eder).
  Yer adları (Kadıköy, Fikirtepe) çevrilmez.
- **Makine çıktıları dilden bağımsız:** CSV/JSON/Parquet her zaman UTF-8 ve nokta ondalık.
  Yerel sayı biçimi yalnız insanın okuduğu çıktılarda.
- **Türkçe karakter:** `"i".upper()` tuzağına dikkat; eşleştirme için `thermalis.text` yardımcılarını kullan.
- **Windows uyumu:** `pathlib` kullan, dosyaları her zaman `encoding="utf-8"` ile aç,
  yol ayırıcı veya sürücü harfi varsayma. CI Windows'ta da koşar.
- **Güvenlik:** Sunucu yalnız `127.0.0.1` dinler. Kullanıcıdan gelen kimlik/yol değerleri doğrulanır.
- **Atomik yazım:** Kayıt dosyaları geçici dosyaya yazılıp `os.replace` ile yerine konur.

## Teslim

- Küçük, tek amaçlı branch + Pull Request. `main`'e doğrudan push yok; birleştirmeyi kullanıcı yapar.
- PR açmadan önce: `pixi run test` ve `pixi run test-ui` geçmeli.
- PR açıklaması şu formatta:

```
COMPONENT: <SHELL | DATA | ANALYSIS LAB | RISK ENGINE | 3D CITY ENGINE | INTELLIGENCE>
SCOPE: <tek iş paketi>
FILES CHANGED: <yollar>
TESTS: <komutlar + sonuç>
OPEN ISSUES: <somut, tahmin yok>
NEXT: <tek somut adım>
```

## Komutlar

```
pixi run start      # programı derle ve tarayıcıda aç
pixi run test       # Python testleri
pixi run test-ui    # arayüz testleri (çeviri anahtarları dahil)
pixi run dev-api    # geliştirme: arka uç (otomatik yeniden yükleme)
pixi run dev-ui     # geliştirme: arayüz (http://localhost:5173)
```
