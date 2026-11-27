// THE TIP'S PLACEMENT RULE (rendered review 2026-11-26): inside its clip
// container, wrapped to its width, shifted or flipped, never past an edge.
import { describe, expect, it } from 'vitest'
import { placeTip, TIP_EDGE_GAP } from '../tipPlacement'

const G = TIP_EDGE_GAP

describe('placeTip', () => {
  it('keeps a tip anchored left when it fits', () => {
    expect(placeTip({ left: 20, right: 48 }, { left: 0, right: 400 }, 200, false)).toEqual({ left: 0, maxWidth: 400 - 2 * G })
  })
  it('shifts a tip back inside when it runs past the right edge of a narrow container', () => {
    const r = placeTip({ left: 200, right: 228 }, { left: 0, right: 300 }, 240, false)
    expect(200 + r.left + 240).toBeLessThanOrEqual(300 - G)
    expect(200 + r.left).toBeGreaterThanOrEqual(G)
  })
  it('wraps a tip wider than the container to the container width, never widening it', () => {
    const r = placeTip({ left: 40, right: 68 }, { left: 0, right: 160 }, 240, false)
    expect(r.maxWidth).toBe(160 - 2 * G)
    expect(40 + r.left).toBe(G)
  })
  it('anchors the right edge to the mark for `end` and still stays inside', () => {
    const r = placeTip({ left: 10, right: 38 }, { left: 0, right: 300 }, 240, true)
    expect(10 + r.left).toBeGreaterThanOrEqual(G)
    expect(r.left + 240).toBeLessThanOrEqual(300 - 10)
  })
})
