// AUTOMATE › SCHEDULES, THE WRITES (docs/schedules.md §6.2-§6.3, §4.2, lane S7):
// the create form, the edit form and the SD3 merge switch on the Gate tab.
//
// WHAT EACH CASE HOLDS:
//   * `/automate/schedules/new` is the create form, reached from the list;
//   * the cron's words and its next five come from `POST /v1/schedules:preview`
//     -- in the schedule's zone and the viewer's -- and a preset re-asks; a
//     refusal or a parser error is said in place;
//   * a create sends what the person chose and only the gate keys that differ
//     from the type's default, never an image or a command, with one
//     `client_request_id`; merge `auto` is never an option of the form;
//   * an edit sends the revision and the changed fields only, and reads the
//     schedule again after it saves; a stale revision is said, not swallowed;
//   * a catalogue offering no creatable type is the `Absent` state, not an
//     empty form; at 390px the form's fields are one column;
//   * the parameters issue-sweep restates from the gate (`merge`,
//     `plan_approval`) are read-only and never sent, so no control offers
//     merge `auto`;
//   * the merge switch is offered only where the type's floor allows `auto`,
//     sends the revision, asks for the typed name only when the server says a
//     one-person tenant needs it, and says which rule refused it.
//
// The bodies are `routes/schedules.py` and `routes/approvals.py`'s shapes.
// Placeholders only; nothing is token-shaped.

import { afterEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { Routed } from './screenroute'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { addressToPath, pathToAddress } from '../paths'

const WAIT = { timeout: 4000 }
const PHONE: CascadeEnv = { width: 390 }

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null
type Handler = (method: string, url: string, body: unknown) => Reply

const hoursFromNow = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString()

function schedule(over: Json = {}): Json {
  return {
    schedule_id: 'sch_a',
    tenant_id: 'eng',
    name: 'Weekday sweep',
    type: 'issue-sweep',
    scope: { mode: 'repos', repo_ids: ['repo_a'] },
    cron: '0 9 * * 1-5',
    timezone: 'Europe/London',
    params: { max_new_per_firing: 3, plan_approval: 'auto', merge: 'approve' },
    gate: { plan: 'auto', run: 'auto', merge: 'approve', approvers: 'members', approval_ttl_hours: 72 },
    budget: { per_run_usd: 15, per_day_usd: 120, max_concurrent: 8 },
    policy: { overlap: 'skip', catch_up: 'skip', jitter: true, dry_run: false },
    state: 'enabled',
    pause: null,
    owner: 'operator@example.com',
    created_by: 'operator@example.com',
    created_at: hoursFromNow(-48),
    next_run_at: hoursFromNow(3),
    last_firing: null,
    revision: 2,
    words: 'at 09:00, Monday through Friday',
    tier: 'R2',
    spend_today: { day: '2026-10-11', reported_usd: 0, unreported_attempts: 0, coverage: 'complete' },
    pending_approvals: 0,
    ...over,
  }
}

function sweepType(over: Json = {}): Json {
  return {
    name: 'issue-sweep',
    description: 'Plans and runs open issues.',
    executor: 'api',
    params_schema: {
      properties: {
        max_new_per_firing: { type: 'integer', title: 'Max New Per Firing', default: 3, minimum: 1 },
        labels: { type: 'array', title: 'Labels', default: ['swarm'] },
        // The served issue-sweep schema restates the gate in these two
        // (scheduletypes.IssueSweepParams), `auto` among merge's values.
        plan_approval: { type: 'string', title: 'Plan Approval', enum: ['auto', 'required'], default: 'auto' },
        merge: { type: 'string', title: 'Merge', enum: ['off', 'approve', 'auto'], default: 'approve' },
      },
    },
    default_gate: { plan: 'auto', run: 'auto', merge: 'approve', approvers: 'members', approval_ttl_hours: 72 },
    floor_gate: { plan: 'auto', run: 'auto', merge: 'auto', approvers: 'members', approval_ttl_hours: 72 },
    min_interval_minutes: 15,
    budget_caps: null,
    scopes: ['all', 'repos'],
    creatable_scopes: ['repos', 'all'],
    available: true,
    disabled_reason: null,
    ...over,
  }
}

const REPO = {
  repo_id: 'repo_a',
  owner: 'example-org',
  repo: 'example-api',
  default_branch: 'main',
  index: { interval_hours: 24, on_change: 'poll', min_change_interval_minutes: 30, paused: false, next_run_at: hoursFromNow(5) },
}

const PREVIEW = {
  preview: {
    words: 'at 09:00, Monday through Friday',
    next: ['2026-10-12T09:00:00+01:00', '2026-10-13T09:00:00+01:00', '2026-10-14T09:00:00+01:00', '2026-10-15T09:00:00+01:00', '2026-10-16T09:00:00+01:00'],
    min_gap_minutes: 1440,
    refusal: null,
  },
}

/** The reads every case needs, with `extra` answering first. */
function routes(extra: Handler, types: Json[] = [sweepType()]): Handler {
  return (m, url, body) => {
    const hit = extra(m, url, body)
    if (hit !== null) return hit
    const path = url.split('?')[0]!
    if (m === 'GET' && path === '/v1/schedule-types') return { status: 200, body: { types, tenant_id: 'eng' } }
    if (m === 'GET' && path === '/v1/repositories') return { status: 200, body: { repositories: [REPO] } }
    if (m === 'POST' && path === '/v1/schedules:preview') return { status: 200, body: PREVIEW }
    return null
  }
}

async function mount(view: string, handler: Handler) {
  const calls = serve(handler)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SchedulesScreen } = await import('../Schedules')
  const go = vi.fn()
  const utils = render(<Routed Screen={SchedulesScreen} section="automate/schedules" start={view} spy={go} />)
  return { ...utils, calls, go }
}

const sent = (calls: { method: string; url: string; body: unknown }[], method: string, path: string) =>
  calls.filter((c) => c.method === method && c.url.split('?')[0] === path)

/** The control under a field's own label (`.c-lbl`): the label element also holds its hint. */
function field(label: string): HTMLInputElement {
  const lbl = [...document.querySelectorAll('.c-lbl')].find((e) => e.textContent === label)
  const control = lbl?.closest('label')?.querySelector<HTMLInputElement>('input, select')
  if (!control) throw new Error(`no field labelled ${label}`)
  return control
}

/** A segmented control (A3-SEGMENTED): the radio group its label alone names. */
function seg(label: string): HTMLElement {
  return screen.getByRole('radiogroup', { name: label })
}

/** Each value of a segmented control, in order, by the words it shows. */
function words(label: string): string[] {
  return within(seg(label))
    .getAllByRole('radio')
    .map((r) => r.textContent ?? '')
}

/** Choose a segment by its words. */
function pick(label: string, word: string): void {
  fireEvent.click(within(seg(label)).getByRole('radio', { name: word }))
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('the create form', () => {
  it('has its own address, reached from the list', async () => {
    expect(addressToPath('automate/schedules?schedule=new')).toBe('/automate/schedules/new')
    expect(pathToAddress('/automate/schedules/new')?.address).toBe('automate/schedules?schedule=new')
    const { go } = await mount('', routes((m, url) => (m === 'GET' && url.startsWith('/v1/schedules') ? { status: 200, body: { schedules: [], tenant_id: 'eng' } } : null)))
    const link = await screen.findByRole('link', { name: 'New schedule' }, WAIT)
    expect(link.getAttribute('href')).toBe('/automate/schedules/new')
    fireEvent.click(link)
    expect(go).toHaveBeenCalledWith('automate/schedules?schedule=new')
  })

  it('draws a next firing once when the reader’s zone gives the same wall time (QA2 automate2-07)', async () => {
    // MUTATION: draw `.sc-yours` whatever the reader's zone and "09:00 your time" repeats the line.
    const was = process.env.TZ
    process.env.TZ = 'Europe/London'
    try {
      await mount('schedule=new', routes(() => null))
      const preview = await waitFor(() => {
        const el = screen.getByTestId('sc-preview')
        expect(within(el).getAllByRole('listitem')).toHaveLength(5)
        return el
      }, WAIT)
      const first = within(preview).getAllByRole('listitem')[0]!
      expect(visible(first)).toBe('Mon, Oct 12 09:00')
      expect(first.querySelector('.sc-yours')).toBeNull()
    } finally {
      if (was === undefined) delete process.env.TZ
      else process.env.TZ = was
    }
  })

  it('shows the cron in words and the next five, in the schedule zone and the viewer’s, from the preview route', async () => {
    const was = process.env.TZ
    process.env.TZ = 'UTC'
    onTestFinished(() => {
      if (was === undefined) delete process.env.TZ
      else process.env.TZ = was
    })
    const { calls } = await mount('schedule=new', routes(() => null))
    await screen.findByRole('heading', { name: 'New schedule', level: 1 }, WAIT)
    const preview = await waitFor(() => {
      const el = screen.getByTestId('sc-preview')
      expect(visible(el)).toContain('Monday through Friday')
      return el
    }, WAIT)
    const next = within(preview).getAllByRole('listitem')
    expect(next).toHaveLength(5)
    // One format, as the sheet's chips (QA2 automate2-07), and the reader's
    // time beside it when it is a different wall time (this runner reads UTC
    // or is pinned to it below).
    expect(visible(next[0]!)).toContain('Mon, Oct 12 09:00')
    expect(visible(next[0]!)).not.toContain('2026-10-12')
    expect(visible(next[0]!)).toContain('Mon, Oct 12 08:00 your time')
    // MUTATION: parse the cron in the browser and the route is never asked.
    expect(sent(calls, 'POST', '/v1/schedules:preview').at(-1)?.body).toMatchObject({ cron: '0 9 * * 1-5', type: 'issue-sweep' })
    fireEvent.click(screen.getByRole('button', { name: 'Every hour' }))
    await waitFor(() => expect(sent(calls, 'POST', '/v1/schedules:preview').at(-1)?.body).toMatchObject({ cron: '0 * * * *' }), WAIT)
  })

  it('says a refusal and a parser error in place', async () => {
    let answer: Reply = { status: 200, body: { preview: { ...PREVIEW.preview, refusal: { code: 'interval_too_short', message: 'issue-sweep fires at most every 15 minutes' } } } }
    await mount('schedule=new', routes((m, url) => (m === 'POST' && url === '/v1/schedules:preview' ? answer : null)))
    await screen.findByText(/This type would refuse it: issue-sweep fires at most every 15 minutes/, undefined, WAIT)
    answer = { status: 422, body: { code: 'invalid_cron', message: 'minute 61 is out of range' } }
    fireEvent.change(field('Cron expression'), { target: { value: '61 * * * *' } })
    await screen.findByText(/Not a schedule: minute 61 is out of range/, undefined, WAIT)
  })

  it('never offers automatic merging', async () => {
    await mount('schedule=new', routes(() => null))
    await screen.findByRole('heading', { name: 'New schedule', level: 1 }, WAIT)
    await waitFor(() => seg('Merge'), WAIT)
    // MUTATION: add `auto` to the form's merge options.
    expect(words('Merge')).toEqual(['off', 'approve'])
    // No control anywhere in the form offers `auto` for merging: the
    // parameter that restates the gate is drawn read-only, not as its enum.
    const form = screen.getByTestId('sc-form')
    const mergeLabels = [...form.querySelectorAll('.c-lbl')].filter((e) => /merge/i.test(e.textContent ?? ''))
    expect(mergeLabels.length).toBe(2)
    for (const lbl of mergeLabels) {
      const field = lbl.closest('.c-field')!
      const control = field.querySelector('input, select, [role="radiogroup"]')!
      // MUTATION: draw the restating parameters through `editable()` and this is a select offering `auto`.
      if (control.tagName === 'SELECT') expect([...(control as HTMLSelectElement).options].map((o) => o.value)).not.toContain('auto')
      // MUTATION: add `auto` to the merge segments and one reads "auto".
      else if (control.getAttribute('role') === 'radiogroup') expect(within(control as HTMLElement).getAllByRole('radio').map((r) => r.textContent)).not.toContain('auto')
      else expect((control as HTMLInputElement).readOnly).toBe(true)
    }
    expect((screen.getByTestId('sc-param-merge') as HTMLInputElement).value).toBe('approve')
    expect((screen.getByTestId('sc-param-plan_approval') as HTMLInputElement).readOnly).toBe(true)
  })

  it('shows the restated parameters as the gate makes them and never sends them', async () => {
    const { calls } = await mount(
      'schedule=new',
      routes((m, url) => (m === 'POST' && url === '/v1/schedules' ? { status: 201, body: { schedule: schedule({ schedule_id: 'sch_new' }), created: true } } : null)),
    )
    await waitFor(() => expect(field('Name')).toBeTruthy(), WAIT)
    fireEvent.change(field('Name'), { target: { value: 'Weekday sweep' } })
    pick('Plan', 'approve')
    pick('Merge', 'off')
    expect((screen.getByTestId('sc-param-plan_approval') as HTMLInputElement).value).toBe('required')
    expect((screen.getByTestId('sc-param-merge') as HTMLInputElement).value).toBe('off')
    fireEvent.click(screen.getByRole('button', { name: 'Create schedule' }))
    await waitFor(() => expect(sent(calls, 'POST', '/v1/schedules')).toHaveLength(1), WAIT)
    const body = sent(calls, 'POST', '/v1/schedules')[0]!.body as Json
    expect(body.gate).toEqual({ plan: 'approve', merge: 'off' })
    // MUTATION: send the restated keys and the server refuses with params_disagree_with_gate.
    expect(body).not.toHaveProperty('params')
  })

  it('creates with the chosen fields and the gate keys that differ from the default, then opens it', async () => {
    const { calls, go } = await mount(
      'schedule=new',
      routes((m, url) => (m === 'POST' && url === '/v1/schedules' ? { status: 201, body: { schedule: schedule({ schedule_id: 'sch_new' }), created: true } } : null)),
    )
    await screen.findByRole('heading', { name: 'New schedule', level: 1 }, WAIT)
    await waitFor(() => expect(field('Name')).toBeTruthy(), WAIT)
    const create = screen.getByRole('button', { name: 'Create schedule' })
    expect((create as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(field('Name'), { target: { value: 'Weekday sweep' } })
    fireEvent.change(field('Timezone'), { target: { value: 'Europe/London' } })
    pick('Plan', 'approve')
    fireEvent.click(screen.getByRole('button', { name: 'Create schedule' }))
    await waitFor(() => expect(sent(calls, 'POST', '/v1/schedules')).toHaveLength(1), WAIT)
    const body = sent(calls, 'POST', '/v1/schedules')[0]!.body as Json
    expect(body).toMatchObject({
      name: 'Weekday sweep',
      type: 'issue-sweep',
      // The one registration is preselected.
      scope: { mode: 'repos', repo_ids: ['repo_a'] },
      cron: '0 9 * * 1-5',
      timezone: 'Europe/London',
      state: 'enabled',
    })
    // MUTATION: send the whole default gate and this is five keys, not one.
    expect(body.gate).toEqual({ plan: 'approve' })
    expect(String(body.client_request_id)).toMatch(/^ui-/)
    // Invariant 10: a type by name, and nothing a backend would run.
    for (const k of ['image', 'command', 'resources', 'params', 'budget']) expect(body).not.toHaveProperty(k)
    await waitFor(() => expect(go).toHaveBeenCalledWith('automate/schedules?schedule=sch_new'), WAIT)
  })

  it('is the Absent state when the catalogue offers no type this account may create', async () => {
    await mount('schedule=new', routes(() => null, []))
    await screen.findByRole('heading', { name: 'New schedule', level: 1 }, WAIT)
    // MUTATION: draw the form over an empty catalogue and there is a Type select with nothing in it.
    await screen.findByText('No type you may create', undefined, WAIT)
    expect(screen.queryByTestId('sc-form')).toBeNull()
  })

  it('puts its fields in one column on a phone', async () => {
    // MUTATION: drop `.sc-facts` from schedules.css's 560px block.
    await mount('schedule=new', routes(() => null))
    await screen.findByRole('heading', { name: 'New schedule', level: 1 }, WAIT)
    const form = await screen.findByTestId('sc-form', undefined, WAIT)
    const grids = [...form.querySelectorAll<HTMLElement>('.sc-facts')]
    expect(grids.length).toBeGreaterThan(0)
    for (const g of grids) expect(painted(g, 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
    expect(painted(form.querySelector<HTMLElement>('.sc-presets')!, 'flex-wrap', PHONE)).toBe('wrap')
  })

  it('says the server’s refusal and creates nothing else', async () => {
    await mount(
      'schedule=new',
      routes((m, url) => (m === 'POST' && url === '/v1/schedules' ? { status: 422, body: { code: 'name_taken', message: 'a schedule named Weekday sweep exists' } } : null)),
    )
    await waitFor(() => expect(field('Name')).toBeTruthy(), WAIT)
    fireEvent.change(field('Name'), { target: { value: 'Weekday sweep' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create schedule' }))
    await screen.findByText(/Not saved: a schedule named Weekday sweep exists/, undefined, WAIT)
  })
})

describe('the edit form', () => {
  function detail(extra: Handler, current: () => Json = () => schedule()): Handler {
    return routes((m, url, body) => {
      const hit = extra(m, url, body)
      if (hit !== null) return hit
      if (m === 'GET' && url.split('?')[0] === '/v1/schedules/sch_a') return { status: 200, body: { schedule: current(), firings: [] } }
      return null
    })
  }

  it('sends the revision and only what changed, then reads the schedule again', async () => {
    const { calls } = await mount(
      'schedule=sch_a',
      detail((m, url) => (m === 'PATCH' && url === '/v1/schedules/sch_a' ? { status: 200, body: { schedule: schedule({ revision: 3 }) } } : null)),
    )
    await screen.findByRole('heading', { name: 'Weekday sweep', level: 1 }, WAIT)
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    await waitFor(() => expect(field('Cron expression').value).toBe('0 9 * * 1-5'), WAIT)
    expect(field('Type').readOnly).toBe(true)
    fireEvent.click(screen.getByRole('button', { name: 'Weekly on Monday' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(sent(calls, 'PATCH', '/v1/schedules/sch_a')).toHaveLength(1), WAIT)
    // MUTATION: send the whole draft and the stored gate, budget and params ride along.
    expect(sent(calls, 'PATCH', '/v1/schedules/sch_a')[0]!.body).toEqual({ revision: 2, cron: '0 9 * * 1' })
    // Frame A2 reads the schedule once for the sheet and once for the editor's
    // page, so the count before the save is not fixed: what holds is a read
    // AFTER the PATCH, back on the sheet.
    // MUTATION: drop the re-read on save and no GET follows the PATCH.
    const patched = calls.findIndex((c) => c.method === 'PATCH' && c.url === '/v1/schedules/sch_a')
    await waitFor(() => expect(calls.slice(patched + 1).some((c) => c.method === 'GET' && c.url.split('?')[0] === '/v1/schedules/sch_a')).toBe(true), WAIT)
    await screen.findByRole('button', { name: 'Edit' }, WAIT)
  })

  it('changes the gate without restating it in the parameters', async () => {
    const { calls } = await mount(
      'schedule=sch_a',
      detail((m, url) => (m === 'PATCH' && url === '/v1/schedules/sch_a' ? { status: 200, body: { schedule: schedule({ revision: 3 }) } } : null)),
    )
    await screen.findByRole('heading', { name: 'Weekday sweep', level: 1 }, WAIT)
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    await waitFor(() => expect(seg('Plan')).toBeTruthy(), WAIT)
    pick('Plan', 'approve')
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(sent(calls, 'PATCH', '/v1/schedules/sch_a')).toHaveLength(1), WAIT)
    expect(sent(calls, 'PATCH', '/v1/schedules/sch_a')[0]!.body).toEqual({ revision: 2, gate: { plan: 'approve' } })
  })

  it('says a stale revision rather than overwriting it', async () => {
    await mount(
      'schedule=sch_a',
      detail((m, url) =>
        m === 'PATCH' && url === '/v1/schedules/sch_a' ? { status: 409, body: { code: 'schedule_changed', message: 'the schedule was changed since it was read' } } : null,
      ),
    )
    await screen.findByRole('heading', { name: 'Weekday sweep', level: 1 }, WAIT)
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    await waitFor(() => expect(field('Name')).toBeTruthy(), WAIT)
    fireEvent.change(field('Name'), { target: { value: 'Weekday sweep 2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))
    await screen.findByText(/changed this schedule since you opened it/, undefined, WAIT)
  })
})

describe('the SD3 merge switch', () => {
  async function gateTab(extra: Handler, current: Json = schedule(), types: Json[] = [sweepType()]) {
    const handler = routes((m, url, body) => {
      const hit = extra(m, url, body)
      if (hit !== null) return hit
      if (m === 'GET' && url.split('?')[0] === '/v1/schedules/sch_a') return { status: 200, body: { schedule: current, firings: [] } }
      return null
    }, types)
    const out = await mount('schedule=sch_a', handler)
    await screen.findByRole('heading', { name: 'Weekday sweep', level: 1 }, WAIT)
    // Frame A2 draws the gate as a section of the sheet, not a tab to open.
    await waitFor(() => expect(document.querySelector('aside.as-sheet [data-sec="gate"]')).not.toBeNull(), WAIT)
    return out
  }

  const MODE = '/v1/schedules/sch_a:merge-mode'

  /** Step one, then step two with the name typed, then Switch now. */
  async function switchTyped(name = 'Weekday sweep') {
    fireEvent.click(await screen.findByRole('button', { name: 'Merge automatically…' }, WAIT))
    const dialog = await screen.findByRole('dialog', undefined, WAIT)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Switch to automatic merging' }))
    const box = await within(dialog).findByRole('textbox', undefined, WAIT)
    fireEvent.change(box, { target: { value: name } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Switch now' }))
    return dialog
  }

  it('switches to automatic in two steps with the revision, and the page reads the schedule again', async () => {
    const { calls } = await gateTab((m, url) => (m === 'POST' && url === MODE ? { status: 200, body: { schedule: schedule({ revision: 3, gate: { merge: 'auto' } }) } } : null))
    fireEvent.click(await screen.findByRole('button', { name: 'Merge automatically…' }, WAIT))
    const dialog = await screen.findByRole('dialog', undefined, WAIT)
    // §4.4, said where the decision is made, behind Details (owner review
    // 2026-10-11): workflow files are refused, and outside a platform
    // repository the holds are a second member's.
    // MUTATION: say they "wait for the owner" and this fails.
    const rules = dialog.querySelector('details')!
    expect(visible(rules)).toContain('hard stops')
    expect(visible(rules)).toMatch(/a plan naming workflow files is refused/)
    expect(visible(rules)).toMatch(/held for a second member to approve \(for the owner, in a platform repository\)/)
    expect(visible(rules)).toMatch(/frozen contract is held for the owner/)
    expect(visible(dialog)).not.toMatch(/wait for the owner/)
    // AUT-4: the first step sends nothing; the second asks for the name typed.
    // MUTATION: send from the first step and a POST lands with no typed step.
    fireEvent.click(within(dialog).getByRole('button', { name: 'Switch to automatic merging' }))
    expect(sent(calls, 'POST', MODE)).toHaveLength(0)
    const box = await within(dialog).findByRole('textbox', undefined, WAIT)
    const now = () => within(dialog).getByRole('button', { name: 'Switch now' }) as HTMLButtonElement
    expect(now().disabled).toBe(true)
    fireEvent.change(box, { target: { value: 'Weekday swee' } })
    expect(now().disabled).toBe(true)
    fireEvent.change(box, { target: { value: 'Weekday sweep' } })
    expect(now().disabled).toBe(false)
    fireEvent.click(now())
    await waitFor(() => expect(sent(calls, 'POST', MODE)).toHaveLength(1), WAIT)
    expect(sent(calls, 'POST', MODE)[0]!.body).toEqual({ mode: 'auto', revision: 2, confirm: 'Weekday sweep' })
    await waitFor(() => expect(sent(calls, 'GET', '/v1/schedules/sch_a').length).toBe(2), WAIT)
    // The re-read lands on the sheet's Gate section it came from.
    await screen.findByTestId('sc-gate', undefined, WAIT)
  })

  it('disables Switch, with the reason above it, for the member who last changed the gate', async () => {
    const me = { tenant: { tenant_id: 'eng', kind: 'group' }, principal: { email: 'operator@example.com', domain: 'example.com', groups: [], is_admin: true } }
    const audit = [{ schedule_id: 'sch_a', action: 'create', by: 'operator@example.com', at: hoursFromNow(-1), detail: {} }]
    const { calls } = await gateTab((m, url) => {
      if (m === 'GET' && url === '/v1/tenants/me') return { status: 200, body: me }
      if (m === 'GET' && url.split('?')[0] === '/v1/schedules/sch_a/audit') return { status: 200, body: { audit } }
      return null
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Merge automatically…' }, WAIT))
    const dialog = await screen.findByRole('dialog', undefined, WAIT)
    // Blocked, the primary reads "Switch to automatic", with no ellipsis: nothing follows a click.
    const sw = () => within(dialog).getByRole('button', { name: 'Switch to automatic' }) as HTMLButtonElement
    // MUTATION: drop `useSwitchBlock` and the button is live for the person the dialog says cannot switch.
    await waitFor(() => expect(sw().disabled).toBe(true), WAIT)
    expect(sw().getAttribute('aria-disabled')).toBe('true')
    expect(visible(dialog)).toContain('You last changed this schedule’s gate, scope or spec')
    fireEvent.click(sw())
    expect(within(dialog).queryByRole('textbox')).toBeNull()
    expect(sent(calls, 'POST', MODE)).toHaveLength(0)
  })

  it('leaves Switch open to another member, and to a one-person tenant, who types the name', async () => {
    const audit = [
      { schedule_id: 'sch_a', action: 'edit', by: 'operator@example.com', at: hoursFromNow(-1), detail: { changed: { name: 'x' } } },
      { schedule_id: 'sch_a', action: 'edit', by: 'admin:someone@example.com', at: hoursFromNow(-2), detail: { changed: { gate: {} } } },
    ]
    for (const kind of ['group', 'user']) {
      const me = { tenant: { tenant_id: 'eng', kind }, principal: { email: kind === 'group' ? 'operator@example.com' : 'someone@example.com', domain: 'example.com', groups: [], is_admin: false } }
      const view = await gateTab((m, url) => {
        if (m === 'GET' && url === '/v1/tenants/me') return { status: 200, body: me }
        if (m === 'GET' && url.split('?')[0] === '/v1/schedules/sch_a/audit') return { status: 200, body: { audit } }
        return null
      })
      fireEvent.click(await screen.findByRole('button', { name: 'Merge automatically…' }, WAIT))
      const dialog = await screen.findByRole('dialog', undefined, WAIT)
      // Let the reads land, then the button is still live: a rename is not a gate edit,
      // and a personal tenant's one member types the name instead.
      await waitFor(() => expect(view.calls.some((c) => c.url.startsWith('/v1/schedules/sch_a/audit'))).toBe(true), WAIT)
      await new Promise((r) => setTimeout(r, 20))
      expect((within(dialog).getByRole('button', { name: 'Switch to automatic merging' }) as HTMLButtonElement).disabled).toBe(false)
      view.unmount()
    }
  })

  it('says which rule refused it', async () => {
    await gateTab((m, url) =>
      m === 'POST' && url === MODE ? { status: 403, body: { code: 'second_person_required', message: 'another member must' } } : null,
    )
    const dialog = await switchTyped()
    await within(dialog).findByText(/another member must make this switch/, undefined, WAIT)
  })

  it('returns focus to its trigger when the dialog closes, wherever focus was when it opened (AUT-13)', async () => {
    await gateTab(() => null)
    const trigger = await screen.findByRole('button', { name: 'Merge automatically…' }, WAIT)
    ;(document.activeElement as HTMLElement | null)?.blur()
    fireEvent.click(trigger)
    await screen.findByRole('dialog', undefined, WAIT)
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), WAIT)
    // MUTATION: drop the focus in `close` and this is <body>.
    await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Merge automatically…' })), WAIT)
  })

  it('is not offered where the type never merges unattended', async () => {
    // MUTATION: offer the switch whatever the type's floor.
    await gateTab(() => null, schedule(), [sweepType({ floor_gate: { merge: 'approve' } })])
    const sw = await screen.findByTestId('sc-merge-switch', undefined, WAIT)
    await waitFor(() => expect(visible(screen.getByTestId('sc-merge-switch'))).toContain('never merges unattended'), WAIT)
    expect(within(sw.parentElement!).queryByRole('button', { name: 'Merge automatically…' })).toBeNull()
  })

  it('switches back to approving each merge without a dialog', async () => {
    const { calls } = await gateTab(
      (m, url) => (m === 'POST' && url === MODE ? { status: 200, body: { schedule: schedule({ revision: 3 }) } } : null),
      schedule({ gate: { plan: 'auto', run: 'auto', merge: 'auto', approvers: 'members' } }),
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Approve each merge again' }, WAIT))
    await waitFor(() => expect(sent(calls, 'POST', MODE)).toHaveLength(1), WAIT)
    expect(sent(calls, 'POST', MODE)[0]!.body).toEqual({ mode: 'approve', revision: 2 })
  })
})

// EDIT A, THE OWNER'S PICK (2026-10-11, docs/schedules.md §6.2, mockup #edit-a):
//   * the type is template tiles, not a <select>; an unavailable type is a
//     disabled tile carrying the catalogue's reason;
//   * a 360px side column on a desktop holds the cron's words, the next five
//     and the template card (tier, gate, budget defaults, shortest interval),
//     and folds under the fields below 1024px;
//   * the head says "workspace eng · N of 25 used", a served limit winning;
//   * Start is Enabled / Dry run first / Paused;
//   * a budget field states the type's default instead of "Blank: the type's
//     default".
describe('edit A', () => {
  const CAPS = { per_run_usd: 15, per_day_usd: 120, max_concurrent: 8 }
  const unbuilt = sweepType({
    name: 'epic-triage',
    description: 'Triages epics.',
    available: false,
    disabled_reason: 'not built yet: lane S10',
  })
  const observer = sweepType({
    name: 'observer',
    description: 'A read-only report on the last day of work.',
    params_schema: { properties: {} },
    default_gate: { run: 'auto' },
    min_interval_minutes: 1440,
    budget_caps: { per_run_usd: 5, per_day_usd: 10, max_concurrent: 1 },
    tier: 'R0',
  })
  const catalogue = [sweepType({ budget_caps: CAPS }), observer, unbuilt]
  const list = (n: number, extra: Json = {}): Reply => ({
    status: 200,
    body: { schedules: Array.from({ length: n }, (_, i) => schedule({ schedule_id: `sch_${i}`, name: `S ${i}` })), tenant_id: 'eng', ...extra },
  })
  const createRoutes = (extra: Handler = () => null, listReply: Reply = list(6)) =>
    routes((m, url, body) => {
      const hit = extra(m, url, body)
      if (hit !== null) return hit
      if (m === 'GET' && url.split('?')[0] === '/v1/schedules') return listReply
      if (m === 'POST' && url === '/v1/schedules') return { status: 201, body: { schedule: schedule({ schedule_id: 'sch_new' }), created: true } }
      return null
    }, catalogue)

  async function openCreate(handler: Handler) {
    const out = await mount('schedule=new', handler)
    await screen.findByRole('heading', { name: 'New schedule', level: 1 }, WAIT)
    await screen.findByTestId('sc-form', undefined, WAIT)
    return out
  }

  it('picks the type from tiles, an unavailable type greyed with its reason', async () => {
    const { calls } = await openCreate(createRoutes())
    const tiles = screen.getByRole('radiogroup', { name: 'Type' })
    // MUTATION: put the type <select> back and there is no radiogroup, and a select labelled Type.
    expect(screen.getByTestId('sc-form').querySelector('select option[value="issue-sweep"]')).toBeNull()
    const radios = within(tiles).getAllByRole('radio')
    expect(radios.map((r) => r.querySelector('b')?.textContent)).toEqual(['issue-sweep', 'observer', 'epic-triage'])
    expect(radios[0]!.getAttribute('aria-checked')).toBe('true')
    const off = radios[2] as HTMLButtonElement
    expect(off.disabled).toBe(true)
    expect(off.className).toContain('is-off')
    // The reason is drawn on the tile, not only in a tooltip.
    expect(visible(off)).toContain('Not available: not built yet: lane S10')
    fireEvent.click(radios[1]!)
    expect(radios[1]!.getAttribute('aria-checked')).toBe('true')
    await waitFor(() => expect(sent(calls, 'POST', '/v1/schedules:preview').at(-1)?.body).toMatchObject({ type: 'observer' }), WAIT)
    fireEvent.change(field('Name'), { target: { value: 'Daily report' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create schedule' }))
    await waitFor(() => expect(sent(calls, 'POST', '/v1/schedules')).toHaveLength(1), WAIT)
    expect((sent(calls, 'POST', '/v1/schedules')[0]!.body as Json).type).toBe('observer')
  })

  it('puts the words, the next five and the template card in a 360px side column, under the fields below 1024px', async () => {
    await openCreate(createRoutes())
    const form = screen.getByTestId('sc-form')
    const cols = form.querySelector<HTMLElement>('.sc-edit-cols')!
    const side = cols.querySelector<HTMLElement>('.sc-edit-side')!
    expect(side.contains(screen.getByTestId('sc-preview'))).toBe(true)
    expect(side.contains(screen.getByTestId('sc-tpl-card'))).toBe(true)
    // MUTATION: drop the column rule from the edit-A block of schedules.css.
    expect(painted(cols, 'grid-template-columns', { width: 1440 })).toBe('minmax(0, 1fr) 360px')
    // MUTATION: drop the 1023px block and a phone is left a 360px column.
    expect(painted(cols, 'grid-template-columns', { width: 1000 })).toBe('minmax(0, 1fr)')
    expect(painted(cols, 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
  })

  it('shows the template card: tier, gate, budget defaults and shortest interval', async () => {
    await openCreate(createRoutes())
    const card = () => screen.getByTestId('sc-tpl-card')
    const fact = (name: string) =>
      [...card().querySelectorAll('dt')].find((dt) => dt.textContent === name)?.nextElementSibling?.textContent ?? null
    expect(visible(card())).toContain('issue-sweep')
    expect(fact('Gate')).toBe('run auto · plan auto · merge approve · any member approves')
    expect(fact('Budget')).toBe('$15.00 / run · $120.00 / day · 8 live')
    expect(fact('Shortest interval')).toBe('15 min')
    // The catalogue serves no tier for issue-sweep: said, not invented.
    expect(fact('Tier')).toBe('set from the gate when the schedule is saved')
    fireEvent.click(within(screen.getByRole('radiogroup', { name: 'Type' })).getAllByRole('radio')[1]!)
    // MUTATION: read the card off the first type instead of the picked one.
    await waitFor(() => expect(fact('Tier')).toBe('R0'), WAIT)
    expect(fact('Shortest interval')).toBe('1 day')
    expect(fact('Budget')).toBe('$5.00 / run · $10.00 / day · 1 live')
  })

  it('says how many of the workspace’s 25 schedules are used, a served limit winning', async () => {
    await openCreate(createRoutes())
    // MUTATION: drop TenantRoom from the create page and this is gone.
    await waitFor(() => expect(visible(screen.getByTestId('sc-room'))).toBe('workspace eng · 6 of 25 used'), WAIT)
  })

  it('counts against a raised limit when the list serves one, and says a full workspace will be refused', async () => {
    await openCreate(createRoutes(() => null, list(30, { limit: 30 })))
    await waitFor(() => expect(visible(screen.getByTestId('sc-room'))).toContain('workspace eng · 30 of 30 used'), WAIT)
    // VQ-056: the full line says workspace too, never tenant.
    expect(visible(screen.getByTestId('sc-room'))).toContain('this workspace has every schedule it may have')
    expect(visible(screen.getByTestId('sc-room'))).not.toMatch(/tenant/i)
    expect(screen.getByTestId('sc-room').getAttribute('data-full')).toBe('true')
  })

  it('starts Enabled, Dry run first or Paused, Dry run first chosen', async () => {
    const { calls } = await openCreate(createRoutes())
    const start = screen.getByRole('radiogroup', { name: 'Start' })
    const opts = within(start).getAllByRole('radio')
    expect(opts.map((o) => o.textContent)).toEqual(['Enabled', 'Dry run first', 'Paused'])
    // AUT-2: a new schedule is a dry run unless the person picks otherwise.
    // MUTATION: start the draft at 'enabled' and Enabled is the one checked.
    expect(opts[1]!.getAttribute('aria-checked')).toBe('true')
    expect(opts[0]!.getAttribute('aria-checked')).toBe('false')
    // MUTATION: keep the old "Start paused" and "Dry run" checkboxes and these are still here.
    expect(screen.queryByRole('checkbox', { name: /Start paused/ })).toBeNull()
    expect(screen.queryByRole('checkbox', { name: /Dry run/ })).toBeNull()
    fireEvent.change(field('Name'), { target: { value: 'Weekday sweep' } })

    fireEvent.click(opts[1]!)
    fireEvent.click(screen.getByRole('button', { name: 'Create schedule' }))
    await waitFor(() => expect(sent(calls, 'POST', '/v1/schedules')).toHaveLength(1), WAIT)
    const dry = sent(calls, 'POST', '/v1/schedules')[0]!.body as Json
    // MUTATION: map "Dry run first" to state alone and the policy is missing.
    expect(dry.state).toBe('enabled')
    expect(dry.policy).toEqual({ dry_run: true })
  })

  it('creates a paused schedule with no dry run when Paused is chosen', async () => {
    const { calls } = await openCreate(createRoutes())
    fireEvent.click(within(screen.getByRole('radiogroup', { name: 'Start' })).getByRole('radio', { name: 'Paused' }))
    fireEvent.change(field('Name'), { target: { value: 'Weekday sweep' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create schedule' }))
    await waitFor(() => expect(sent(calls, 'POST', '/v1/schedules')).toHaveLength(1), WAIT)
    const body = sent(calls, 'POST', '/v1/schedules')[0]!.body as Json
    expect(body.state).toBe('paused')
    expect(body).not.toHaveProperty('policy')
  })

  it('states the type’s budget defaults in the budget fields', async () => {
    const { calls } = await openCreate(createRoutes())
    const hintOf = (label: string) => field(label).closest('label')!.querySelector('.c-hint')!.textContent
    // MUTATION: put back "Blank: the type's default." and none of these name a value.
    expect(hintOf('Per run (USD)')).toBe('Default $15.00, the most this type allows.')
    expect(hintOf('Per day (USD)')).toBe('Default $120.00, the most this type allows.')
    expect(hintOf('Live runs at most')).toBe('Default 8, the most this type allows.')
    expect(field('Per run (USD)').placeholder).toBe('15')
    expect(screen.getByTestId('sc-form').textContent).not.toContain('Blank')
    // A blank field still sends nothing: the server starts the schedule at its caps.
    fireEvent.change(field('Name'), { target: { value: 'Weekday sweep' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create schedule' }))
    await waitFor(() => expect(sent(calls, 'POST', '/v1/schedules')).toHaveLength(1), WAIT)
    expect(sent(calls, 'POST', '/v1/schedules')[0]!.body).not.toHaveProperty('budget')
  })

  it('still never offers automatic merging among the tiles’ types', async () => {
    await openCreate(createRoutes())
    // MUTATION: add `auto` to the form's merge options.
    expect(words('Merge')).toEqual(['off', 'approve'])
  })

  it('in an edit, keeps the type fixed, shows the schedule’s own tier and offers no Start', async () => {
    await mount(
      'schedule=sch_a',
      routes((m, url) => (m === 'GET' && url.split('?')[0] === '/v1/schedules/sch_a' ? { status: 200, body: { schedule: schedule(), firings: [] } } : null), catalogue),
    )
    await screen.findByRole('heading', { name: 'Weekday sweep', level: 1 }, WAIT)
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    await screen.findByTestId('sc-form', undefined, WAIT)
    expect(screen.queryByRole('radiogroup', { name: 'Type' })).toBeNull()
    expect(screen.queryByRole('radiogroup', { name: 'Start' })).toBeNull()
    expect(field('Type').readOnly).toBe(true)
    // MUTATION: ignore the schedule's tier and the card says "set when saved".
    const tierDt = [...screen.getByTestId('sc-tpl-card').querySelectorAll('dt')].find((dt) => dt.textContent === 'Tier')
    expect(tierDt?.nextElementSibling?.textContent).toBe('R2')
    expect(screen.getByTestId('sc-form').querySelector('.sc-edit-side')!.contains(screen.getByTestId('sc-preview'))).toBe(true)
  })
})

// LIVE QA, AUTOMATE (2026-10-11): what the create and edit forms did on the
// live console that they must not do again.
describe('live QA: automate, the form', () => {
  const customPrompt = sweepType({
    name: 'custom-prompt',
    description: 'Runs a saved, signed workflow spec.',
    params_schema: {
      properties: { spec: { $ref: '#/$defs/WorkflowCreate' }, max_steps: { type: 'integer', title: 'Max Steps', default: 5, minimum: 1 } },
      required: ['spec'],
    },
    default_gate: { run: 'auto' },
  })
  const listed = sweepType({
    params_schema: {
      properties: {
        labels_include: { type: 'array', title: 'Labels Include', items: { type: 'string' }, default: [] },
        labels_exclude: { type: 'array', title: 'Labels Exclude', items: { type: 'string' }, default: ['security', 'needs-owner', 'wontfix', 'question'] },
        branch: { anyOf: [{ type: 'string' }, { type: 'null' }], title: 'Branch', default: null },
      },
    },
  })
  const created = (m: string, url: string): Reply =>
    m === 'POST' && url === '/v1/schedules' ? { status: 201, body: { schedule: schedule({ schedule_id: 'sch_new' }), created: true } } : null

  it('never creates on Enter: the form only writes on a click of Create (AUT-2)', async () => {
    const { calls } = await mount('schedule=new', routes(created))
    await waitFor(() => expect(field('Name')).toBeTruthy(), WAIT)
    fireEvent.change(field('Name'), { target: { value: 'qa-enter-probe' } })
    // Enter in a text field is the browser's implicit submission: a submit event on the form.
    // MUTATION: make `onSubmit` call `submit()` and this POSTs.
    fireEvent.submit(screen.getByTestId('sc-form'))
    await new Promise((r) => setTimeout(r, 30))
    expect(sent(calls, 'POST', '/v1/schedules')).toHaveLength(0)
    // MUTATION: give Create `type="submit"` and it is the form's default button again.
    expect(screen.getByRole('button', { name: 'Create schedule' }).getAttribute('type')).toBe('button')
  })

  it('defaults to chosen repositories wherever the type offers them, never every repository (AUT-2)', async () => {
    const { calls } = await mount('schedule=new', routes(created, [sweepType({ creatable_scopes: ['all', 'repos'] })]))
    await waitFor(() => expect(seg('Scope')).toBeTruthy(), WAIT)
    // MUTATION: take the first creatable scope and this is "all".
    expect(within(seg('Scope')).getByRole('radio', { checked: true }).textContent).toBe('Chosen repositories')
    fireEvent.change(field('Name'), { target: { value: 'Weekday sweep' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create schedule' }))
    await waitFor(() => expect(sent(calls, 'POST', '/v1/schedules')).toHaveLength(1), WAIT)
    expect(sent(calls, 'POST', '/v1/schedules')[0]!.body).toMatchObject({ scope: { mode: 'repos', repo_ids: ['repo_a'] }, state: 'enabled', policy: { dry_run: true } })
  })

  it('does not create a type whose required parameter the form cannot write, and says why (AUT-5)', async () => {
    const { calls } = await mount('schedule=new', routes(created, [customPrompt]))
    await waitFor(() => expect(field('Name')).toBeTruthy(), WAIT)
    fireEvent.change(field('Name'), { target: { value: 'Nightly prompt' } })
    const create = screen.getByRole('button', { name: 'Create schedule' }) as HTMLButtonElement
    // MUTATION: drop `paramsWhy` and Create is live over a body the server refuses.
    expect(create.disabled).toBe(true)
    expect(visible(screen.getByTestId('sc-form'))).toContain('custom-prompt needs spec, which this form cannot write')
    expect(visible(screen.getByTestId('sc-form'))).toContain('params.spec')
    fireEvent.click(create)
    expect(sent(calls, 'POST', '/v1/schedules')).toHaveLength(0)
  })

  it('sets labels_include, shows the default labels_exclude, and sends only what changed (AUT-12)', async () => {
    const { calls } = await mount('schedule=new', routes(created, [listed]))
    await waitFor(() => expect(field('Labels Include')).toBeTruthy(), WAIT)
    // MUTATION: draw lists read-only again and these are not inputs you can type in.
    expect(field('Labels Include').readOnly).toBe(false)
    expect(field('Labels Exclude').value).toBe('security, needs-owner, wontfix, question')
    expect(visible(field('Labels Exclude').closest('label'))).toContain('Default: security, needs-owner, wontfix, question.')
    fireEvent.change(field('Labels Include'), { target: { value: 'swarm, good first issue, ' } })
    expect(field('Labels Include').value).toBe('swarm, good first issue, ')
    fireEvent.change(field('Name'), { target: { value: 'Weekday sweep' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create schedule' }))
    await waitFor(() => expect(sent(calls, 'POST', '/v1/schedules')).toHaveLength(1), WAIT)
    const body = sent(calls, 'POST', '/v1/schedules')[0]!.body as Json
    // The untouched exclusions and the unset branch are the defaults, so they are not sent.
    expect(body.params).toEqual({ labels_include: ['swarm', 'good first issue'] })
  })

  it('does not save an edit that changes nothing (AUT-8)', async () => {
    const { calls } = await mount(
      'schedule=sch_a',
      routes((m, url) => (m === 'GET' && url.split('?')[0] === '/v1/schedules/sch_a' ? { status: 200, body: { schedule: schedule(), firings: [] } } : null)),
    )
    await screen.findByRole('heading', { name: 'Weekday sweep', level: 1 }, WAIT)
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    await waitFor(() => expect(field('Name')).toBeTruthy(), WAIT)
    const save = () => screen.getByRole('button', { name: 'Save changes' }) as HTMLButtonElement
    // MUTATION: drop the "Nothing has changed yet." check and Save sends `{revision}` alone.
    expect(save().disabled).toBe(true)
    expect(visible(screen.getByTestId('sc-form'))).toContain('Nothing has changed yet.')
    fireEvent.click(save())
    expect(sent(calls, 'PATCH', '/v1/schedules/sch_a')).toHaveLength(0)
    fireEvent.change(field('Name'), { target: { value: 'Weekday sweep 2' } })
    expect(save().disabled).toBe(false)
  })

  it('keeps Create disabled while the preview says the expression is not a schedule (AUT-10)', async () => {
    let answer: Reply = { status: 200, body: PREVIEW }
    await mount('schedule=new', routes((m, url) => (m === 'POST' && url === '/v1/schedules:preview' ? answer : null)))
    await waitFor(() => expect(field('Name')).toBeTruthy(), WAIT)
    fireEvent.change(field('Name'), { target: { value: 'Weekday sweep' } })
    const create = () => screen.getByRole('button', { name: 'Create schedule' }) as HTMLButtonElement
    await screen.findByText(/at least 1 day apart/, undefined, WAIT)
    expect(create().disabled).toBe(false)
    answer = { status: 422, body: { code: 'invalid_cron', message: 'minute: 61 is outside 0-59' } }
    fireEvent.change(field('Cron expression'), { target: { value: '61 * * * *' } })
    await waitFor(() => expect(screen.getByTestId('sc-preview').getAttribute('data-refused')).toBe('true'), WAIT)
    // MUTATION: leave the 422 out of `previewBlocks` and Create stays live.
    await waitFor(() => expect(create().disabled).toBe(true), WAIT)
    answer = { status: 200, body: { preview: { ...PREVIEW.preview, refusal: { message: 'issue-sweep fires at most every 15 minutes' } } } }
    fireEvent.change(field('Cron expression'), { target: { value: '*/5 * * * *' } })
    await waitFor(() => expect(visible(screen.getByTestId('sc-preview'))).toContain('This type would refuse it'), WAIT)
    await waitFor(() => expect(create().disabled).toBe(true), WAIT)
  })

  it('names each control by its label alone and describes it by its hint (AUT-16)', async () => {
    await mount('schedule=new', routes(() => null))
    await waitFor(() => expect(field('Name')).toBeTruthy(), WAIT)
    // MUTATION: drop `aria-labelledby` from `Segmented` and the group has no name, so none of these is found.
    for (const label of ['Scope', 'Run', 'Plan', 'Merge', 'Who may approve']) expect(screen.getByRole('radiogroup', { name: label }), label).toBeTruthy()
    // MUTATION: draw any of them as a `<select>` again and it is a combobox.
    expect(screen.queryAllByRole('combobox', { name: /^(Scope|Run|Plan|Merge|Who may approve)$/ })).toEqual([])
    const run = seg('Run')
    const hint = document.getElementById(run.getAttribute('aria-describedby') ?? '')
    expect(hint?.textContent).toBe('type default: auto')
    expect(screen.getByRole('textbox', { name: 'Cron expression' })).toBeTruthy()
  })

  it('names the approvers group "Who may approve", proposal B’s words (A3-LABEL)', async () => {
    await mount('schedule=new', routes(() => null))
    await waitFor(() => seg('Merge'), WAIT)
    // MUTATION: label the group "Approvers" again and it is found by that name, not by BGD’s pick of 2026-10-28.
    const group = screen.getByRole('radiogroup', { name: 'Who may approve' })
    expect(within(group).getAllByRole('radio').map((r) => r.textContent)).toEqual(['any member', 'the owner only'])
    expect(screen.queryByRole('radiogroup', { name: 'Approvers' })).toBeNull()
  })
})

// A3-SEGMENTED (part of #2417): BGD picked proposal B's segmented controls
// over dropdowns for Scope, Run, Plan, Merge and Approvers, 2026-10-28
// (docs/web-ui/mockups/automate-b.html #A3a, cards 2 and 4).
describe('A3: the five choices are segmented controls', () => {
  const FIVE = ['Scope', 'Run', 'Plan', 'Merge', 'Who may approve'] as const

  it('shows every value of each group without opening anything, in today’s order', async () => {
    await mount('schedule=new', routes(() => null, [sweepType({ creatable_scopes: ['repos', 'all'] })]))
    await waitFor(() => seg('Scope'), WAIT)
    // MUTATION: draw any of the five as a `<select>` and its values are options behind a click, not radios.
    expect(words('Scope')).toEqual(['Chosen repositories', 'Every registered repository'])
    expect(words('Run')).toEqual(['auto', 'approve'])
    expect(words('Plan')).toEqual(['auto', 'approve'])
    expect(words('Merge')).toEqual(['off', 'approve'])
    expect(words('Who may approve')).toEqual(['any member', 'the owner only'])
    for (const label of FIVE) {
      // MUTATION: put the unchosen segments behind a disclosure (a closed `<details>` or `hidden`) and one is found inside it.
      for (const r of within(seg(label)).getAllByRole('radio')) expect(r.closest('[hidden], details:not([open]), select'), `${label}: ${r.textContent}`).toBeNull()
      // MUTATION: drop the shared `.c-seg` and the group is drawn in a new visual language.
      expect(seg(label).classList.contains('c-seg'), label).toBe(true)
    }
  })

  it('checks the chosen value, starting at the type’s defaults, and a click moves the check', async () => {
    await mount('schedule=new', routes(() => null))
    await waitFor(() => seg('Merge'), WAIT)
    const checked = (label: string) => within(seg(label)).getAllByRole('radio', { checked: true }).map((r) => r.textContent)
    // MUTATION: compare against the label instead of the value and nothing is checked.
    expect(checked('Scope')).toEqual(['Chosen repositories'])
    expect(checked('Run')).toEqual(['auto'])
    expect(checked('Plan')).toEqual(['auto'])
    expect(checked('Merge')).toEqual(['approve'])
    expect(checked('Who may approve')).toEqual(['any member'])
    pick('Merge', 'off')
    // MUTATION: drop `onClick` from the segments and the check stays on approve.
    expect(checked('Merge')).toEqual(['off'])
  })

  it('moves and chooses with the arrow keys, wrapping, with one Tab stop per group', async () => {
    await mount('schedule=new', routes(() => null))
    await waitFor(() => seg('Plan'), WAIT)
    const radios = () => within(seg('Plan')).getAllByRole('radio')
    // MUTATION: leave every segment at tabIndex 0 and Tab walks each value.
    expect(radios().map((r) => r.tabIndex)).toEqual([0, -1])
    radios()[0]!.focus()
    fireEvent.keyDown(radios()[0]!, { key: 'ArrowRight' })
    // MUTATION: drop the `onKeyDown` and the check stays on auto.
    expect(radios()[1]!.getAttribute('aria-checked')).toBe('true')
    expect(document.activeElement).toBe(radios()[1])
    expect(radios().map((r) => r.tabIndex)).toEqual([-1, 0])
    // MUTATION: stop at the ends instead of wrapping and the check stays on approve.
    fireEvent.keyDown(radios()[1]!, { key: 'ArrowDown' })
    expect(radios()[0]!.getAttribute('aria-checked')).toBe('true')
    fireEvent.keyDown(radios()[0]!, { key: 'ArrowLeft' })
    expect(radios()[1]!.getAttribute('aria-checked')).toBe('true')
  })

  it('draws a value below the type’s floor disabled, with its reason, and the arrows pass over it', async () => {
    const strict = sweepType({
      default_gate: { plan: 'auto', run: 'approve', merge: 'approve', approvers: 'members', approval_ttl_hours: 72 },
      floor_gate: { plan: 'auto', run: 'approve', merge: 'auto', approvers: 'members', approval_ttl_hours: 72 },
    })
    await mount('schedule=new', routes(() => null, [strict]))
    await waitFor(() => seg('Run'), WAIT)
    const auto = within(seg('Run')).getByRole('radio', { name: 'auto' }) as HTMLButtonElement
    // MUTATION: drop `gateOptions` from Run and auto is offered, for the API to refuse with gate_below_floor.
    expect(auto.disabled).toBe(true)
    const reason = document.getElementById(auto.getAttribute('aria-describedby') ?? '')
    // MUTATION: drop the reason's line and the disabled value says nothing about why.
    expect(reason?.textContent).toBe('auto: below issue-sweep’s floor (approve), refused for everyone')
    expect(visible(seg('Run').parentElement!)).toContain('below issue-sweep’s floor (approve)')
    // Plan's floor is auto, so neither of its values is off.
    expect(within(seg('Plan')).getAllByRole('radio').map((r) => (r as HTMLButtonElement).disabled)).toEqual([false, false])
    const approve = within(seg('Run')).getByRole('radio', { name: 'approve' })
    fireEvent.keyDown(approve, { key: 'ArrowRight' })
    // MUTATION: walk every segment instead of the live ones and the check lands on the disabled auto.
    expect(approve.getAttribute('aria-checked')).toBe('true')
    expect(auto.getAttribute('aria-checked')).toBe('false')
  })

  it('wraps a group onto a second line at 390 and never scrolls sideways', async () => {
    await mount('schedule=new', routes(() => null))
    await waitFor(() => seg('Scope'), WAIT)
    for (const label of FIVE) {
      // MUTATION: drop the shared phone rule's `flex-wrap: wrap` and the group runs off the card.
      expect(painted(seg(label), 'flex-wrap', PHONE), label).toBe('wrap')
      expect(painted(seg(label), 'max-width', PHONE), label).toBe('100%')
      // MUTATION: give the group `overflow-x: auto` (as `.pp-filter` does) and it scrolls sideways.
      expect(['auto', 'scroll']).not.toContain(painted(seg(label), ['overflow', 'overflow-x'], PHONE))
    }
  })

  // The review's render (2026-10-28): at 1440 Scope sat beside Name in the
  // two-column `.sc-facts` grid, ~200px wide, and broke onto two rows with an
  // empty slot beside "Chosen repositories". Proposal B draws it as one row.
  it('gives Scope the card’s full width at desktop, and a wrapped segment fills its row', async () => {
    const DESK: CascadeEnv = { width: 1440 }
    await mount('schedule=new', routes(() => null, [sweepType({ creatable_scopes: ['repos', 'all'] })]))
    await waitFor(() => seg('Scope'), WAIT)
    const field = seg('Scope').parentElement!
    // MUTATION: drop `sc-wide` from Scope's field and it is one ~200px cell of the Name | Scope grid again.
    expect(field.parentElement!.classList.contains('sc-facts')).toBe(true)
    expect(painted(field, 'grid-column', DESK)).toBe('1 / -1')
    for (const r of within(seg('Scope')).getAllByRole('radio')) {
      // MUTATION: drop `flex: 1 0 auto` from the desktop `.ae-seg` rule and a wrapped segment leaves an empty slot beside it.
      expect(painted(r, 'flex', DESK), r.textContent ?? '').toBe('1 0 auto')
    }
  })
})
