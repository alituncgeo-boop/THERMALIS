import { describe, expect, it } from "vitest";
import type { CatalogItem } from "../api";
import { categories, filterCatalog, intakeBuckets, isMissing, latestRegistered, matchKey } from "./dataModel";

function item(overrides: Partial<CatalogItem>): CatalogItem {
  return {
    id: "THERMALIS_DATA/TT01/ISTANBUL/DISTRICTS", dataset_id: "THERMALIS_DATA/TT01/ISTANBUL", version: "1.0.0",
    package_id: "TT01_v1.0.0", package_version: "1.0.0", package_fingerprint: null, layer_name: "Districts", place: "Istanbul",
    source_title: null, category: "administrative_boundaries", source_institution: "İstanbul Büyükşehir Belediyesi",
    format: null, format_key: "filegdb_vector", asset_id: null, asset_path: null, geometry_type: "Multi Polygon",
    feature_count: 39, role: "core", crs_name: null, spatial_extent: null,
    temporal: { state: "unknown", currency_year: null, valid_from: null, valid_to: null },
    quality_status: "QA_CONDITIONAL", access_class: "RESTRICTED",
    integrity: { status: null, checked_at: null, last_successful_at: null },
    metadata_completeness: { total: 0, present: 0, partial: 0, missing: 0, unknown: 0, not_applicable: 0 },
    registered_at: "2026-11-12T12:32:51Z", summary: null, description: "ilçe sınırları", record_issue_count: 0, version_count: 1,
    ...overrides,
  };
}

const districts = item({});
const hoods = item({ id: "THERMALIS_DATA/TT01/ISTANBUL/NEIGHBORHOODS", layer_name: "Neighborhoods", description: "mahalle verileri", registered_at: "2026-11-13T00:00:00Z" });
const lst = item({ id: "THERMALIS_DATA/LS01/ISTANBUL/LST", layer_name: "LST", category: null, description: null });

describe("katalog arama ve filtre / catalog search and filter", () => {
  it("Türkçe harfler katlanır", () => {
    expect(matchKey("  İSTANBUL  Büyükşehir ")).toBe("istanbul buyuksehir");
    expect(matchKey("ılıca")).toBe(matchKey("ILICA"));
  });

  it("arama tüm terimleri gerçek alanlarda arar", () => {
    expect(filterCatalog([districts, hoods], "mahalle", "")).toEqual([hoods]);
    expect(filterCatalog([districts, hoods], "istanbul belediyesi", "")).toHaveLength(2);
    expect(filterCatalog([districts, hoods], "ilce sinir", "")).toEqual([districts]);
    expect(filterCatalog([districts, hoods], "yok-boyle-bir-sey", "")).toEqual([]);
  });

  it("kategori filtresi; kategorisi olmayan kayıt '' altında", () => {
    expect(categories([districts, hoods, lst])).toEqual(["", "administrative_boundaries"]);
    expect(filterCatalog([districts, hoods, lst], "", "administrative_boundaries")).toEqual([districts, hoods]);
    expect(filterCatalog([districts, hoods, lst], "", "")).toHaveLength(3);
  });
});

describe("işlem durumu özetleri / intake summaries", () => {
  it("durumlar doğru kümelere düşer", () => {
    expect(intakeBuckets({ VALIDATING: 1, INGESTING: 1, WAITING_FOR_COMPLETION: 2, DETECTED: 1, ADAPTER_REQUIRED: 1, FAILED: 1, BLOCKED: 2, REGISTERED: 5 }))
      .toEqual({ inProgress: 2, waiting: 3, adapterRequired: 1, failed: 3 });
    expect(intakeBuckets({})).toEqual({ inProgress: 0, waiting: 0, adapterRequired: 0, failed: 0 });
  });

  it("son kayıt gerçek tarihten seçilir; 0 eksik sayılmaz", () => {
    expect(latestRegistered([districts, hoods])).toBe(hoods);
    expect(latestRegistered([])).toBeNull();
    expect(isMissing(0)).toBe(false);
    expect(isMissing(null)).toBe(true);
    expect(isMissing("")).toBe(true);
  });
});
