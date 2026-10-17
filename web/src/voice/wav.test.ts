import { describe, expect, it } from "vitest";

import { encodeWav } from "./wav";

describe("wav encoding", () => {
  it("writes a 16-bit mono header that matches the data", () => {
    const bytes = encodeWav(new Float32Array([0, 0.5, -0.5, 1, -1]), 16_000);
    const view = new DataView(bytes.buffer);
    const tag = (at: number) => String.fromCharCode(...bytes.slice(at, at + 4));
    expect([tag(0), tag(8), tag(12), tag(36)]).toEqual(["RIFF", "WAVE", "fmt ", "data"]);
    expect(view.getUint16(20, true)).toBe(1);
    expect(view.getUint16(22, true)).toBe(1);
    expect(view.getUint32(24, true)).toBe(16_000);
    expect(view.getUint32(28, true)).toBe(32_000);
    expect(view.getUint16(34, true)).toBe(16);
    expect(view.getUint32(40, true)).toBe(10);
    expect(view.getUint32(4, true)).toBe(bytes.length - 8);
    expect(bytes.length).toBe(44 + 10);
  });

  it("scales samples and clips anything past full scale", () => {
    const bytes = encodeWav(new Float32Array([0.5, -0.5, 3, -3]), 16_000);
    const view = new DataView(bytes.buffer);
    expect(view.getInt16(44, true)).toBe(Math.round(0.5 * 0x7fff - 0.5));
    expect(view.getInt16(46, true)).toBe(-0x4000);
    expect(view.getInt16(48, true)).toBe(0x7fff);
    expect(view.getInt16(50, true)).toBe(-0x8000);
  });
});
