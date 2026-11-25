import { describe, expect, it } from "vitest";
import { connectionTone, formatBytes, percent } from "./dataModel";

describe("formatBytes", () => {
  it("uses binary units and never invents unknown values", () => {
    expect(formatBytes(0, "en-GB")).toBe("0 B");
    expect(formatBytes(1536, "en-GB")).toBe("1.5 KB");
    expect(formatBytes(20 * 1024 ** 3, "en-GB")).toBe("20 GB");
    expect(formatBytes(1024 ** 4 * 1.25, "tr-TR")).toBe("1,3 TB");
    expect(formatBytes(null)).toBeNull();
    expect(formatBytes(undefined)).toBeNull();
    expect(formatBytes(-1)).toBeNull();
  });
});

describe("percent", () => {
  it("clamps and refuses to guess without a known total", () => {
    expect(percent(50, 200)).toBe(25);
    expect(percent(300, 200)).toBe(100);
    expect(percent(5, 0)).toBeNull();
    expect(percent(null, 100)).toBeNull();
    expect(percent(10, undefined)).toBeNull();
  });
});

describe("connectionTone", () => {
  it("maps watcher results to tones", () => {
    expect(connectionTone("ok")).toBe("ok");
    expect(connectionTone("offline")).toBe("warn");
    expect(connectionTone("credentials_missing")).toBe("bad");
    expect(connectionTone("disabled")).toBe("muted");
    expect(connectionTone(null)).toBe("muted");
  });
});
