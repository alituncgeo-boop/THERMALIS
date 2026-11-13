# THERMALIS

**From Climate Data to Climate Intelligence.**

Urban Climate Intelligence Framework — İstanbul pilotu, şehirden bağımsız mimari.

Bileşenler: **DATA · ANALYSIS LAB · RISK ENGINE · 3D CITY ENGINE · INTELLIGENCE**

## Bir bilgisayarda ilk kurulum (Windows)

1. **pixi** kurun (Python, Node ve tüm kütüphaneleri o bilgisayara kilitli sürümlerle kuran araç).
   PowerShell'i açıp şunu çalıştırın, sonra PowerShell'i kapatıp yeniden açın:
   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm -useb https://pixi.sh/install.ps1 | iex"
   ```
2. **GitHub Desktop** ile repoyu bilgisayara indirin: *File → Clone repository → THERMALIS*.
3. Repo klasöründe PowerShell açın (GitHub Desktop: *Repository → Open in Command Prompt / PowerShell*) ve çalıştırın:
   ```powershell
   pixi run start
   ```
   İlk çalıştırma kurulum yaptığı için birkaç dakika sürer. Program tarayıcıda
   `http://127.0.0.1:8765` adresinde açılır. Durdurmak için PowerShell'de `Ctrl+C`.

## Veri nerede durur?

Kod GitHub'da, **veri her bilgisayarın kendi veri kökü klasöründe** durur.
Varsayılan: kullanıcı klasörünüzde `THERMALIS_DATA`. Programda *Ayarlar* ekranından
değiştirilebilir (örn. `D:\THERMALIS_DATA`). Veri dosyaları asla repoya eklenmez.

> Veri kökünü OneDrive / Google Drive gibi eşitlenen klasörlere koymayın.

## Veri paketlerini kataloğa alma

**Olağan yol:** THERMALIS açıkken hazırlanan aktarım paketini (içinde `PACKAGE/MANIFEST.json` olan
klasör) gelen klasöre bırakın (Ayarlar → *Veri aktarım klasörü*). Program klasörü aralıklı tarar,
açılışta da hemen tarar (kapalıyken bırakılan paketler bir sonraki açılışta algılanır). Durum,
DATA ekranındaki *Veri kabul* panelinde görünür.

**Tamamlanmış teslim (önerilen):** kopyalama bitince paket köküne şu dosyayı yazın:

```
THERMALIS_READY.json   {"manifest_sha256": "<PACKAGE/MANIFEST.json dosyasının SHA-256'sı>"}
```

İşaret yoksa paket, bekleme süresi geçtikten ve dosyalar iki taramada değişmedikten sonra işlenir.
Her durumda manifestteki her dosya SHA-256 ile doğrulanır. Klasörü `.` ile başlayan bir adla
kopyalayıp bitince yeniden adlandırmak da güvenli bir yoldur (gizli adlar taranmaz).

Arayüzde *Veri ekle* / *Yeni sürüm yükle* bir paket klasörünü aynı denetimlerden geçirir.
Komut satırından:

```
pixi run data set-incoming "C:\THERMALIS_TRANSFER\INCOMING"   # bir kez
pixi run data ingest --dry-run      # yalnız keşif: hangi paket hazır, hangisi bekliyor
pixi run data ingest                # hazır paketleri doğrula, kaydet, dizinle, denetle
```

- Kopyalanmakta olan, eksik, bağlantı içeren veya arşiv hâlindeki paketler işlenmez.
- Aynı paket ikinci kez kaydedilmez; aynı sürüm farklı içerikle gelirse durur (üzerine yazılmaz).
- Kaynak paket hiçbir koşulda değiştirilmez, taşınmaz, silinmez.
- Kalite durumu paketten alınır, asla yükseltilmez. Erişim sınıfı veya yetki beyanı eksikse durur.
- Sonuçlar: `<veri kökü>/data/_ingest/runs/<kimlik>/INGESTION_REPORT.md` ve `INGESTION_RESULT.json`.

**Desteklenen veri türleri:** şu an yalnız File Geodatabase (vektör). Başka bir tür içeren paket
işlenmez (`BLOCKED`) ve `SUPPORT_GAP.json` yazılır; adaptör `src/thermalis/data/adapters.py` içindeki
adımlarla eklenir ve gerçek veriyle doğrulandıktan sonra kalıcı olur.

Diğer komutlar: `data formats`, `data layers`, `data findings <id> <sürüm>`, `data verify <id> <sürüm>`,
`data reindex`.

## Güncelleme

GitHub Desktop'ta *Fetch origin → Pull*, ardından tekrar `pixi run start`.

## Geliştirme

Kurallar: [AGENTS.md](AGENTS.md) (Claude Code, Codex ve insan geliştiriciler için tek kaynak).

```
pixi run test       # Python testleri
pixi run test-ui    # arayüz testleri
pixi run dev-api    # arka uç, otomatik yeniden yükleme
pixi run dev-ui     # arayüz, http://localhost:5173
```
