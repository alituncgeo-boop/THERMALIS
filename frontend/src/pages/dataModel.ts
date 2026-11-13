import type { CatalogItem } from "../api";

/** Arama anahtarı: Türkçe harfler ASCII'ye katlanır (sunucudaki thermalis.text.match_key ile aynı kural). */
const FOLD: Record<string, string> = {
  ç: "c", Ç: "c", ğ: "g", Ğ: "g", ı: "i", I: "i", İ: "i", ö: "o", Ö: "o", ş: "s", Ş: "s", ü: "u", Ü: "u",
  â: "a", Â: "a", î: "i", Î: "i", û: "u", Û: "u",
};

export function matchKey(value: string): string {
  return value
    .normalize("NFC")
    .replace(/[çÇğĞıIİöÖşŞüÜâÂîÎûÛ]/g, (ch) => FOLD[ch] ?? ch)
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean)
    .join(" ");
}

/** Arama yalnız gerçek katalog alanlarında yapılır. */
export function searchText(item: CatalogItem, extra: string[] = []): string {
  return matchKey(
    [item.id, item.dataset_id, item.layer_name, item.place, item.category, item.source_institution, item.source_title,
      item.summary, item.description, item.version, item.package_id, ...extra]
      .filter((v): v is string => typeof v === "string")
      .join(" "),
  );
}

export function filterCatalog(items: CatalogItem[], query: string, category: string, extra?: (i: CatalogItem) => string[]): CatalogItem[] {
  const terms = matchKey(query).split(" ").filter(Boolean);
  return items.filter((item) => {
    if (category && (item.category ?? "") !== category) return false;
    if (!terms.length) return true;
    const text = searchText(item, extra ? extra(item) : []);
    return terms.every((term) => text.includes(term));
  });
}

/** Kategori listesi kayıtlardan çıkar; kategorisi olmayan kayıtlar "" (bilinmiyor) altında toplanır. */
export function categories(items: CatalogItem[]): string[] {
  return [...new Set(items.map((i) => i.category ?? ""))].sort();
}

export const IN_PROGRESS = ["VALIDATING", "INGESTING"];
export const WAITING = ["DETECTED", "WAITING_FOR_COMPLETION", "READY"];

/** Durum özetleri. UNSUPPORTED_INPUT (ör. bağımsız .zip) başarısızlık sayılmaz; ayrı "işlem dışı" sayılır. */
export function intakeBuckets(counts: Record<string, number>) {
  const sum = (states: string[]) => states.reduce((n, s) => n + (counts[s] ?? 0), 0);
  return {
    inProgress: sum(IN_PROGRESS),
    waiting: sum(WAITING),
    adapterRequired: counts.ADAPTER_REQUIRED ?? 0,
    failed: sum(["FAILED", "BLOCKED"]),
    ignored: counts.UNSUPPORTED_INPUT ?? 0,
  };
}

export type SortKey = "title" | "version" | "quality" | "features" | "registered";
const QUALITY_RANK: Record<string, number> = { QA_PASS: 0, QA_CONDITIONAL: 1, QA_PENDING: 2, QA_FAIL: 3 };

/** Sıralama kararlıdır; eşitlikte kimliğe göre. Eksik değerler her yönde sona gider. */
export function sortCatalog(items: CatalogItem[], key: SortKey, dir: "asc" | "desc", title: (i: CatalogItem) => string, locale = "tr-TR"): CatalogItem[] {
  const value = (i: CatalogItem): string | number | null => {
    switch (key) {
      case "title": return title(i);
      case "version": return i.version;
      case "quality": return QUALITY_RANK[i.quality_status] ?? 9;
      case "features": return i.feature_count;
      case "registered": return i.registered_at;
    }
  };
  const sign = dir === "asc" ? 1 : -1;
  return [...items].sort((a, b) => {
    const va = value(a);
    const vb = value(b);
    if (isMissing(va) !== isMissing(vb)) return isMissing(va) ? 1 : -1;
    let c = 0;
    if (typeof va === "number" && typeof vb === "number") c = va - vb;
    else if (!isMissing(va)) c = String(va).localeCompare(String(vb), locale, { numeric: true, sensitivity: "base" });
    return c !== 0 ? c * sign : a.id.localeCompare(b.id);
  });
}

/** Sayfalama: büyük kataloglarda tablo yalnız bir sayfayı çizer. */
export function pageOf<T>(items: T[], page: number, size: number) {
  const pages = Math.max(1, Math.ceil(items.length / size));
  const current = Math.min(Math.max(page, 0), pages - 1);
  const start = current * size;
  const rows = items.slice(start, start + size);
  return { rows, pages, page: current, from: items.length ? start + 1 : 0, to: start + rows.length };
}

export function latestRegistered(items: CatalogItem[]): CatalogItem | null {
  return items.reduce<CatalogItem | null>((best, i) => (i.registered_at && (!best || (best.registered_at ?? "") < i.registered_at) ? i : best), null);
}

/** Dışa aktarılan değerler ve görünen değerler: yok = null; sıfır ve boş metin farklıdır. */
export function isMissing(value: unknown): boolean {
  return value === null || value === undefined || value === "";
}
