import { describe, expect, it } from "vitest";
import { download } from "@/test/fixtures";
import { applyEvent, backoff, parseEvent } from "./events";

describe("applyEvent", () => {
  it("patches progress in place without a refetch", () => {
    const list = [download({ status: "queued", done_bytes: 0 }), download({ id: "d2" })];
    const result = applyEvent(list, { event: "download.progress", id: "d1", done: 10, total: 20, speed: 5, connections: 4 });
    expect(result.refetch).toBe(false);
    expect(result.list?.[0]).toMatchObject({ status: "active", done_bytes: 10, total_bytes: 20, speed: 5, connections: 4 });
    expect(result.list?.[1]).toBe(list[1]);
  });

  it("does not resurrect a paused download from a late progress event", () => {
    const result = applyEvent([download({ status: "paused" })], { event: "download.progress", id: "d1", done: 1 });
    expect(result.list?.[0]?.status).toBe("paused");
  });

  it("drops removed downloads and refetches for anything else", () => {
    expect(applyEvent([download()], { event: "download.removed", id: "d1" }).list).toEqual([]);
    expect(applyEvent([download()], { event: "download.completed", id: "d1" }).refetch).toBe(true);
    expect(applyEvent([download()], { event: "download.progress", id: "new" }).refetch).toBe(true);
    expect(applyEvent(undefined, { event: "download.added", id: "x" }).refetch).toBe(true);
  });
});

describe("parseEvent and backoff", () => {
  it("accepts only event objects", () => {
    expect(parseEvent('{"event":"download.added","id":"x"}')).toEqual({ event: "download.added", id: "x" });
    expect(parseEvent("not json")).toBeNull();
    expect(parseEvent('{"id":"x"}')).toBeNull();
    expect(parseEvent(new ArrayBuffer(1))).toBeNull();
  });

  it("backs off up to ten seconds", () => {
    expect([0, 1, 2, 10].map(backoff)).toEqual([500, 1000, 2000, 10_000]);
  });
});
