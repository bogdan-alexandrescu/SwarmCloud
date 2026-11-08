// The workflow builder's managed plugins (docs/design/managed-plugins.md, lane
// 10d; sections 6.4 and 7; the owner's cards direction, 6 November 2026):
//
//   * the workspace's every-step defaults are chips on the step card, each
//     switching off for this step;
//   * a managed plugin is added to one step from the step plugins sheet;
//   * a non-secret setting is overridden for that step only;
//   * what the builder sends is the step's `plugins` and `plugins_off`, the
//     fields swarm_api.schemas.WorkflowStepCreate declares and
//     swarm_api.plugins.merge_step_plugins reads.
//
// MUTATIONS, each of which turns a case red: draw no chip for a default; send
// a turned-off default anywhere but `plugins_off`; send a default with no
// override in `plugins`; send an override for a secret; name a field the
// server's step schema does not declare.

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'

import type { Result } from '../fetch'
import type { Capacity, ManagedPluginInstall, PluginConfigView, PluginFormField } from '../types'

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
const sheet = await import('../PluginSheet')
type PluginsView = import('../PluginSheet').PluginsView
type ManagedPluginsRead = import('../PluginSheet').ManagedPluginsRead

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const realFetch = globalThis.fetch
const TENANT = 'team-a'
const PROFILES = ['claude-code', 'claude-code-large']
const DEFAULT_ID = 'p-1a2b3c4d5e6f'
const ADDED_ID = 'w-0a1b2c3d4e5f'
const DENIED_ID = 'p-6f5e4d3c2b1a'

function ok<T>(data: T): Result<T> {
  return { status: 'ok', data, fetchedAt: Date.now() }
}

function view(): PluginsView {
  return {
    tenant_id: TENANT, plugins: { allowed: ['gstack'] }, defaults: { plugins: { allowed: [] } },
    set_at: null, set_by: null, can_edit: false,
    catalogue: [{ name: 'gstack', version: '1.0.0', runner_profiles: PROFILES, modes: {
      unattended: { available: true, skills: ['review'] },
    } }],
  }
}

function install(id: string, name: string, scope: 'platform' | 'workspace', allowed?: boolean): ManagedPluginInstall {
  return {
    install_id: id, name, display_name: name, scope, tenant_id: scope === 'workspace' ? TENANT : null, built_in: false,
    state: 'active', current_version: `v-${id.slice(2)}`, version: '1.0.0', description: null, hosts: [],
    components: { hooks: 1 }, ...(allowed === undefined ? {} : { allowed }),
  }
}

function managed(defaults: string[] = [DEFAULT_ID]): ManagedPluginsRead {
  return {
    catalogue: {
      scope: 'workspace', tenant_id: TENANT, built_in: [], can_install: false,
      platform_installs: [install(DEFAULT_ID, 'rules-gate', 'platform', true), install(DENIED_ID, 'trace-log', 'platform', false)],
      installs: [install(ADDED_ID, 'lint-notes', 'workspace')],
    },
    defaults,
  }
}

function field(key: string, type: PluginFormField['type'], over: Partial<PluginFormField> = {}): PluginFormField {
  return {
    key, source: 'user_config', type, kind: type === 'directory' ? 'path' : 'value', title: null, description: null,
    required: false, multiple: false, sensitive: false, default: null, ...over,
  }
}

function form(installId: string): PluginConfigView {
  return {
    tenant_id: TENANT, missing: [], can_edit: false,
    form: { install_id: installId, version_id: `v-${installId.slice(2)}`, fields: [
      field('strictness', 'string', { default: 'normal' }),
      field('notes_dir', 'directory'),
      field('api_key', 'string', { kind: 'secret', sensitive: true }),
    ] },
    values: { strictness: { set: true, value: 'normal' }, notes_dir: { set: false }, api_key: { set: true } },
  }
}

/** The field names a Pydantic class in swarm_api/schemas.py declares, read from its source. */
function serverFields(cls: string): string[] {
  const src = readFileSync(resolve(__dirname, '../../../swarm-api/swarm_api/schemas.py'), 'utf8')
  const start = src.indexOf(`class ${cls}(`)
  if (start === -1) throw new Error(`schemas.py declares no ${cls}`)
  const rest = src.slice(start).split('\n').slice(1)
  const end = rest.findIndex((line) => /^\S/.test(line))
  const body = rest.slice(0, end === -1 ? undefined : end)
  return body.map((line) => /^ {4}([a-z_][a-z0-9_]*)\s*:/.exec(line)?.[1]).filter((n): n is string => n !== undefined)
}

describe("the step's managed plugins as merge_step_plugins reads them", () => {
  const ctx = (defaults?: string[]) => ({ view: ok(view()), managed: ok(managed(defaults)), forms: {} })

  it('sends a default turned off for this step in plugins_off and nothing in plugins', () => {
    expect(sheet.stepManagedPlan({ added: {}, off: [DEFAULT_ID] }, null, ctx()))
      .toEqual({ ok: true, plugins: {}, off: [DEFAULT_ID] })
  })

  it('sends an added plugin by install id, with its overrides when it has any', () => {
    expect(sheet.stepManagedPlan({ added: { [ADDED_ID]: {} }, off: [] }, null, ctx()))
      .toEqual({ ok: true, plugins: { [ADDED_ID]: {} }, off: [] })
    expect(sheet.stepManagedPlan({ added: { [ADDED_ID]: { strictness: 'high' } }, off: [] }, null, ctx()))
      .toEqual({ ok: true, plugins: { [ADDED_ID]: { overrides: { strictness: 'high' } } }, off: [] })
  })

  it('names a default with overrides in plugins, never also in plugins_off, and sends no copy of a default without', () => {
    expect(sheet.stepManagedPlan({ added: { [DEFAULT_ID]: { strictness: 'high' } }, off: [DEFAULT_ID] }, null, ctx()))
      .toEqual({ ok: true, plugins: { [DEFAULT_ID]: { overrides: { strictness: 'high' } } }, off: [] })
    expect(sheet.stepManagedPlan({ added: { [DEFAULT_ID]: {} }, off: [] }, null, ctx()))
      .toEqual({ ok: true, plugins: {}, off: [] })
  })

  it("turns off no built-in default the step picks itself, and nothing that is not a default", () => {
    expect(sheet.stepManagedPlan({ added: {}, off: ['gstack', 'p-000000000000'] }, 'gstack', ctx(['gstack'])))
      .toEqual({ ok: true, plugins: {}, off: [] })
  })

  it('refuses a platform plugin the workspace does not allow, and anything while the read is missing', () => {
    expect(sheet.stepManagedPlan({ added: { [DENIED_ID]: {} }, off: [] }, null, ctx()))
      .toEqual({ ok: false, message: 'trace-log: the workspace does not allow that plugin' })
    expect(sheet.stepManagedPlan({ added: {}, off: [DEFAULT_ID] }, null, { view: null }).ok).toBe(false)
    expect(sheet.stepManagedPlan(null, null, { view: null })).toEqual({ ok: true, plugins: {}, off: [] })
  })

  it("refuses an override check_value refuses, in its words", () => {
    const withForm = { ...ctx(), forms: { [ADDED_ID]: ok(form(ADDED_ID)) } }
    expect(sheet.stepManagedPlan({ added: { [ADDED_ID]: { notes_dir: '../out' } }, off: [] }, null, withForm))
      .toEqual({ ok: false, message: "lint-notes notes_dir: a path setting is a relative path inside the checkout, with no '..'" })
    expect(sheet.stepManagedPlan({ added: { [ADDED_ID]: { api_key: 'x' } }, off: [] }, null, withForm))
      .toEqual({ ok: false, message: "lint-notes api_key: a step overrides only a plugin's non-secret values" })
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
    mocks.loadMe.mockResolvedValue(ok({ tenant: { tenant_id: TENANT }, principal: { email: 'member@example.com', domain: 'example.com', groups: [], is_admin: false } }))
    globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const answer = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: JSON_HEADERS })
      if ((init?.method ?? 'GET') === 'POST' && url.startsWith('/v1/workflows')) {
        posts.push(JSON.parse(String(init?.body ?? 'null')) as Record<string, unknown>)
        return new Response(JSON.stringify({ workflow: { workflow_id: 'wf_m_1', steps: [], created_at: '2026-11-08T12:00:00Z' }, dispatch: null }),
          { status: 201, headers: JSON_HEADERS })
      }
      gets.push(url)
      if (url.startsWith('/v1/plugins')) return answer(view())
      if (url === `/v1/workspaces/${TENANT}/plugin-catalogue`) return answer(managed().catalogue)
      if (url === `/v1/workspaces/${TENANT}/plugin-defaults`) return answer({ tenant_id: TENANT, defaults: [DEFAULT_ID], can_edit: false })
      if (url === `/v1/workspaces/${TENANT}/plugin-config/${ADDED_ID}`) return answer(form(ADDED_ID))
      throw new Error(`unexpected request ${url}`)
    }) as unknown as typeof fetch
  })

  afterEach(() => {
    globalThis.fetch = realFetch
    document.body.innerHTML = ''
    vi.clearAllMocks()
  })

  async function agentStep() {
    const { container } = render(<SubmitWorkflowScreen />)
    await screen.findByRole('button', { name: 'Submit this workflow' }, WAIT)
    expect(gets, 'nothing is read before a step has a runner').toEqual([])
    fireEvent.change(container.querySelector<HTMLSelectElement>('.wfb-step select.sb-runner-select')!, { target: { value: 'claude-code' } })
    fireEvent.change(container.querySelector<HTMLTextAreaElement>('.wfb-step textarea')!, { target: { value: 'review the change' } })
    return container
  }

  async function submitted(): Promise<Record<string, unknown>> {
    const go = screen.getByRole('button', { name: 'Submit this workflow' }) as HTMLButtonElement
    await waitFor(() => expect(go.disabled).toBe(false), WAIT)
    fireEvent.click(go)
    await waitFor(() => expect(posts).toHaveLength(1), WAIT)
    return (posts[0]!.steps as Array<Record<string, unknown>>)[0]!
  }

  it("draws the workspace's defaults as chips on the step card, on for every step", async () => {
    await agentStep()
    const chip = await screen.findByRole('button', { name: 'rules-gate: on for every step; turn it off for this step' }, WAIT)
    expect(chip.getAttribute('aria-pressed')).toBe('true')
    expect(chip.textContent).toBe('rules-gateevery step')
    const sent = await submitted()
    expect('plugins' in sent, 'a default on for this step is merged by swarm-api, never copied').toBe(false)
    expect('plugins_off' in sent).toBe(false)
  })

  it('turns a default off for this step only, and sends it as plugins_off', async () => {
    await agentStep()
    fireEvent.click(await screen.findByRole('button', { name: /^rules-gate: on for every step/ }, WAIT))
    const off = screen.getByRole('button', { name: 'rules-gate: off for this step; turn it back on' })
    expect(off.getAttribute('aria-pressed')).toBe('false')
    expect(off.textContent).toBe('rules-gateoff for this step')
    const sent = await submitted()
    expect(sent.plugins_off).toEqual([DEFAULT_ID])
    expect('plugins' in sent).toBe(false)
  })

  it('adds a managed plugin with a non-secret override, and sends what the server declares', async () => {
    await agentStep()
    fireEvent.click(await screen.findByRole('button', { name: /^Managed plugins on / }, WAIT))
    const dialog = await screen.findByRole('dialog', {}, WAIT)
    const denied = within(dialog).getByRole('button', { name: 'Add trace-log to this step' }) as HTMLButtonElement
    expect(denied.disabled, 'a platform plugin the workspace does not allow').toBe(true)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add lint-notes to this step' }))
    fireEvent.click(within(dialog).getByRole('button', { name: 'lint-notes settings for this step' }))
    const strictness = await within(dialog).findByLabelText('strictness', {}, WAIT) as HTMLInputElement
    expect(strictness.placeholder, "the workspace's value shows through an empty box").toBe('normal')
    expect(within(dialog).queryByLabelText('api_key'), 'a secret is never overridden per step').toBeNull()
    fireEvent.change(strictness, { target: { value: 'high' } })
    fireEvent.click(within(dialog).getByRole('checkbox', { name: 'rules-gate off for this step' }))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Use on this step' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), WAIT)
    expect(screen.getByRole('button', { name: 'Change the managed plugins: lint-notes' }).textContent).toBe('lint-notesstrictness')

    const sent = await submitted()
    expect(sent.plugins).toEqual({ [ADDED_ID]: { overrides: { strictness: 'high' } } })
    expect(sent.plugins_off).toEqual([DEFAULT_ID])
    expect(gets).toContain(`/v1/workspaces/${TENANT}/plugin-config/${ADDED_ID}`)

    // THE SUBMITTED BODY MATCHES THE SERVER'S FIELDS: every key of the step is
    // one WorkflowStepCreate declares, and every plugin entry's key one
    // StepPluginCreate declares.
    const stepFields = serverFields('WorkflowStepCreate')
    expect(stepFields).toEqual(expect.arrayContaining(['plugins', 'plugins_off']))
    for (const key of Object.keys(sent)) expect(stepFields, `step field ${key}`).toContain(key)
    const entryFields = serverFields('StepPluginCreate')
    for (const entry of Object.values(sent.plugins as Record<string, Record<string, unknown>>)) {
      for (const key of Object.keys(entry)) expect(entryFields, `plugin entry field ${key}`).toContain(key)
    }
  // The longest walk here (~12s alone): headroom for a loaded runner, as
  // vitest.config.ts's testTimeout explains.
  }, 60_000)

  it('refuses an override the server would refuse before anything is sent', async () => {
    await agentStep()
    fireEvent.click(await screen.findByRole('button', { name: /^Managed plugins on / }, WAIT))
    const dialog = await screen.findByRole('dialog', {}, WAIT)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add lint-notes to this step' }))
    fireEvent.click(within(dialog).getByRole('button', { name: 'lint-notes settings for this step' }))
    fireEvent.change(await within(dialog).findByLabelText('notes_dir', {}, WAIT), { target: { value: '/etc' } })
    expect(dialog.textContent).toContain("notes_dir: a path setting is a relative path inside the checkout, with no '..'.")
    expect((within(dialog).getByRole('button', { name: 'Use on this step' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('still shows a refused box after its settings are collapsed and reopened', async () => {
    await agentStep()
    fireEvent.click(await screen.findByRole('button', { name: /^Managed plugins on / }, WAIT))
    const dialog = await screen.findByRole('dialog', {}, WAIT)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Add lint-notes to this step' }))
    const settings = within(dialog).getByRole('button', { name: 'lint-notes settings for this step' })
    fireEvent.click(settings)
    fireEvent.change(await within(dialog).findByLabelText('notes_dir', {}, WAIT), { target: { value: '/etc' } })
    fireEvent.click(settings)
    expect(within(dialog).queryByLabelText('notes_dir')).toBeNull()
    fireEvent.click(settings)
    const box = await within(dialog).findByLabelText('notes_dir', {}, WAIT) as HTMLInputElement
    expect(box.value).toBe('/etc')
    expect(dialog.textContent).toContain("notes_dir: a path setting is a relative path inside the checkout, with no '..'.")
    const use = within(dialog).getByRole('button', { name: 'Use on this step' }) as HTMLButtonElement
    expect(use.disabled).toBe(true)
    fireEvent.change(box, { target: { value: 'notes' } })
    expect(use.disabled).toBe(false)
  })
})
