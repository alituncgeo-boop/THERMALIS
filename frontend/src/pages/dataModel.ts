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

export function intakeBuckets(counts: Record<string, number>) {
  const sum = (states: string[]) => states.reduce((n, s) => n + (counts[s] ?? 0), 0);
  return {
    inProgress: sum(IN_PROGRESS),
    waiting: sum(WAITING),
    adapterRequired: counts.ADAPTER_REQUIRED ?? 0,
    failed: sum(["FAILED", "BLOCKED"]),
  };
}

export function latestRegistered(items: CatalogItem[]): CatalogItem | null {
  return items.reduce<CatalogItem | null>((best, i) => (i.registered_at && (!best || (best.registered_at ?? "") < i.registered_at) ? i : best), null);
}

/** Dışa aktarılan değerler ve görünen değerler: yok = null; sıfır ve boş metin farklıdır. */
export function isMissing(value: unknown): boolean {
  return value === null || value === undefined || value === "";
}
