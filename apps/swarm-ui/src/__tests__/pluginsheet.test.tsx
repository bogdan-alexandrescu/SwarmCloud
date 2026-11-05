// A workflow step's plugin, chosen in the guided sheet (PluginSheet.tsx; the
// owner picked direction B on 4 November 2026; docs/design/agent-plugins.md).
//
//   * the sheet's three parts: 1 the plugin, only those the workspace allows
//     pickable and each saying why; 2 the mode, one that does not run yet
//     refused in the platform's words; 3 the skills the mode offers;
//   * the refusals are swarm_api.plugins.check_step's own words, in its order;
//   * the pick reaches the submitted step as its own `plugins` field (the
//     owner's pick on PR #3264), never as a work contract the console writes;
//   * nothing is read until a sheet is opened.
//
// MUTATIONS, each of which turns a case red: offer a plugin the workspace has
// not allowed; let a planner mode be picked; reword a refusal; send the pick
// anywhere but the step's `plugins` field; read /v1/plugins on mount.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity } from '../types'

const mocks = vi.hoisted(() => ({
  loadCapacity: vi.fn(),
  loadMe: vi.fn(),
}))

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  mocks.loadCapacity.mockImplementation(actual.loadCapacity)
  return { ...actual, loadCapacity: mocks.loadCapacity, loadMe: mocks.loadMe, USE_FIXTURES: false }
})

const actualApi = await vi.importActual<typeof import('../api')>('../api')
const { SubmitWorkflowScreen } = await import('../SubmitWorkflow')
const {
  PluginSheet, stepPluginPlan, stepPluginProblem, stepPluginsField,
} = await import('../PluginSheet')
type PluginsView = import('../PluginSheet').PluginsView

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const realFetch = globalThis.fetch
const BRIDGE = 'runs an interview with a person, which needs the question bridge; until it lands only unattended mode runs'
const PROFILES = ['claude-code', 'claude-code-large', 'claude-code-review', 'claude-code-browser', 'claude-code-lightpanda']

function view(allowed: string[] = ['gstack']): PluginsView {
  return {
    tenant_id: 'eng', plugins: { allowed }, defaults: { plugins: { allowed: [] } },
    set_at: null, set_by: null, can_edit: false,
    catalogue: [
      { name: 'gstack', version: '1.92.55.0', runner_profiles: PROFILES, modes: {
        unattended: { available: true, skills: ['review', 'investigate', 'health'] },
        planner: { available: false, reason: BRIDGE, skills: ['office-hours', 'spec'] },
        designer: { available: false, reason: BRIDGE, skills: ['design-html'] },
      } },
      { name: 'security-audit', version: 'c1c8a8c1471069fb0e188eeaff69b8e8db6564a8', runner_profiles: PROFILES, modes: {
        unattended: { available: true, skills: ['security-audit'] },
      } },
    ],
  }
}

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

describe("check_step's refusals, in its words", () => {
  it('names a profile that cannot load the plugin', () => {
    expect(stepPluginProblem(view(), { name: 'gstack', mode: 'unattended', skills: ['review'] }, 'codex'))
      .toBe(`gstack runs only on the profiles ${PROFILES.join(', ')}, not codex`)
  })
  it('refuses a mode that needs the question bridge', () => {
    expect(stepPluginProblem(view(), { name: 'gstack', mode: 'planner', skills: ['spec'] }, 'claude-code'))
      .toBe(`gstack in planner mode ${BRIDGE}`)
  })
  it('refuses a skill the mode does not offer', () => {
    expect(stepPluginProblem(view(), { name: 'gstack', mode: 'unattended', skills: ['spec', 'review'] }, 'claude-code'))
      .toBe('gstack in unattended mode does not offer spec')
  })
  it('refuses a plugin the workspace has not allowed', () => {
    expect(stepPluginProblem(view([]), { name: 'gstack', mode: 'unattended', skills: ['review'] }, 'claude-code'))
      .toBe("this workspace has not allowed gstack: its owner or admin allows it in the workspace's plugins setting")
  })
  it('refuses a name outside the catalogue, and accepts an allowed pick', () => {
    expect(stepPluginProblem(view(), { name: 'nope', mode: 'unattended', skills: ['x'] }, 'claude-code'))
      .toBe("'nope' is not in the plugin catalogue")
    expect(stepPluginProblem(view(), { name: 'gstack', mode: 'unattended', skills: ['review'] }, 'claude-code')).toBeNull()
  })
})

describe("the step's plugins field", () => {
  it('names the plugin with its mode and skills, nothing else', () => {
    expect(stepPluginsField({ name: 'gstack', mode: 'unattended', skills: ['review', 'health'] }))
      .toEqual({ gstack: { mode: 'unattended', skills: ['review', 'health'] } })
  })
  it('is what the plan sends for a pick the platform runs', () => {
    const pick = { name: 'gstack', mode: 'unattended', skills: ['review'] }
    expect(stepPluginPlan(pick, { stepId: 's-1', profile: 'claude-code' }, { view: ok(view()) }))
      .toEqual({ ok: true, plugins: { gstack: { mode: 'unattended', skills: ['review'] } } })
  })
  it('is not sent while the plugins are unread, or for a pick check_step refuses', () => {
    const pick = { name: 'gstack', mode: 'unattended', skills: ['review'] }
    expect(stepPluginPlan(pick, { stepId: 's-1', profile: 'claude-code' }, { view: null }).ok).toBe(false)
    expect(stepPluginPlan(pick, { stepId: 's-1', profile: 'codex' }, { view: ok(view()) }).ok).toBe(false)
  })
})

describe('the guided sheet', () => {
  afterEach(() => { document.body.innerHTML = '' })

  it('offers only allowed plugins, refuses a mode that does not run yet, and hands back the pick', () => {
    const onUse = vi.fn()
    render(<PluginSheet stepId="review-1" profile="claude-code" pick={null} ctx={{ view: ok(view()) }}
      onUse={onUse} onClose={() => {}} />)
    const sheet = screen.getByRole('dialog', { name: 'review-1' })
    const gstack = within(sheet).getByRole('radio', { name: /^gstack/ }) as HTMLInputElement
    const audit = within(sheet).getByRole('radio', { name: /^security-audit/ }) as HTMLInputElement
    expect(gstack.checked).toBe(true)
    expect(audit.disabled).toBe(true)
    expect(sheet.textContent).toContain('this workspace has not allowed security-audit')

    const planner = within(sheet).getByRole('radio', { name: /Planner/ }) as HTMLInputElement
    expect(planner.disabled).toBe(true)
    expect(sheet.textContent).toContain(`Not yet: gstack in planner mode ${BRIDGE}.`)

    const use = within(sheet).getByRole('button', { name: 'Use on this step' }) as HTMLButtonElement
    expect(use.disabled, 'no skill ticked yet').toBe(true)
    fireEvent.click(within(sheet).getByRole('checkbox', { name: /investigate/ }))
    fireEvent.click(within(sheet).getByRole('checkbox', { name: /review/ }))
    expect(sheet.textContent).toContain('2 of 3')
    expect(within(sheet).getByLabelText('What the step will show').textContent).toContain('review · investigate')
    fireEvent.click(use)
    expect(onUse).toHaveBeenCalledWith({ name: 'gstack', mode: 'unattended', skills: ['review', 'investigate'] })
  })

  it("says a profile that cannot load a plugin in check_step's words", () => {
    render(<PluginSheet stepId="s" profile="codex" pick={null} ctx={{ view: ok(view()) }}
      onUse={() => {}} onClose={() => {}} />)
    const sheet = screen.getByRole('dialog')
    expect(sheet.textContent).toContain(`gstack runs only on the profiles ${PROFILES.join(', ')}, not codex.`)
    expect((within(sheet).getByRole('radio', { name: /^gstack/ }) as HTMLInputElement).disabled).toBe(true)
  })

  it('closes on Escape', () => {
    const onClose = vi.fn()
    render(<PluginSheet stepId="s" profile="claude-code" pick={null} ctx={{ view: ok(view()) }}
      onUse={() => {}} onClose={onClose} />)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onClose).toHaveBeenCalled()
  })
})

describe('the workflow builder', () => {
  const posts: Array<Record<string, unknown>> = []
  const gets: string[] = []

  beforeEach(async () => {
    posts.length = 0
    gets.length = 0
    const r = await actualApi.loadCapacity()
    if (r.status !== 'ok') throw new Error('the fixture capacity did not load')
    mocks.loadCapacity.mockResolvedValue(ok<Capacity>(r.data))
    mocks.loadMe.mockResolvedValue(ok({ tenant: { tenant_id: 'eng' }, principal: { email: 'ana@example.com', domain: 'example.com', groups: [], is_admin: false } }))
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if ((init?.method ?? 'GET') === 'POST' && url.startsWith('/v1/workflows')) {
        posts.push(JSON.parse(String(init?.body ?? 'null')) as Record<string, unknown>)
        return new Response(JSON.stringify({ workflow: { workflow_id: 'wf_p_1', steps: [], created_at: '2026-11-04T12:00:00Z' }, dispatch: null }),
          { status: 201, headers: JSON_HEADERS })
      }
      if (url.startsWith('/v1/plugins')) {
        gets.push(url)
        return new Response(JSON.stringify(view()), { status: 200, headers: JSON_HEADERS })
      }
      throw new Error(`unexpected request ${url}`)
    }) as unknown as typeof fetch
  })

  afterEach(() => {
    globalThis.fetch = realFetch
    document.body.innerHTML = ''
    vi.clearAllMocks()
  })

  it("puts the sheet's pick on the step as one chip and sends it as the step's plugins field", async () => {
    const { container } = render(<SubmitWorkflowScreen />)
    await screen.findByRole('button', { name: 'Submit this workflow' }, WAIT)
    const step = container.querySelector<HTMLElement>('.wfb-step')!
    fireEvent.change(step.querySelector<HTMLSelectElement>('select.sb-runner-select')!, { target: { value: 'claude-code' } })
    fireEvent.change(container.querySelector<HTMLElement>('.wfb-step')!.querySelector<HTMLTextAreaElement>('textarea')!, { target: { value: 'review the change' } })
    expect(gets, 'nothing is read before a sheet opens').toEqual([])

    fireEvent.click(screen.getByRole('button', { name: /Add a plugin to/ }))
    const sheet = await screen.findByRole('dialog', {}, WAIT)
    const review = await within(sheet).findByRole('checkbox', { name: /review/ }, WAIT)
    fireEvent.click(review)
    fireEvent.click(within(sheet).getByRole('button', { name: 'Use on this step' }))

    const chip = await screen.findByRole('button', { name: /Change the plugin: gstack, Unattended mode: review/ }, WAIT)
    expect(chip.textContent).toBe('gstackUnattendedreview')

    const go = screen.getByRole('button', { name: 'Submit this workflow' }) as HTMLButtonElement
    await waitFor(() => expect(go.disabled).toBe(false), WAIT)
    fireEvent.click(go)
    await waitFor(() => expect(posts).toHaveLength(1), WAIT)
    const sent = (posts[0]!.steps as Array<Record<string, unknown>>)[0]!
    expect(sent.plugins).toEqual({ gstack: { mode: 'unattended', skills: ['review'] } })
    expect('metadata' in sent, 'no work contract is written by the console').toBe(false)
    expect(gets).toHaveLength(1)
  })

  it('sends no plugins and no metadata for a step with no plugin', async () => {
    const { container } = render(<SubmitWorkflowScreen />)
    await screen.findByRole('button', { name: 'Submit this workflow' }, WAIT)
    fireEvent.change(container.querySelector<HTMLSelectElement>('select.sb-runner-select')!, { target: { value: 'claude-code' } })
    fireEvent.change(container.querySelector<HTMLTextAreaElement>('.wfb-step textarea')!, { target: { value: 'plain' } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit this workflow' }))
    await waitFor(() => expect(posts).toHaveLength(1), WAIT)
    expect('metadata' in (posts[0]!.steps as Array<Record<string, unknown>>)[0]!).toBe(false)
    expect('plugins' in (posts[0]!.steps as Array<Record<string, unknown>>)[0]!).toBe(false)
    expect(gets).toEqual([])
  })
})
