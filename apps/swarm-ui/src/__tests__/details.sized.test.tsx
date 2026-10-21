// SIZES-DETAIL (docs/design/runner-sizes.md §6; part of #1689): DETAILS SAYS
// WHAT THE ATTEMPT WAS GUARANTEED AND WHAT IT COULD BURST TO.
//
// Since RS-1 a GKE pod's CPU limit may be twice its request, and since RS-4
// the scheduler records the size it rendered on the attempt (`sized_*`).
// Details showed one number, the limit, and the head's class size beside it,
// so a 4-core request bursting to 8 read as "of 8 cores" with nothing saying
// 4 of them were the guarantee. These tests hold the words that say it, read
// from the attempt's own record and never from the class read here (after a
// resize the class no longer says what an older attempt ran at):
//
//   GKE, limit above request   `4 cores guaranteed, bursts to 8`
//   GKE, limit == request      `4 cores guaranteed, no burst`
//   Cloud Run                  `4 cores guaranteed, no burst on Cloud Run`:
//                              Cloud Run renders the request as its only
//                              number (§2.5), whatever the record's limit says
//   live browser pod           the agent's own container, then the display's:
//                              the pod's 4 / 8 is not what the agent got
//   no sized fields            exactly as before this change
//
// The words appear in three places, from one helper: the CPU tile's sub-line,
// the Resources card's CPU peak row, and each attempt's CPU strip.

import { describe, expect, it, vi } from 'vitest'
import { render, waitFor } from '@testing-library/react'

import { type AgentRun } from '../api'
import type { AttemptRow, Task } from '../types'
import { attempt, task } from './runfixture'

const api = vi.hoisted(() => ({
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, ...api }
})

const { Run } = await import('../AgentDetail')

const WAIT = { timeout: 5000 }

/** The worker's typed CPU fields: the kernel's limit of 8, read from `cpu.max`. */
const MEASURED = {
  cpu_seconds: 402.311,
  peak_cpu_cores: 5.5,
  mean_cpu_cores: 1.74,
  cpu_limit_cores: 8,
  cpu_limit_source: 'cgroup',
}

/** RS-4's record of a standard GKE pod at the §4.1 defaults. */
const SIZED = {
  size_version: 0,
  sized_cpu_request: 4,
  sized_cpu_limit: 8,
  sized_memory_gib: 8,
  sized_disk_gib: 4,
  sized_live_split: null,
}

/** The live browser pod's two containers (BROWSER-SPLIT's 2 / 2, §4.1's limits). */
const LIVE_SPLIT = {
  worker: { cpu_request: 2, cpu_limit: 4, memory_gib: 3 },
  display: { cpu_request: 2, cpu_limit: 3, memory_gib: 5 },
}

function row(sized: Record<string, unknown> | null, over: Partial<AttemptRow> = {}): AttemptRow {
  const a = attempt(1, { backend: 'GKE_AUTOPILOT', ...over })
  return Object.assign(a, MEASURED, sized ?? {})
}

function agentRun(a: AttemptRow, t: Partial<Task> = {}): AgentRun {
  return {
    task: task({ state: 'SUCCEEDED', attempt_count: 1, ...t }),
    events: [],
    eventsDetail: null,
    attempts: [a],
    attemptsDetail: null,
    classes: { standard: { name: 'standard', cpu: 4, memory_gib: 8, disk_gib: 4, units: 1 } },
    classesDetail: null,
    classesRouteMissing: false,
  }
}

async function mount(a: AttemptRow, t: Partial<Task> = {}): Promise<HTMLElement> {
  api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
  const { container } = render(<Run run={agentRun(a, t)} />)
  await waitFor(() => expect(container.querySelector('.dt-strip')).not.toBeNull(), WAIT)
  return container as HTMLElement
}

/** The CPU tile's sub-line in the Details strip. */
function tileSub(root: HTMLElement): string {
  const cell = [...root.querySelectorAll<HTMLElement>('.dt-sc')].find((c) =>
    (c.querySelector('.dt-sc-l')?.textContent ?? '').startsWith('CPU'),
  )
  expect(cell, 'no CPU tile').toBeTruthy()
  return cell!.querySelector('.dt-sc-s')?.textContent ?? ''
}

/** The Resources card's CPU peak row, its note. */
function peakNote(root: HTMLElement): string {
  const r = [...root.querySelectorAll<HTMLElement>('.dt-rr')].find((x) => x.querySelector('.dt-rr-n')?.textContent === 'CPU peak')
  expect(r, 'no CPU peak row').toBeTruthy()
  return r!.querySelector('.dt-rr-v small')?.textContent ?? ''
}

/** The attempt card's CPU strip, under its two rows. */
function strip(root: HTMLElement): string {
  const note = root.querySelector<HTMLElement>('.att-cpu-note')
  expect(note, 'no CPU strip').not.toBeNull()
  return note!.textContent ?? ''
}

describe('Details says what an attempt was guaranteed and what it could burst to', () => {
  it('reads a GKE attempt sized 4 / 8 as "4 cores guaranteed, bursts to 8" in the tile, the Resources row and the strip', async () => {
    const root = await mount(row(SIZED))
    // MUTATION: drop the sized words from `cpuCell`'s sub-line -> red.
    expect(tileSub(root)).toContain('4 cores guaranteed, bursts to 8')
    // MUTATION: drop them from the Resources card's CPU peak note -> red.
    expect(peakNote(root)).toContain('4 cores guaranteed, bursts to 8')
    // MUTATION: drop them from `CpuNote`'s extra list -> red.
    expect(strip(root)).toContain('4 cores guaranteed, bursts to 8')
  })

  it('reads the attempt’s own record, not the class read here, after a resize', async () => {
    // The class read here is 4 cores; this attempt was admitted at a version
    // that made standard 6 / 12.
    const root = await mount(row({ ...SIZED, size_version: 2, sized_cpu_request: 6, sized_cpu_limit: 12 }))
    // MUTATION: take the request from `cls.cpu` instead of `sized_cpu_request` -> red.
    expect(tileSub(root)).toContain('6 cores guaranteed, bursts to 12')
    expect(tileSub(root)).not.toContain('4 cores guaranteed')
  })

  it('says "no burst" when the recorded limit is the request', async () => {
    const root = await mount(row({ ...SIZED, sized_cpu_limit: 4 }))
    // MUTATION: always print "bursts to <limit>" -> red.
    expect(tileSub(root)).toContain('4 cores guaranteed, no burst')
    expect(tileSub(root)).not.toMatch(/bursts to/)
  })

  it('never claims a burst on Cloud Run, which renders the request as its only number', async () => {
    const root = await mount(row(SIZED, { backend: 'CLOUD_RUN_JOB' }))
    // MUTATION: ignore `backend` and print the record's limit -> red.
    expect(tileSub(root)).toContain('4 cores guaranteed, no burst on Cloud Run')
    expect(tileSub(root)).not.toMatch(/bursts to/)
  })

  it('names the agent’s own container and the display’s on a live browser pod, never the pod’s 4 / 8', async () => {
    const root = await mount(row({ ...SIZED, sized_live_split: LIVE_SPLIT }))
    // MUTATION: ignore `sized_live_split` and print the pod's request and limit -> red.
    expect(tileSub(root)).toContain('agent 2 cores guaranteed, bursts to 4 · display 2, bursts to 3')
    expect(tileSub(root)).not.toContain('bursts to 8')
  })

  it('shows the size on a running attempt before its first reading: it was pinned at admission', async () => {
    const a = row(SIZED, { completed_at: null, exit_code: null })
    Object.assign(a, { cpu_seconds: null, peak_cpu_cores: null, mean_cpu_cores: null, cpu_limit_cores: null, cpu_limit_source: null })
    const root = await mount(a, { state: 'RUNNING', current_lease_id: 'lse_1' })
    // MUTATION: add the sized words only on the measured branch of `cpuCell` -> red.
    expect(tileSub(root)).toContain('4 cores guaranteed, bursts to 8')
  })

  it('reads an attempt without sized fields exactly as before', async () => {
    const before = await mount(row(null))
    const sub = tileSub(before)
    const note = peakNote(before)
    const cpuStrip = strip(before)
    // MUTATION: fall back to the class's cpu when `sized_cpu_request` is absent -> red.
    for (const text of [sub, note, cpuStrip]) {
      expect(text).not.toMatch(/guaranteed|burst/)
    }
    // The words of today, pinned so an addition anywhere in them is seen.
    expect(sub).toBe('peak · mean 1.7 · cgroup limit (class 4 vCPU)')
  })

  it('reads a malformed record as no record', async () => {
    const root = await mount(row({ ...SIZED, sized_cpu_request: 'four' }))
    // MUTATION: print `sized_cpu_request` without checking it is a finite number -> red.
    expect(tileSub(root)).not.toMatch(/guaranteed|burst/)
  })
})
