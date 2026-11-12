"""DATA kayıt teslim raporu (Markdown, Türkçe)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from .register import _Run


def _cell(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value).replace("|", "\\|").replace("\n", " ")


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    out += ["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows]
    return "\n".join(out)


def render_report(run: "_Run", status: str, error: str | None, location: Path | None) -> str:
    pkg = run.pkg
    lines: list[str] = []
    family = pkg.family_prefix if pkg else "DATA"
    lines.append(f"# {family} — THERMALIS DATA kayıt raporu")
    lines.append("")
    verdict = "**PASS** — kayıt tamamlandı ve doğrulandı" if status == "PASS" else "**FAIL** — kayıt tamamlanmadı; katalogda görünmez"
    lines.append(f"- **Sonuç:** {verdict}")
    if pkg:
        lines.append(f"- **Kalite durumu:** `{pkg.quality_status}` (paketten alındı; yükseltilmedi)")
    lines.append(f"- **Kayıt kimliği:** `{run.ingest_id}` · başlangıç {run.started_at}")
    if error:
        lines.append(f"- **Durma nedeni:** `{error}`")
    lines.append("")

    if pkg:
        rec = getattr(run, "version_record", {})
        lines += ["## 1. Kimlik", ""]
        lines.append(_table(["Alan", "Değer"], [
            ["Veri kimliği", f"`{pkg.dataset_id}`"],
            ["Sürüm", f"`{pkg.version}`"],
            ["Paket", f"`{pkg.package_id}` ({pkg.manifest_meta.get('package_type')})"],
            ["Paket oluşturma (UTC)", pkg.manifest_meta.get("built_at_utc")],
            ["Paket durumu", pkg.release_status.get("status")],
            ["Veri sahibi", pkg.release_status.get("data_owner")],
            ["Kaynak güncelleme yılı", pkg.release_status.get("source_update_year")],
            ["Araç", f"thermalis · GDAL {run.gdal_version or 'yok'}"],
            ["Kayıt zamanı (UTC)", rec.get("registration", {}).get("registered_at")],
        ]))
        lines += ["", "## 2. Kaynak ve hedef konumlar", ""]
        lines.append(_table(["", "Konum"], [
            ["Kaynak paket (bu bilgisayar)", f"`{pkg.root}`"],
            ["Köken (paketleme bilgisayarı)", f"`{pkg.manifest_meta.get('source_path_on_origin_machine')}`"],
            ["Hedef (katalog sürüm klasörü)", f"`{location}`" if location else "—"],
            ["Veri kopyası", "`payload/` — paketin bayt-bayt kopyası, dosyalar salt okunur"],
        ]))
        lines += ["", "Kaynak paket yalnız okundu; değiştirilmedi, taşınmadı, silinmedi.", ""]

        lines += ["## 3. Erişim politikası", ""]
        access = rec.get("access", {})
        lines.append(f"- Sınıf: **{access.get('classification', '—')}** · açık veri olarak yayımlanabilir: **hayır**")
        lines.append(f"- Yetki: {access.get('authorization', '—')}")
        lines.append(f"- {access.get('policy', '')}")
        lines.append("")

    if run.layer_records:
        lines += ["## 4. Aktarılan katmanlar", ""]
        rows = []
        for name, layer in run.layer_records.items():
            nulls = {f["name"]: f["null_count"] for f in layer["fields"] if f["name"] == "KIMLIKNO"}
            rows.append([
                f"`{name}`", layer["role"], "evet" if layer["published"] else "hayır", layer["geometry_type"],
                layer["feature_count"], layer["expected_feature_count"],
                "evet" if layer["crs_matches_package_wkt"] else ("—" if layer["crs_matches_package_wkt"] is None else "HAYIR"),
                nulls.get("KIMLIKNO"),
            ])
        lines.append(_table(["Katman", "Rol", "Yayımlanır", "Geometri", "Sayılan", "Beklenen", "CRS aynı", "Boş KIMLIKNO"], rows))
        lines += ["", "`core` katmanlar üretim idari sınırlarıdır. `auxiliary` katmanlar köken için saklanır, yayımlanmaz.", ""]

    if pkg:
        lines += ["## 5. CRS", ""]
        lines.append(f"- Ad: `{pkg.crs.get('native_crs_name')}` · EPSG eşdeğeri varsayılmadı (factory_code={pkg.crs.get('factory_code')}) · birim: {pkg.crs.get('unit')}")
        lines.append("- Koordinat dönüşümü uygulanmadı. Paket WKT'si aynen saklandı:")
        lines += ["", "```", str(pkg.crs.get("native_wkt")), "```", ""]

        lines += ["## 6. Şema, veri sözlüğü ve domainler", ""]
        for name in pkg.core_layers:
            layer = run.layer_records.get(name)
            if not layer:
                continue
            lines.append(f"**{name}** — {len(layer['fields'])} öznitelik alanı (+ OBJECTID, Shape)")
            lines.append("")
            lines.append(_table(["Alan", "Takma ad", "Tür", "Domain", "Boş"], [
                [f"`{f['name']}`", f["dictionary_alias"] or f["alias"], f["dictionary_type"] or f["type"], f["coded_domain"], f["null_count"]]
                for f in layer["fields"]
            ]))
            lines.append("")
        lines.append(_table(["Domain", "Açıklama", "Kod sayısı"], [
            [f"`{d['name']}`", d.get("description"), len(d.get("coded_values", {}))] for d in pkg.domains
        ]))
        lines += ["", "Kodlu tamsayılar adlara dönüştürülmedi; domain tanımları kaynakla birebir.", ""]

        integrity = getattr(run, "integrity_record", None)
        lines += ["## 7. Bütünlük", ""]
        if integrity:
            v = integrity["verifications"]
            lines.append(_table(["Doğrulama", "Sonuç", "Dosya"], [
                [label, v[key]["status"], v[key]["checked_files"]]
                for key, label in [("source_package", "Kaynak paket"), ("staging_copy", "Kopya (staging)"), ("final_location", "Nihai konum (kayıt sonrası)")]
                if key in v
            ]))
            lines.append("")
            lines.append(f"- Manifest kapsamı: {integrity['file_count']} dosya · {integrity['total_bytes']:,} bayt · SHA-256".replace(",", "."))
            lines.append("- Dosya bazlı özetler: `integrity.json` (sürüm klasöründe)")
        else:
            lines.append("- Bütünlük doğrulaması tamamlanmadı.")
        lines.append("")

        lines += ["## 8. Metadata durumu", ""]
        lines.append("Aşağıdaki belgeler değiştirilmeden `payload/` içinde saklandı ve `version.json` içinde listelendi:")
        lines.append("")
        lines += [f"- `{doc}`" for doc in pkg.documents()]
        lines.append("")

    quality = getattr(run, "quality_record", None)
    if quality:
        lines += ["## 9. Kalite kataloğu ve QA istisnaları", ""]
        lines.append(f"- Durum: **`{quality['status']}`** — {quality['upgrade_policy']}")
        lines.append(f"- Veri sorumlusu durumu: {quality.get('steward_disposition')}")
        lines.append(f"- Kullanım uygunluğu: {quality['fitness_for_use']}")
        lines.append("")
        lines.append(_table(["Kategori", "Kayıt"], sorted(quality["summary"].items())))
        lines.append("")
        exc = quality["exceptions"]

        def section(title: str, categories: tuple[str, ...], cols: list[tuple[str, str]]) -> None:
            rows = [e for e in exc if e["category"] in categories]
            if not rows:
                return
            lines.append(f"### {title} ({len(rows)})")
            lines.append("")
            lines.append(_table([c[1] for c in cols], [[e.get(c[0]) for c in cols] for e in rows]))
            lines.append("")

        pair = [("exception_id", "Kimlik"), ("feature_a", "A"), ("oid_a", "OID A"), ("feature_b", "B"), ("oid_b", "OID B"), ("area_m2", "Alan m²"), ("disposition", "Durum")]
        section("İlçe–ilçe çakışmaları", ("DISTRICT_OVERLAP",), pair)
        section("Mahalle–mahalle çakışmaları", ("NEIGHBORHOOD_OVERLAP_SAME_DISTRICT", "NEIGHBORHOOD_OVERLAP_CROSS_DISTRICT"),
                [("exception_id", "Kimlik"), ("category", "Tür")] + pair[1:])
        section("Üst ilçe dışına taşma", ("NEIGHBORHOOD_OUTSIDE_PARENT",),
                [("exception_id", "Kimlik"), ("feature_a", "Mahalle"), ("oid_a", "OID"), ("feature_b", "Üst ilçe"), ("area_m2", "Alan m²"), ("percent_of_feature", "%"), ("disposition", "Durum")])
        section("Kasıtlı boş kimlik numaraları", ("INTENTIONAL_NULL_IDENTIFIER",),
                [("exception_id", "Kimlik"), ("dataset", "Katman"), ("feature_a", "Ad"), ("oid_a", "OBJECTID"), ("evidence", "Kanıt"), ("disposition", "Durum")])
        section("Yardımcı katman bulguları", ("AUXILIARY_SELF_INTERSECTION",),
                [("exception_id", "Kimlik"), ("dataset", "Katman"), ("category", "Tür"), ("evidence", "Kanıt"), ("disposition", "Durum")])
        if quality.get("summary_by_origin"):
            lines.append("Kayıt kökeni: " + ", ".join(f"`{k}` {v}" for k, v in sorted(quality["summary_by_origin"].items()))
                         + f" · toplam {len(exc)} kayıt, {len({e['exception_id'] for e in exc})} tekil kimlik.")
            lines.append("")
        if quality.get("layer_status"):
            lines.append(f"### Yardımcı katman durumu ({len(quality['layer_status'])}) — QA bulgusu değildir, sayıma girmez")
            lines.append("")
            lines.append(_table(["Katman", "Geometri durumu", "Tür", "Kayıt", "Yayımlanır"], [
                [f"`{s['layer']}`", s["geometry_state"], s["geometry_type"], s["feature_count"], "hayır"] for s in quality["layer_status"]
            ]))
            lines.append("")
        gaps = [e for e in exc if e["category"] == "DISTRICT_COVERAGE_GAP_DIAGNOSTIC"]
        if gaps:
            top = sorted(gaps, key=lambda e: e["area_m2"] or 0, reverse=True)[:3]
            total = sum(e["area_m2"] or 0 for e in gaps)
            lines.append(f"### İlçe kapsama farkı tanılamaları ({len(gaps)})")
            lines.append("")
            lines.append(f"Toplam {total:.3f} m²; en büyükler: " + ", ".join(f"{e['feature_a']} {e['area_m2']:.3f} m²" for e in top)
                         + ". Tanılayıcıdır; bağımsız topoloji hatası kanıtı değildir. Tam liste `quality.json` içinde.")
            lines.append("")
        if quality.get("gates"):
            lines.append("### Paket kapıları (değiştirilmeden taşındı)")
            lines.append("")
            lines.append(_table(["Kapı", "Durum", "Açıklama"], [[g.get("id"), g.get("status"), g.get("detail")] for g in quality["gates"]]))
            lines.append("")

    lines += ["## 10. Denetimler", ""]
    lines.append(_table(["Kod", "Denetim", "Sonuç", "Ayrıntı"], [[c.id, c.title, c.status, c.detail] for c in run.checks]))
    lines.append("")
    lines += ["## 11. Yapılmayanlar (bilinçli)", ""]
    lines += [
        "- Kaynak veri değiştirilmedi; geometri onarımı, snap, kırpma, birleştirme, sadeleştirme uygulanmadı.",
        "- Koordinat dönüşümü yapılmadı; EPSG kodu varsayılmadı.",
        "- Kayıt silinmedi; boş kimlik numaraları doldurulmadı.",
        "- QA bulguları kaynağı reddetmek veya düzeltmek için kullanılmadı; kalite durumu yükseltilmedi.",
        "- Türetilmiş geometri veya indeks üretilmedi; analiz motoru çalıştırılmadı.",
        "",
    ]
    return "\n".join(lines)
