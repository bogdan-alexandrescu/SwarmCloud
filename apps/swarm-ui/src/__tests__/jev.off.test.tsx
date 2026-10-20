// LANE JEV-OFF (part of #1345): JEV'S UNAVAILABILITY IS VISIBLE, NOT SILENT.
//
// Measured 2026-10-15 on u-bogdan, whose judgment backend is internal-jev:
// the tailnet egress to your internal decision API is off, so the dispatcher sends no
// JEV_API_URL, the jev wrapper refuses, and the agent drove Playwright. The
// Submit picker had offered Jev and the live chip read "Jev". Measured here:
//
//   * the Submit picker reads `GET /v1/judgment` and draws Jev disabled with
//     the API's own reason when the workspace's backend cannot run -- and,
//     for an internal backend, never asks for a TypeSafe key it does not need;
//   * the live chip reads "Jev off · Playwright" when Jev was picked and the
//     attempt fell back, and its card says why (`no_route` included).
//
// MUTATIONS, each of which turns a case red: enable Jev on an internal
// backend whose `jev.available` is false; drop the reason line; label the
// chip by the selection alone; drop `no_route` from `browserStatusOf`.
//
// Nothing here is credential-shaped.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { Judgment, JevBackend } from '../api'
import type { Result } from '../fetch'
import type { Capacity } from '../types'
import { COMPANY_NAME } from '../../test/companyName'

const api = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadProviderKeys: vi.fn(),
  loadJudgment: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  api.loadCapacity.mockImplementation(actual.loadCapacity)
  return { ...actual, ...api }
})

const actualApi = await vi.importActual<typeof import('../api')>('../api')
const { SubmitScreen } = await import('../Submit')
const { BrowserToolPicker } = await import('../BrowserToolPicker')
const { BrowserBadges, browserStatusOf } = await import('../BrowserBadges')

const WAIT = { timeout: 4000 }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
const TOOLS = ['auto', 'playwright', 'dev-browser', 'jev']
const NO_EGRESS =
  'the tailnet egress to the decision API is not enabled on this platform (INTERNAL_DECISION_API_URL is unset)'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function judgment(backend: JevBackend, available: boolean, reason: string | null): Judgment {
  const label = backend === 'typesafe' ? 'Jev (TypeSafe)' : backend === 'internal-laya' ? 'Jev (Internal Laya)' : 'Jev (Internal)'
  return {
    tenant_id: 'u-bogdan',
    can_edit: true,
    jev: { backend, label, available, reason, default: 'internal-jev', set_at: null, set_by: null, options: [] },
  }
}

function keys(uses: 'user' | 'tenant' | 'none') {
  const blank = { available: true, set: false, set_at: null, set_by: null, version: null }
  return ok({
    provider: 'typesafe',
    user: { ...blank, set: uses === 'user' },
    tenant: { ...blank, set: uses === 'tenant', tenant_id: 'eng' },
    resolution: { tenant_id: 'eng', uses },
    consumed_by: [],
  })
}

async function capacity(): Promise<Result<Capacity>> {
  const r = await actualApi.loadCapacity()
  if (r.status !== 'ok') throw new Error('the fixture capacity did not load')
  const data = structuredClone(r.data)
  const p = data.runner_profiles['claude-code-browser']! as unknown as Record<string, unknown>
  p.input_contract = {
    required_keys: ['prompt'],
    inputs: { browser_tool: { kind: 'string', choices: TOOLS, means: 'which browser tool the agent drives' } },
  }
  return ok(data)
}

async function browserForm() {
  const r = render(<SubmitScreen />)
  await screen.findByRole('button', { name: 'Submit one task' }, WAIT)
  fireEvent.click(r.container.querySelector<HTMLInputElement>('input[name="runner-profile"][value="claude-code-browser"]')!)
  return r.container
}

const picker = () => screen.queryByRole('radiogroup', { name: 'Browser tool' })
const tool = (name: RegExp) => within(picker()!).getByRole('radio', { name })

beforeEach(() => {
  api.loadProviderKeys.mockResolvedValue(keys('none'))
})

afterEach(() => {
  document.body.innerHTML = ''
  vi.clearAllMocks()
})

describe('the Submit picker reads whether Jev can run', () => {
  it('draws Jev disabled with the reason when internal-jev has no route to the decision API', async () => {
    api.loadJudgment.mockResolvedValue(ok(judgment('internal-jev', false, NO_EGRESS)))
    api.loadCapacity.mockResolvedValue(await capacity())
    const c = await browserForm()
    await waitFor(() => expect(visible(c.querySelector('.btp-jev-off'))).toContain('Jev is unavailable'), WAIT)
    expect((tool(/^Jev/) as HTMLInputElement).disabled).toBe(true)
    const why = visible(c.querySelector('.btp-jev-off'))
    expect(why).toContain('Jev (Internal)')
    expect(why).toContain(NO_EGRESS)
    // Not the TypeSafe key's words: an internal backend needs no key.
    expect(why).not.toContain('TypeSafe key')
    expect(api.loadJudgment).toHaveBeenCalled()
  })

  it('enables Jev on an internal backend that can run, with no TypeSafe key at all', async () => {
    api.loadJudgment.mockResolvedValue(ok(judgment('internal-jev', true, null)))
    api.loadCapacity.mockResolvedValue(await capacity())
    const c = await browserForm()
    await waitFor(() => expect((tool(/^Jev/) as HTMLInputElement).disabled).toBe(false), WAIT)
    expect(c.querySelector('.btp-jev-off')).toBeNull()
    fireEvent.click(tool(/^Jev/))
    // Page text stays inside your network: no TypeSafe notice.
    expect(visible(c.querySelector('.btp-notice'))).not.toContain('TypeSafe')
    expect(visible(c.querySelector('.btp-notice'))).toContain('their text stays inside your network.')
    expect(c.querySelector('.btp-hint')?.textContent).toContain('judged inside your network')
    // The company is not named by the picker (#1647).
    expect(c.textContent).not.toMatch(COMPANY_NAME)
  })

  it('keeps Jev off while the backend read is unanswered or failed, never on a guess', () => {
    const { rerender, container } = render(
      <BrowserToolPicker tools={TOOLS} value="auto" onChange={() => {}} typesafe={{ kind: 'read', uses: 'user' }}
        judgment={{ kind: 'reading' }} egress={null} />,
    )
    expect((screen.getByRole('radio', { name: /^Jev/ }) as HTMLInputElement).disabled).toBe(true)
    expect(visible(container.querySelector('.btp-jev-off'))).toBe('Checking whether Jev can run…')
    rerender(
      <BrowserToolPicker tools={TOOLS} value="auto" onChange={() => {}} typesafe={{ kind: 'read', uses: 'user' }}
        judgment={{ kind: 'unread', detail: 'HTTP 503' }} egress={null} />,
    )
    expect((screen.getByRole('radio', { name: /^Jev/ }) as HTMLInputElement).disabled).toBe(true)
    expect(visible(container.querySelector('.btp-jev-off'))).toContain('could not be read')
  })

  it('on the TypeSafe backend still asks for a key where none resolves', () => {
    const { container } = render(
      <BrowserToolPicker tools={TOOLS} value="auto" onChange={() => {}} typesafe={{ kind: 'read', uses: 'none' }}
        judgment={{ kind: 'read', jev: judgment('typesafe', false, 'no key').jev }} egress={null} />,
    )
    expect((screen.getByRole('radio', { name: /^Jev/ }) as HTMLInputElement).disabled).toBe(true)
    expect(visible(container.querySelector('.btp-jev-off'))).toBe('Add a TypeSafe key in Settings › API keys')
  })
})

describe('the live chip says when the attempt fell back', () => {
  const fellBack = (reason: 'no_route' | 'no_key' | 'not_installed') =>
    browserStatusOf({
      tool_selected: 'jev',
      tools_available: ['playwright'],
      tool_last_used: null,
      jev: { enabled: false, reason, key_scope: null },
      egress: { mode: 'direct', country: null, state: 'off', last_error: null },
    })

  it.each(['no_route', 'no_key', 'not_installed'] as const)('reads "Jev off · Playwright" (%s)', (reason) => {
    const s = fellBack(reason)
    expect(s, 'the shape is taken, reason and all').not.toBeNull()
    const { container } = render(<BrowserBadges status={s} />)
    const b = container.querySelector<HTMLElement>('[data-testid="browser-badge-tool"]')!
    expect(b.querySelector('.bb-label')?.textContent).toBe('Jev off · Playwright')
    expect(b.classList.contains('is-warn')).toBe(true)
    expect(container.querySelector('[data-testid="browser-badge-jev"]')).toBeNull()
  })

  it('says why on focus: no route to the decision API', () => {
    const { container } = render(<BrowserBadges status={fellBack('no_route')} />)
    const b = container.querySelector<HTMLElement>('[data-testid="browser-badge-tool"]')!
    fireEvent.focus(b)
    const card = document.getElementById(b.getAttribute('aria-describedby') ?? '')!
    expect(card.textContent).toMatch(/judges with your internal decision API, and this task has no route to it/)
    // The company is not named on the chip (#1647).
    expect(card.textContent).not.toMatch(COMPANY_NAME)
    expect(card.textContent).toMatch(/tailnet egress is off/)
  })

  it('still reads "Jev" while Jev is on', () => {
    const s = browserStatusOf({
      tool_selected: 'jev',
      tools_available: ['jev', 'playwright'],
      tool_last_used: null,
      jev: { enabled: true, reason: null, key_scope: null },
      egress: { mode: 'direct', country: null, state: 'off', last_error: null },
    })
    const { container } = render(<BrowserBadges status={s} />)
    expect(container.querySelector('[data-testid="browser-badge-tool"] .bb-label')?.textContent).toBe('Jev')
  })
})
