// CONSOLE VISUAL QA PASS 1B, LANE QA1B-12 (overview-agents-24/25; part of #2596).
//
// The over-time fold's title wrapped to three lines and printed a second mean
// of a second limit (`mean 2.8 of 4 cores (the limit the 5 s samples carry;
// the attempt's cgroup limit is 2 cores)`) under the Resources rows' `mean 0.8
// … of 2 cores`. The title now says only what the chart adds -- time at the
// limit and throttling -- and the samples' mean stays in the chart's caption.

import { act, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { UtilisationRead } from '../utilisation'
import { at } from './runfixture'

const api = vi.hoisted(() => ({ loadUtilisation: vi.fn() }))
vi.mock('../api', async (importOriginal) => ({ ...(await importOriginal<typeof import('../api')>()), ...api }))

const { UtilisationPanel } = await import('../UtilisationPanel')

const GIB = 1024 ** 3
const ID = 'task_0123456789abcdef0b12'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() } as Result<T>
}

/** The fixture build's case: the 5 s samples carry 4 cores at a steady 2.8, throttled 6%. */
function read(throttled: number | null): UtilisationRead {
  const p = (i: number) => ({ at: at(30 + i / 12), cpu_cores: 2.8, memory_bytes: 2 * GIB, throttled_share: throttled })
  return {
    task_id: ID,
    read_at: at(36),
    attempts: [
      {
        attempt_id: 'att_3',
        generation: 3,
        cpu_limit_cores: 4,
        memory_limit_bytes: 8 * GIB,
        throttled_seconds: throttled === null ? null : 41.2,
        peak_throttled_share: throttled,
        mean_throttled_share: throttled,
        points: [p(0), p(1), p(2)],
      },
    ],
  } as UtilisationRead
}

afterEach(() => api.loadUtilisation.mockReset())

describe('overview-agents-24/25: the over-time fold’s title is short, with no second mean or limit', () => {
  it('says time at the limit and throttling, and no mean of any limit', async () => {
    api.loadUtilisation.mockResolvedValue(ok(read(0.06)))
    const { container } = render(<UtilisationPanel taskId={ID} live={false} />)
    await act(async () => {})
    const head = container.querySelector('[data-testid="util-headline"]')?.textContent ?? ''
    // MUTATION: put back namedLimit -- `mean 2.8 of 4 cores (the limit the 5 s samples carry; …)` in the title.
    expect(head).toBe('CPU and memory over time · at the limit 0% of the time · throttled 6%')
    expect(head).not.toMatch(/mean|cores|cgroup/)
    // The samples' mean is not lost: it is the chart's own caption, opened.
    expect(container.querySelector('[data-testid="util-summary"]')?.textContent).toContain('mean 2.8 of 4 cores')
  })

  it('says throttling was not measured when the samples carry none', async () => {
    api.loadUtilisation.mockResolvedValue(ok(read(null)))
    const { container } = render(<UtilisationPanel taskId={ID} live={false} />)
    await act(async () => {})
    const head = container.querySelector('[data-testid="util-headline"]')?.textContent ?? ''
    expect(head).toBe('CPU and memory over time · at the limit 0% of the time · throttling not measured')
  })
})
