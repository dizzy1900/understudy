import type { NodeInfo } from "./types";

export type Vec3 = [number, number, number];

export const RACK_SPACING_X = 3.1;
export const RACK_SPACING_Z = 3.5;
export const RACK_SIZE: Vec3 = [1.3, 2.5, 1.1];

/** Floor level is y = 0. Racks face +z (toward the camera and the command desk). */
export const DESK_POSITION: Vec3 = [0, 0, 7.4];
export const COMMANDER_ORB_POSITION: Vec3 = [0, 1.9, 7.4];

/** The glowing counter that the ceiling stream flows into, on the far wall. */
export const COUNTER_POSITION: Vec3 = [0, 4.6, -10.2];
export const COUNTER_FRAME = { w: 11, h: 3 };
export const CEILING_Y = 4.4;

/** Proving ground bay: 2 x 2 smaller racks behind glass, left of the hall. */
export const PROVING_CENTER: Vec3 = [-12.6, 0, -1.4];
export const PROVING_RACK_SCALE = 0.82;
export const LIBRARY_POSITION: Vec3 = [-4.6, 1.5, 6.6];

export function isProving(id: string): boolean {
  return id.startsWith("pg-");
}

/** Row and column indexes (zero based) of a node inside its own zone. */
export function gridIndex(node: Pick<NodeInfo, "id" | "row" | "col">): { row: number; col: number } {
  if (isProving(node.id)) {
    const i = Math.max(0, parseInt(node.id.slice(3), 10) - 1);
    return { row: Math.floor(i / 2), col: i % 2 };
  }
  const match = /^r(\d)-(\d+)$/.exec(node.id);
  if (match) return { row: parseInt(match[1], 10) - 1, col: parseInt(match[2], 10) - 1 };
  return { row: Math.max(0, node.row - 1), col: Math.max(0, node.col - 1) };
}

export function nodePosition(node: Pick<NodeInfo, "id" | "row" | "col">): Vec3 {
  const { row, col } = gridIndex(node);
  if (isProving(node.id)) {
    return [PROVING_CENTER[0] + (col - 0.5) * 2.0, 0, PROVING_CENTER[2] + (row - 0.5) * 2.2];
  }
  return [(col - 1.5) * RACK_SPACING_X, 0, (row - 1) * RACK_SPACING_Z - 2.2];
}

export function nodeIdPosition(id: string): Vec3 {
  return nodePosition({ id, row: 1, col: 1 });
}

/** Where an agent hovers above its rack. */
export function hoverPosition(id: string, lift = 3.5): Vec3 {
  const [x, , z] = nodeIdPosition(id);
  return [x, isProving(id) ? lift - 0.3 : lift, z + 0.2];
}
