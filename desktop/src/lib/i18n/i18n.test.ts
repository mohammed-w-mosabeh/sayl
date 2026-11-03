import { describe, expect, it } from "vitest";
import ar from "./ar.json";
import en from "./en.json";
import { direction, makeI18n, translate, type Key } from ".";

const placeholders = (text: string) => [...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();

describe("catalogs", () => {
  it("Arabic has every English key and nothing else", () => {
    expect(Object.keys(ar).sort()).toEqual(Object.keys(en).sort());
  });

  it("every string keeps the same placeholders", () => {
    for (const key of Object.keys(en) as Key[]) {
      expect(placeholders(ar[key]), key).toEqual(placeholders(en[key]));
    }
  });

  it("no string is empty", () => {
    for (const [key, value] of [...Object.entries(en), ...Object.entries(ar)]) {
      expect(value.trim(), key).not.toBe("");
    }
  });
});

describe("translate", () => {
  it("fills variables and leaves unknown ones visible", () => {
    expect(translate("en", "add.quality.height", { height: 1080 })).toBe("Up to 1080p");
    expect(translate("en", "add.quality.height")).toBe("Up to {height}p");
  });

  it("Arabic is right to left, English left to right", () => {
    expect(direction("ar")).toBe("rtl");
    expect(direction("en")).toBe("ltr");
    expect(makeI18n("ar").t("action.add")).toBe("إضافة");
  });
});
