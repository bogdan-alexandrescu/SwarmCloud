// AG-5 (#82). THE MASKED-CREDENTIAL COUNT IS A MEASUREMENT, AND IT IS DRAWN AS ONE.
//
// THE DEFECT. The artifact viewer drew `masked 4` with the `not read` mark --
// the dashed silhouette whose one meaning is "the read failed" -- and dimmed
// the fact with `.is-absent`, the treatment for a figure nothing measured.
// The count WAS read: `redaction_count` is what the serve path's `redact()`
// returned over these bytes. A reader who knows the kit reads that line as
// "we could not tell whether this artifact held credentials", which is the
// opposite of what it says.
//
// THE OWNER'S DECISION, 2026-09-25: a plain measured fact, in `--warn` ink
// when above zero and plain ink at zero, with no mark and no dimming, linked
// to the help topic about what masking does. No seventh mark kind: the six
// marks are six kinds of NOTHING, and a count that was read is something.
//
// The render half of the above-zero case is `prose.runs.test.tsx`, which
// pinned the defect and was re-pointed. This file holds the zero case and the
// ink, through the cascade rather than `getComputedStyle` (design-system.md
// §14.1: jsdom orders rules by source position alone).
//
// MUTATION: drop the `is-warn` modifier, paint `.art-masked` in `--warn`
// unconditionally, or put the mark back. One of the three cases below fails.

import STYLES from '../styles.css?raw'
import { describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import type { ArtifactContent, ArtifactRef } from '../types'
import { HELP } from '../help'
import { cascade } from './cssgate'

const api = vi.hoisted(() => ({ loadArtifactContent: vi.fn() }))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

import { ArtifactViewer } from '../ArtifactViewer'

const REF: ArtifactRef = { name: 'transcript.json', bytes: 2048, uri: 'gs://acme/art/transcript.json' }

function artifact(over: Partial<ArtifactContent>): ArtifactContent {
  return {
    task_id: 'tsk_masked',
    tenant_id: 'acme',
    attempt_id: 'att_1',
    artifact: REF,
    status: 'ok',
    detail: null,
    key: null,
    uri: REF.uri,
    content: '{"turns": []}\n',
    total_bytes: 14,
    offset: 0,
    returned_bytes: 14,
    next_offset: null,
    truncated: false,
    redacted: false,
    redaction_count: 0,
    redaction: { applied_at_read_time: true, rules: 12 },
    ...over,
  }
}

async function masked(over: Partial<ArtifactContent>): Promise<{ fact: Element; count: Element }> {
  api.loadArtifactContent.mockResolvedValue({ status: 'ok', data: artifact(over), fetchedAt: Date.now() })
  const { container } = render(<ArtifactViewer taskId="tsk_masked" artifact={REF} onClose={() => {}} />)
  return waitFor(() => {
    const fact = container.querySelector('.ctl-fact.art-redacted')
    expect(fact, 'the masked fact is not on the viewer').not.toBeNull()
    const count = fact!.querySelector('.art-masked')
    expect(count, 'the count has no element of its own to carry its ink').not.toBeNull()
    return { fact: fact!, count: count! }
  })
}

function ink(el: Element): string | null {
  const r = cascade(STYLES, el, 'color', { width: 1440 })
  expect(r.unsupported, 'selectors the resolver could not evaluate').toEqual([])
  return r.winner?.value ?? null
}

describe('the masked-credential count', () => {
  it('above zero: the figure, in --warn, with no mark and no dimming', async () => {
    const { fact, count } = await masked({ redacted: true, redaction_count: 4 })
    expect(count.textContent).toBe('4')
    expect(fact.querySelector('.ctl-mark'), 'a measured count wears an absence mark').toBeNull()
    expect(fact.classList.contains('is-absent')).toBe(false)
    expect(ink(count), 'four masked credentials are drawn in plain ink').toBe('var(--warn)')
  })

  it('at zero: the figure, in plain ink, still with no mark', async () => {
    const { fact, count } = await masked({ redacted: false, redaction_count: 0 })
    expect(count.textContent).toMatch(/^0\b/)
    expect(count.classList.contains('is-warn'), 'a zero is drawn as needing attention').toBe(false)
    expect(fact.querySelector('.ctl-mark')).toBeNull()
    expect(fact.classList.contains('is-absent')).toBe(false)
    expect(ink(count), 'a zero count is painted a state colour').toBe('var(--text)')
  })

  it('keeps its route to the words: the `?` opens the serve-time masking topic', async () => {
    const { fact } = await masked({ redacted: true, redaction_count: 1 })
    const topic = HELP['masking-is-serve-time'].title
    const glyph = [...fact.querySelectorAll('button[aria-expanded]')].find((b) =>
      (b.getAttribute('aria-label') ?? '').includes(topic),
    )
    expect(glyph, 'the count links to no help topic').toBeDefined()
  })
})
