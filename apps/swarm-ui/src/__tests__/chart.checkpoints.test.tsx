// The checkpoint dot strip, read back off the circles it drew.
//
// THE DEFECTS THESE EXIST FOR:
//
//   * a checkpoint whose event is off the page drawn AT A TIME -- there is no
//     recorded instant for it, so any x is invented (redesign-v2 §4 row 6);
//   * a checkpoint with no location drawn like one with a location, so a
//     paging artefact reads the same as a located archive;
//   * size mapped to RADIUS, which makes a checkpoint four times as big look
//     sixteen times as big.

import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { CheckpointStrip } from '../charts/CheckpointStrip'
import { at, attempt, ev } from './runfixture'

const a = attempt(1, {
  started_at: at(0),
  completed_at: at(20),
  checkpoints: ['ck_1', 'ck_2', 'ck_3'],
})

const events = [
  ev('checkpoint_completed', at(2), 'att_1', {
    checkpoint_id: 'ck_1',
    uri: 'gs://acme/ck_1.tar.zst',
    size_bytes: 1_000_000,
    seq: 1,
  }),
  // Located on the page but its event carried no uri: hollow, and still placed.
  ev('checkpoint_completed', at(4), 'att_1', {
    checkpoint_id: 'ck_2',
    size_bytes: 4_000_000,
    seq: 2,
  }),
  // ck_3 is on the attempt document and has NO event on this page.
]

function dot(container: HTMLElement, id: string): Element | null {
  return container.querySelector(`[data-testid="ckpt-dot"][data-id="${id}"]`)
}

describe('checkpoint cadence', () => {
  it('places a checkpoint by when it completed, in order along the attempt', () => {
    const { container } = render(<CheckpointStrip attempt={a} events={events} />)
    const one = dot(container, 'ck_1')
    const two = dot(container, 'ck_2')
    expect(one).not.toBeNull()
    expect(two).not.toBeNull()
    expect(Number(one!.getAttribute('cx'))).toBeLessThan(Number(two!.getAttribute('cx')))
    expect(one!.querySelector('title')?.textContent).toContain('T+2m 0s')
  })

  it('sizes a dot by AREA: four times the bytes is twice the radius', () => {
    const { container } = render(<CheckpointStrip attempt={a} events={events} />)
    const r1 = Number(dot(container, 'ck_1')!.getAttribute('r'))
    const r2 = Number(dot(container, 'ck_2')!.getAttribute('r'))
    expect(r2 / r1).toBeCloseTo(2, 6)
  })

  it('draws a checkpoint with no location hollow, and keys it', () => {
    const { container } = render(<CheckpointStrip attempt={a} events={events} />)
    expect(dot(container, 'ck_2')!.getAttribute('class')).toContain('is-hollow')
    expect(dot(container, 'ck_1')!.getAttribute('class')).not.toContain('is-hollow')
    expect(container.querySelector('.ctl-chart-legend')?.textContent).toContain('no location')
  })

  it('puts a checkpoint whose event is off the page in the tray, never on the time axis', () => {
    const { container } = render(<CheckpointStrip attempt={a} events={events} />)
    expect(dot(container, 'ck_3'), 'an unrecorded instant was drawn on the axis').toBeNull()
    const tray = container.querySelector('[data-testid="ckpt-tray"]')
    expect(tray).not.toBeNull()
    const off = tray!.querySelector('[data-testid="ckpt-offpage"][data-id="ck_3"]')
    expect(off, 'the off-page checkpoint was dropped instead of counted').not.toBeNull()
    expect(off!.getAttribute('class')).toContain('is-hollow')
    // Not a broken checkpoint: the sentence says so.
    expect(off!.getAttribute('aria-label') ?? '').toMatch(/not evidence that it is broken/i)
  })

  it('puts every checkpoint in the tray when the event read failed', () => {
    const { container } = render(<CheckpointStrip attempt={a} events={null} />)
    // Read off one drawing: each width is drawn separately (AG-20).
    expect(container.querySelectorAll('[data-testid="ckpt-dot"]')).toHaveLength(0)
    expect(container.querySelectorAll('svg.is-wide [data-testid="ckpt-offpage"]')).toHaveLength(3)
    expect(container.querySelectorAll('svg.is-narrow [data-testid="ckpt-offpage"]')).toHaveLength(3)
  })

  it('draws nothing for an attempt that wrote no checkpoint', () => {
    const { container } = render(
      <CheckpointStrip attempt={attempt(1, { checkpoints: [] })} events={[]} />,
    )
    expect(container.innerHTML).toBe('')
  })
})
