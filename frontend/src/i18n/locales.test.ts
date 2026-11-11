import { describe, expect, it } from "vitest";
import en from "./locales/en.json";
import tr from "./locales/tr.json";

type Tree = { [key: string]: string | Tree };

function flatten(tree: Tree, prefix = ""): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(tree)) {
    const path = prefix ? `${prefix}.${key}` : key;
    if (typeof value === "string") out[path] = value;
    else Object.assign(out, flatten(value, path));
  }
  return out;
}

const trFlat = flatten(tr as Tree);
const enFlat = flatten(en as Tree);

describe("çeviri dosyaları / locale files", () => {
  it("TR ve EN aynı anahtarlara sahip", () => {
    expect(Object.keys(trFlat).sort()).toEqual(Object.keys(enFlat).sort());
  });

  it("boş çeviri yok", () => {
    for (const [key, value] of [...Object.entries(trFlat), ...Object.entries(enFlat)]) {
      expect(value.trim(), key).not.toBe("");
    }
  });

  it("yer tutucular iki dilde aynı", () => {
    const placeholders = (s: string) => (s.match(/{{\s*\w+\s*}}/g) ?? []).sort();
    for (const key of Object.keys(trFlat)) {
      expect(placeholders(trFlat[key]), key).toEqual(placeholders(enFlat[key]));
    }
  });
});
