// THE LOG DOCK'S ARITHMETIC (viewers.html A, picked 2026-10-02): where a
// window sits in its stream, whether output fell out of the tail between two
// reads, how many lines arrived while paused, the console-side error pattern,
// search, and the server's mask drawn as its own run.
//
// MUTATIONS, one per block: drop the header's length from `windowAt`; answer
// null instead of `unknown` for a live tail with no header; report a gap when
// the windows touch; count lines arrived across a slid tail as exact; let a
// search hit split the mask.

import { describe, expect, it } from 'vitest'

import {
  MASK,
  arrivedLines,
  isErrorLine,
  linesOf,
  searchLines,
  segments,
  tailGap,
  windowAt,
  type WindowFacts,
} from '../logLines'

function live(objectOffset: number, returned: number, over: Partial<WindowFacts> = {}): WindowFacts {
  // A tail object of `returned` stream bytes behind a 40-byte header.
  return {
    source: 'live',
    status: 'ok',
    offset: 40,
    returned_bytes: returned,
    total_bytes: 40 + returned,
    tail_window: { object_offset: objectOffset, stream_size: objectOffset + returned, published_at: '2026-10-02T14:02:11Z' },
    ...over,
  }
}

describe('where a window sits in its stream', () => {
  it('places a live tail by its header, with the header’s own bytes taken off', () => {
    const at = windowAt(live(1_000_000, 262_144))
    expect(at).toEqual({ start: 1_000_000, end: 1_262_144, publishedAt: '2026-10-02T14:02:11Z', live: true })
  })

  it('places a final record by its own offset, which is the stream’s', () => {
    const at = windowAt({ source: 'final', status: 'ok', offset: 524_288, returned_bytes: 1000, total_bytes: 3_000_000, tail_window: null })
    expect(at).toEqual({ start: 524_288, end: 525_288, publishedAt: null, live: false })
  })

  it('gives a live tail with no header no position, rather than a guessed one', () => {
    const at = windowAt(live(0, 10, { tail_window: null }))
    expect(at.start).toBeNull()
    expect(at.end).toBeNull()
  })
})

describe('output missing between two reads', () => {
  it('names the bytes nobody read when the tail moved past them', () => {
    const before = windowAt(live(948_736, 256_144))
    const after = windowAt(live(1_318_400, 256_512, { tail_window: { object_offset: 1_318_400, stream_size: 1_574_912, published_at: '2026-10-02T14:02:16Z' } }))
    expect(tailGap(before, after)).toEqual({
      kind: 'gap',
      from: 1_204_880,
      to: 1_318_400,
      before: '2026-10-02T14:02:11Z',
      after: '2026-10-02T14:02:16Z',
    })
  })

  it('says nothing when the windows touch or overlap', () => {
    const a = windowAt(live(0, 1000))
    expect(tailGap(a, windowAt(live(1000, 500)))).toBeNull()
    expect(tailGap(a, windowAt(live(400, 900)))).toBeNull()
  })

  it('says it cannot tell when a tail carried no header -- which is not "nothing is missing"', () => {
    const a = windowAt(live(0, 1000))
    expect(tailGap(a, windowAt(live(0, 1000, { tail_window: null })))).toEqual({ kind: 'unknown' })
  })

  it('compares nothing across a final record, which cannot skip', () => {
    const a = windowAt(live(0, 1000))
    const f = windowAt({ source: 'final', status: 'ok', offset: 0, returned_bytes: 9000, total_bytes: 9000, tail_window: null })
    expect(tailGap(a, f)).toBeNull()
    expect(tailGap(null, a)).toBeNull()
  })
})

describe('lines that arrived while paused', () => {
  it('counts exactly while the window has not slid', () => {
    const paused = { at: windowAt(live(0, 6)), content: 'a\nb\nc\n' }
    const next = { at: windowAt(live(0, 10)), content: 'a\nb\nc\nd\ne\n' }
    expect(arrivedLines(paused, next)).toEqual({ lines: 2, exact: true })
  })

  it('counts from the new bytes once the tail slid, and says the count is approximate', () => {
    const paused = { at: windowAt(live(0, 6)), content: 'a\nb\nc\n' }
    const next = { at: windowAt(live(4, 8)), content: 'c\nd\ne\nf\n' }
    expect(arrivedLines(paused, next)).toEqual({ lines: 3, exact: false })
  })

  it('counts nothing when nothing was appended', () => {
    const paused = { at: windowAt(live(0, 6)), content: 'a\nb\nc\n' }
    expect(arrivedLines(paused, paused)).toEqual({ lines: 0, exact: true })
  })
})

describe('jump to error is a console-side pattern', () => {
  it('matches a traceback, an ERROR level, a failed test and a non-zero exit', () => {
    expect(isErrorLine('Traceback (most recent call last):')).toBe(true)
    expect(isErrorLine('14:02:09 ERROR lease renew failed')).toBe(true)
    expect(isErrorLine('FAILED tests/unit/test_x.py::test_y')).toBe(true)
    expect(isErrorLine('process ended with exit code 2')).toBe(true)
  })

  it('does not match an ordinary line, or a zero exit', () => {
    expect(isErrorLine('14:02:09 INFO collected 214 items')).toBe(false)
    expect(isErrorLine('exit code 0')).toBe(false)
    expect(isErrorLine('errors were handled')).toBe(false)
  })
})

describe('search and the mask', () => {
  it('finds lines case-insensitively over this window', () => {
    const lines = linesOf('Traceback one\nok\ntraceback two\n')
    expect(lines).toHaveLength(3)
    expect(searchLines(lines, 'TRACEBACK')).toEqual([0, 2])
    expect(searchLines(lines, '   ')).toEqual([])
  })

  it('draws the server’s mask as its own run, and a hit never splits it', () => {
    const line = `token=${MASK} and Token`
    const segs = segments(line, '*')
    expect(segs.filter((s) => s.kind === 'mask').map((s) => s.text)).toEqual([MASK])
    expect(segs.map((s) => s.text).join('')).toBe(line)
    const hits = segments(line, 'token').filter((s) => s.kind === 'hit').map((s) => s.text)
    expect(hits).toEqual(['token', 'Token'])
  })
})
