/**
 * VISUAL QA LANE L10 (#1038, 2026-10-11): the runner picker and the Submit chooser.
 *
 *   V126  "held by" named its pool at 14px mono (`.mono`) inside the 12px
 *         runner facts and broke the id mid-way at 1280. The holder is mono at
 *         the fact's own size and is kept whole.
 *   V135  "None of yours yet." sat above "Among the newest 0 tasks, 0
 *         workflows, 0 issue runs this tenant has." A window that held nothing
 *         is not named; a tenant with nothing at all says so in one line.
 *   V136  With who you are unread the card said only "who you are is
 *         unknown": the failed task read was not listed and there was no
 *         retry. Every failed read is listed and the card offers Try again.
 *
 * MUTATIONS: put `className="mono"` back on the holder, or drop its
 * `white-space: nowrap`; name a window of 0; drop `unread` from the
 * who-unread state or the Try again button -- each turns a case red.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Result } from '../fetch'
import type { CascadeEnv } from './cssgate'
import { familyOf } from './faces'
import { painted } from './marks'

const api = vi.hoisted(() => ({
  loadMe: vi.fn(),
  loadTasks: vi.fn(),
  loadWorkflows: vi.fn(),
  loadRuns: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  // Only the chooser's four reads are replaced, and only while a case sets
  // them; the task form's reads stay the development fixture's.
  return {
    ...actual,
    loadMe: (...a: unknown[]) => (api.loadMe.getMockImplementation() ? api.loadMe(...a) : actual.loadMe(...(a as []))),
    loadTasks: (...a: unknown[]) => (api.loadTasks.getMockImplementation() ? api.loadTasks(...a) : actual.loadTasks(...(a as []))),
    loadWorkflows: (...a: unknown[]) => (api.loadWorkflows.getMockImplementation() ? api.loadWorkflows(...a) : actual.loadWorkflows(...(a as []))),
    loadRuns: (...a: unknown[]) => (api.loadRuns.getMockImplementation() ? api.loadRuns(...a) : actual.loadRuns(...(a as []))),
  }
})

const { SubmitChooser } = await import('../SubmitChooser')
const { SubmitScreen } = await import('../Submit')

const WAIT = { timeout: 4000 }
const AT_1280: CascadeEnv = { width: 1280 }
const ME = 'someone@saga.xyz'
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}
function failed(message: string): Result<never> {
  return { status: 'error', error: { kind: 'unavailable', httpStatus: 503, code: null, message } } as unknown as Result<never>
}

/** The first font-size the cascade declares on the element or an ancestor. */
function sizeOf(el: Element): string | null {
  for (let node: Element | null = el; node !== null; node = node.parentElement) {
    const v = painted(node, 'font-size', AT_1280)
    if (v === null || /^\s*inherit\s*$/.test(v)) continue
    return v
  }
  return null
}

function chooserReads() {
  api.loadMe.mockImplementation(async () => ok({ principal: { email: ME, domain: 'saga.xyz', groups: [], is_admin: false }, tenant: { tenant_id: 'eng' } }))
  api.loadTasks.mockImplementation(async () => ok({ tasks: [], next_page_token: null }))
  api.loadWorkflows.mockImplementation(async () => ok({ workflows: [] }))
  api.loadRuns.mockImplementation(async () => ok({ runs: [], next_page_token: null }))
}

afterEach(() => {
  vi.resetAllMocks()
})

describe('V126: the pool holding a runner at zero', () => {
  it('is mono at the fact\'s own size, kept whole, with its name in the title', async () => {
    const { container } = render(<SubmitScreen />)
    await screen.findByRole('button', { name: 'Submit one task' }, WAIT)
    const radio = container.querySelector<HTMLInputElement>('input[name="runner-profile"][value="claude-code"]')!
    const room = radio.closest('label')!.querySelector<HTMLElement>('.sbf-runner-room')!
    expect(visible(room)).toMatch(/^0 can start now · held by \S+/)
    const holder = room.querySelector<HTMLElement>('.sbf-runner-holder')!
    expect(holder, 'the holder is not its own element').not.toBeNull()
    expect(holder.classList.contains('mono'), '`.mono` sets 14px').toBe(false)
    expect(holder.getAttribute('title')).toBe(visible(holder))
    expect(familyOf(holder, AT_1280)).toBe('mono')
    expect(sizeOf(holder)).toBe(sizeOf(room))
    expect(sizeOf(holder)).not.toMatch(/--t-body/)
    expect(painted(holder, 'white-space', AT_1280)).toBe('nowrap')
    expect(painted(holder, 'overflow-wrap', AT_1280)).not.toBe('anywhere')
  })
})

describe('V135 and V136: Start from a recent one', () => {
  beforeEach(chooserReads)

  it('names no window of 0, and says nothing has been submitted when none held anything', async () => {
    const { container } = render(<SubmitChooser go={() => {}} />)
    const recent = container.querySelector<HTMLElement>('.sb-recent')!
    await waitFor(() => expect(visible(recent.querySelector('.sb-empty'))).toBe('Nothing has been submitted here yet.'))
    expect(visible(recent)).not.toMatch(/\b0 (tasks?|workflows?|issue runs?)\b/)
    expect(recent.querySelector('.sb-recent-scope')).toBeNull()
  })

  it('names only the windows that held something', async () => {
    api.loadWorkflows.mockImplementation(async () => ok({ workflows: [{ workflow_id: 'wf_x', submitted_by: 'other@saga.xyz', created_at: '2026-10-11T09:00:00Z', steps: [] }] }))
    const { container } = render(<SubmitChooser go={() => {}} />)
    const recent = container.querySelector<HTMLElement>('.sb-recent')!
    await waitFor(() => expect(visible(recent.querySelector('.sb-empty'))).toBe('None of yours yet.'))
    expect(visible(recent.querySelector('.sb-recent-scope'))).toBe('Among 1 workflow this tenant has.')
  })

  it('lists every failed read under an unread who-you-are, and offers Try again', async () => {
    api.loadMe.mockImplementation(async () => failed('Busy.'))
    api.loadTasks.mockImplementation(async () => failed('Tasks timed out.'))
    const { container } = render(<SubmitChooser go={() => {}} />)
    const recent = container.querySelector<HTMLElement>('.sb-recent')!
    await waitFor(() => expect(visible(recent)).toMatch(/who you are is unknown here \(Busy\.\)/))
    expect(visible(recent)).toContain('Tasks not read: Tasks timed out.')
    expect(recent.querySelectorAll('.sb-recent-i').length).toBe(0)

    const retry = within(recent).getByRole('button', { name: 'Try again' })
    const before = api.loadMe.mock.calls.length
    api.loadMe.mockImplementation(async () => ok({ principal: { email: ME, domain: 'saga.xyz', groups: [], is_admin: false }, tenant: { tenant_id: 'eng' } }))
    api.loadTasks.mockImplementation(async () => ok({ tasks: [], next_page_token: null }))
    fireEvent.click(retry)
    await waitFor(() => expect(api.loadMe.mock.calls.length).toBeGreaterThan(before))
    await waitFor(() => expect(visible(recent.querySelector('.sb-empty'))).toBe('Nothing has been submitted here yet.'))
    expect(within(recent).queryByRole('button', { name: 'Try again' })).toBeNull()
  })

  it('offers Try again when a list read failed and who you are was read', async () => {
    api.loadRuns.mockImplementation(async () => failed('Runs unavailable.'))
    const { container } = render(<SubmitChooser go={() => {}} />)
    const recent = container.querySelector<HTMLElement>('.sb-recent')!
    await waitFor(() => expect(visible(recent)).toContain('Issue runs not read: Runs unavailable.'))
    expect(visible(recent.querySelector('.sb-empty'))).toBe('None of yours in what was read.')
    expect(within(recent).getByRole('button', { name: 'Try again' })).toBeTruthy()
  })
})
