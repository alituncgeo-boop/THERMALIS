import { describe, expect, it } from "vitest";
import { takeAuthCode } from "./session";

const at = (hash: string) => ({ hash, pathname: "/", search: "" });

describe("takeAuthCode", () => {
  it("reads only a well-formed one-time code from the hash", () => {
    expect(takeAuthCode(at("#auth=AbCdEfGhIjKlMnOpQrStUvWxYz012345"))).toBe("AbCdEfGhIjKlMnOpQrStUvWxYz012345");
  });

  it("ignores anything else", () => {
    expect(takeAuthCode(at(""))).toBeNull();
    expect(takeAuthCode(at("#auth=short"))).toBeNull();
    expect(takeAuthCode(at("#auth=AbCdEfGhIjKlMnOp<script>"))).toBeNull();
    expect(takeAuthCode(at("#section"))).toBeNull();
  });
});
