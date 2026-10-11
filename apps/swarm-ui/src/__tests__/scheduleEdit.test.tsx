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

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
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
  const utils = render(<SchedulesScreen view={view} go={go} />)
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

  it('shows the cron in words and the next five, in the schedule zone and the viewer’s, from the preview route', async () => {
    const { calls } = await mount('schedule=new', routes(() => null))
    await screen.findByRole('heading', { name: 'New schedule', level: 1 }, WAIT)
    const preview = await waitFor(() => {
      const el = screen.getByTestId('sc-preview')
      expect(visible(el)).toContain('Monday through Friday')
      return el
    }, WAIT)
    const next = within(preview).getAllByRole('listitem')
    expect(next).toHaveLength(5)
    expect(visible(next[0]!)).toContain('2026-10-12 09:00')
    expect(visible(next[0]!)).toContain('your time')
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
    const merge = await waitFor(() => field('Merge') as unknown as HTMLSelectElement, WAIT)
    // MUTATION: add `auto` to the form's merge options.
    expect([...merge.options].map((o) => o.value)).toEqual(['off', 'approve'])
    // No control anywhere in the form offers `auto` for merging: the
    // parameter that restates the gate is drawn read-only, not as its enum.
    const form = screen.getByTestId('sc-form')
    const mergeLabels = [...form.querySelectorAll('.c-lbl')].filter((e) => /merge/i.test(e.textContent ?? ''))
    expect(mergeLabels.length).toBe(2)
    for (const lbl of mergeLabels) {
      const control = lbl.closest('label')!.querySelector('input, select')!
      // MUTATION: draw the restating parameters through `editable()` and this is a select offering `auto`.
      if (control.tagName === 'SELECT') expect([...(control as HTMLSelectElement).options].map((o) => o.value)).not.toContain('auto')
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
    fireEvent.change(field('Plan'), { target: { value: 'approve' } })
    fireEvent.change(field('Merge'), { target: { value: 'off' } })
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
    fireEvent.change(field('Plan'), { target: { value: 'approve' } })
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
    await waitFor(() => expect(sent(calls, 'GET', '/v1/schedules/sch_a').length).toBe(2), WAIT)
    await screen.findByRole('button', { name: 'Edit' }, WAIT)
  })

  it('changes the gate without restating it in the parameters', async () => {
    const { calls } = await mount(
      'schedule=sch_a',
      detail((m, url) => (m === 'PATCH' && url === '/v1/schedules/sch_a' ? { status: 200, body: { schedule: schedule({ revision: 3 }) } } : null)),
    )
    await screen.findByRole('heading', { name: 'Weekday sweep', level: 1 }, WAIT)
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    await waitFor(() => expect(field('Plan')).toBeTruthy(), WAIT)
    fireEvent.change(field('Plan'), { target: { value: 'approve' } })
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
    fireEvent.click(screen.getByRole('tab', { name: /Gate/ }))
    return out
  }

  const MODE = '/v1/schedules/sch_a:merge-mode'

  it('switches to automatic with the revision, and the page reads the schedule again', async () => {
    const { calls } = await gateTab((m, url) => (m === 'POST' && url === MODE ? { status: 200, body: { schedule: schedule({ revision: 3, gate: { merge: 'auto' } }) } } : null))
    fireEvent.click(await screen.findByRole('button', { name: 'Merge automatically…' }, WAIT))
    const dialog = await screen.findByRole('dialog', undefined, WAIT)
    expect(visible(dialog)).toContain('hard stops')
    // §4.4, said where the decision is made: workflow files are refused, and
    // outside a platform repository the holds are a second member's.
    // MUTATION: say they "wait for the owner" and this fails.
    expect(visible(dialog)).toMatch(/a plan naming workflow files is refused/)
    expect(visible(dialog)).toMatch(/held for a second member to approve \(for the owner, in a platform repository\)/)
    expect(visible(dialog)).toMatch(/frozen contract is held for the owner/)
    expect(visible(dialog)).not.toMatch(/wait for the owner/)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Switch to automatic merging' }))
    await waitFor(() => expect(sent(calls, 'POST', MODE)).toHaveLength(1), WAIT)
    expect(sent(calls, 'POST', MODE)[0]!.body).toEqual({ mode: 'auto', revision: 2 })
    await waitFor(() => expect(sent(calls, 'GET', '/v1/schedules/sch_a').length).toBe(2), WAIT)
    // The re-read lands on the Gate tab it came from.
    await screen.findByTestId('sc-gate', undefined, WAIT)
  })

  it('asks for the typed name only when the server says a one-person tenant needs it', async () => {
    let n = 0
    const { calls } = await gateTab((m, url) => {
      if (m !== 'POST' || url !== MODE) return null
      n += 1
      return n === 1
        ? { status: 422, body: { code: 'confirmation_required', message: "type the schedule's name to switch merging to automatic" } }
        : { status: 200, body: { schedule: schedule({ revision: 3 }) } }
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Merge automatically…' }, WAIT))
    const dialog = await screen.findByRole('dialog', undefined, WAIT)
    expect(within(dialog).queryByRole('textbox')).toBeNull()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Switch to automatic merging' }))
    const box = await within(dialog).findByRole('textbox', undefined, WAIT)
    expect(visible(dialog)).toContain('only person in this tenant')
    const confirm = () => within(dialog).getByRole('button', { name: 'Switch to automatic merging' }) as HTMLButtonElement
    // MUTATION: drop the exact-name check and the button is live on an empty field.
    expect(confirm().disabled).toBe(true)
    fireEvent.change(box, { target: { value: 'Weekday sweep' } })
    expect(confirm().disabled).toBe(false)
    fireEvent.click(confirm())
    await waitFor(() => expect(sent(calls, 'POST', MODE)).toHaveLength(2), WAIT)
    expect(sent(calls, 'POST', MODE)[1]!.body).toEqual({ mode: 'auto', revision: 2, confirm: 'Weekday sweep' })
  })

  it('says which rule refused it', async () => {
    await gateTab((m, url) =>
      m === 'POST' && url === MODE ? { status: 403, body: { code: 'second_person_required', message: 'another member must' } } : null,
    )
    fireEvent.click(await screen.findByRole('button', { name: 'Merge automatically…' }, WAIT))
    const dialog = await screen.findByRole('dialog', undefined, WAIT)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Switch to automatic merging' }))
    await within(dialog).findByText(/another member must make this switch/, undefined, WAIT)
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
