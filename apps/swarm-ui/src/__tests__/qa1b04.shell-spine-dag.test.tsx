// QA1B-04 (pass 1b of the 2026-10-28 visual QA, part of #2596): the findings
// whose fix sat in Shell.tsx, Spine.tsx or dag.ts, applied from pass 1's
// outside-territory patches.
//
//   * admin-shell-12: a 404 near nothing does not light Overview in the rail.
//   * overview-agents-79: a screen that does not poll says `read once · N ago`.
//   * admin-shell-19: a capacity screen on a phone says `every 1 min on a phone`.
//   * automate-25: a read the API answered (404, 403) offers no `⟳ refresh`.
//   * overview-agents-64: a node whose dependency line is a count is measured
//     as that one line, not the list it replaced.
//   (admin-shell-23, the refusal in place of an Admin page, is held by
//   config.nav.test.tsx, which held CONFIG-NAV's redirect it replaces.)

import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { RefreshControl, Screen } from '../Shell'
import { capacityPoll } from '../capacityPoll'
import { heightOf, DEP_FILES_LISTED } from '../dag'
import { DEP_FILES_LISTED as NODE_DEP_FILES_LISTED } from '../Workflows'
import type { ApiError, ApiErrorKind } from '../fetch'
import type { WorkflowStep } from '../types'

afterEach(() => {
  cleanup()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('admin-shell-12: a 404 is never lit as Overview', () => {
  it('lights no section in the rail over a 404 that fell back to Overview', async () => {
    globalThis.fetch = vi.fn(() =>
      Promise.resolve(new Response('{}', { status: 503, headers: { 'content-type': 'application/json' } })),
    ) as unknown as typeof fetch
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { SkyShell } = await import('../Spine')
    render(
      <SkyShell section="overview" tab="now" title="Not found" go={vi.fn()} foot={null} missing>
        {null}
      </SkyShell>,
    )
    const overview = document.querySelector('.sk-spine a[data-sec="overview"]')
    expect(overview).not.toBeNull()
    // MUTATION: `const on = section === s.key` again and Overview is drawn lit.
    expect(overview!.classList.contains('is-on')).toBe(false)
    expect(document.querySelector('.sk-spine a.sk-ri.is-on')).toBeNull()
  })

  it('still lights Overview on Overview itself', async () => {
    globalThis.fetch = vi.fn(() =>
      Promise.resolve(new Response('{}', { status: 503, headers: { 'content-type': 'application/json' } })),
    ) as unknown as typeof fetch
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { SkyShell } = await import('../Spine')
    render(
      <SkyShell section="overview" tab="now" title="Overview" go={vi.fn()} foot={null}>
        {null}
      </SkyShell>,
    )
    expect(document.querySelector('.sk-spine a[data-sec="overview"]')!.classList.contains('is-on')).toBe(true)
  })
})

describe('overview-agents-79: a screen that does not poll says it was read once', () => {
  it('reads `read once · N ago` without a cadence, and the plain age with one', () => {
    const now = Date.parse('2026-10-28T12:00:00Z')
    // MUTATION: `read ${age} ago` for the cadence-less case and this fails.
    render(<RefreshControl readAt={now - 60_000} now={now} onRefresh={() => {}} />)
    expect(screen.getByRole('button', { name: 'read once · 1 min ago · refresh' })).toBeTruthy()
    cleanup()
    render(<RefreshControl readAt={now} now={now} cadence={{ base: 5_000, wait: 5_000 }} onRefresh={() => {}} />)
    expect(screen.getByRole('button', { name: 'every 5 s · read 0 s ago · refresh' })).toBeTruthy()
  })
})

describe('admin-shell-19: the phone says why its cadence is slower', () => {
  const widths = (phone: boolean) =>
    vi.stubGlobal(
      'matchMedia',
      vi.fn((query: string) => ({
        matches: phone,
        media: query,
        onchange: null,
        addEventListener: () => {},
        removeEventListener: () => {},
        addListener: () => {},
        removeListener: () => {},
        dispatchEvent: () => false,
      })),
    )

  const mount = () =>
    render(
      <Screen<{ n: number }>
        title="Pool limits"
        load={async () => ({ status: 'ok', data: { n: 1 }, fetchedAt: Date.now() })}
        pollMs={capacityPoll(30_000)}
      >
        {() => <p>rows</p>}
      </Screen>,
    )

  it('reads `every 1 min on a phone` below the phone breakpoint', async () => {
    widths(true)
    mount()
    await screen.findByText('rows')
    // MUTATION: drop `cadenceNote` from `setCadence` and the head reads `every 1 min` alone.
    await waitFor(() => expect(document.querySelector('.c-phead .c-refresh')?.textContent).toMatch(/^⟳ every 1 min on a phone · read \d+ s ago$/))
  })

  it('says nothing more on a desktop', async () => {
    widths(false)
    mount()
    await screen.findByText('rows')
    await waitFor(() => expect(document.querySelector('.c-phead .c-refresh')?.textContent).toMatch(/^⟳ every 30 s · read \d+ s ago$/))
  })
})

describe('automate-25: no refresh over an answer', () => {
  function err(kind: ApiErrorKind, httpStatus: number): ApiError {
    return { kind, httpStatus, code: null, message: `The read answered ${httpStatus}.` }
  }

  function mount(error: ApiError) {
    return render(
      <Screen<{ n: number }> title="New schedule" load={async () => ({ status: 'error', error })}>
        {(d) => <p>{d.n}</p>}
      </Screen>,
    )
  }

  it.each([
    ['a 404 on a list read (not served)', { ...err('not_found', 404), template: '/v1/schedule-types' }],
    ['a 404 on an item read', { ...err('not_found', 404), template: '/v1/schedules/{id}' }],
    ['a 403', err('forbidden', 403)],
  ])('%s: the head offers no refresh', async (_name, error) => {
    mount(error)
    await screen.findByText(error.message)
    if (error.kind === 'not_found') expect(document.body.textContent).toContain('Reading it again gets the same answer')
    // MUTATION: test only `admin_required` in the head's guard and `⟳ refresh` sits over the answer.
    expect(screen.queryByRole('button', { name: /refresh/i })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
  })

  it('keeps the refresh where reading again can help', async () => {
    mount(err('server_error', 503))
    await screen.findByText('The read answered 503.')
    expect(screen.getByRole('button', { name: /refresh/i })).toBeTruthy()
  })
})

describe('overview-agents-64: a counted dependency line is measured as one line', () => {
  const step = (depends_on: string[], input_from: Record<string, string>): WorkflowStep => ({
    step_id: 'synthesis',
    runner_profile: 'claude-code',
    resource_class: 'standard',
    depends_on,
    input_from,
    task_id: null,
  })
  const parents = ['cold-start-latency', 'dispatch-throughput', 'fencing-generations', 'quota-park-and-exit', 'tenant-isolation-audit']
  const files: Record<string, string> = Object.fromEntries(parents.map((p) => [p, `${p}-findings.md`]))

  it('a five-file synthesis node is as tall as a node with one short parent', () => {
    // MUTATION: measure `units.map((u) => u.text)` for every node and the
    // synthesis node is several dependency lines taller than the card drawn.
    expect(heightOf(step(parents, files))).toBe(heightOf(step(['plan'], {})))
  })

  it('a node at the limit still names its files, and is measured by them', () => {
    const two = parents.slice(0, DEP_FILES_LISTED)
    expect(heightOf(step(two, Object.fromEntries(two.map((p) => [p, `${p}-findings.md`]))))).toBeGreaterThan(heightOf(step(['plan'], {})))
  })

  it('counts past the same number of files the node draws by', () => {
    // MUTATION: change one of the two constants alone and the height is
    // measured for a line the card does not draw.
    expect(DEP_FILES_LISTED).toBe(NODE_DEP_FILES_LISTED)
  })
})
