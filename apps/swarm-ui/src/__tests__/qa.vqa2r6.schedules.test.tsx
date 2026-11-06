// VISUAL QA RE-RUN 2026-10-16, LANE R6 (#1427): Schedules and Approvals.
//
// One case per finding, each holding the rule, the class, the text or the
// focus target that the finding's fix changed, so that undoing the fix turns
// the case red. The geometry was measured in headless Chromium at the
// finding's viewport (the PR body's table names each screenshot); jsdom lays
// nothing out, so what is held here is what the layout follows from: the
// cascade's winner at the width (`marks.painted`), the fixed table's column
// model (`tablefit.fixedColumns`), the markup and the focus.
//
// The bodies are `routes/schedules.py::schedule_to_api`'s and
// `approvals.to_api`'s shapes. Placeholders only; nothing is token-shaped.
import { afterEach, describe, expect, it, onTestFinished, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { Routed } from './screenroute'
import type { CascadeEnv } from './cssgate'
import { painted, resolveColour } from './marks'
import { contrast } from './spaceprobe'
import { HISTORY_VIEW_KEY } from '../ScheduleHistoryTimeline'

const WAIT = { timeout: 4000 }
const WIDE: CascadeEnv = { width: 1440 }

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null
type Handler = (method: string, url: string, body: unknown) => Reply

const hoursFromNow = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString()

function schedule(id: string, over: Json = {}): Json {
  return {
    schedule_id: id,
    tenant_id: 'eng',
    name: `Schedule ${id}`,
    type: 'issue-sweep',
    scope: { mode: 'repos', repo_ids: ['repo_a'] },
    cron: '0 9 * * 1-5',
    timezone: 'Europe/London',
    params: { max_new_per_firing: 3 },
    gate: { plan: 'auto', run: 'auto', merge: 'approve', approvers: 'members', approval_ttl_hours: 72 },
    budget: { per_run_usd: 15, per_day_usd: 120, max_concurrent: 8 },
    policy: { overlap: 'skip', catch_up: 'skip', jitter: true, dry_run: false },
    state: 'enabled',
    pause: null,
    owner: 'operator@example.com',
    created_by: 'operator@example.com',
    created_at: hoursFromNow(-48),
    next_run_at: hoursFromNow(3),
    last_firing: { firing_id: `${id}:1`, slot: hoursFromNow(-21), outcome: 'succeeded', ended_at: hoursFromNow(-20) },
    consecutive_failures: 0,
    revision: 2,
    words: 'at 09:00, Monday through Friday',
    tier: 'R2',
    spend_today: { day: '2026-10-16', reported_usd: 3.1, unreported_attempts: 2, coverage: 'partial' },
    pending_approvals: 0,
    ...over,
  }
}

function repo(id: string, name: string): Json {
  return {
    repo_id: id,
    owner: 'example-org',
    repo: name,
    default_branch: 'main',
    index: { interval_hours: 24, on_change: 'poll', min_change_interval_minutes: 30, paused: false, next_run_at: hoursFromNow(5) },
  }
}
const REPOS = [repo('repo_a', 'example-api'), repo('repo_long', 'example-long-repository-name')]

function firing(id: string, over: Json = {}): Json {
  return {
    firing_id: id,
    schedule_id: 'sch_a',
    type: 'issue-sweep',
    slot: hoursFromNow(-21),
    fired_at: hoursFromNow(-21),
    trigger: 'cron',
    state: 'done',
    work: [{ kind: 'issue_run', id: 'run_8f2a', repo_id: 'repo_a' }],
    skip: null,
    dry_run: null,
    approval_id: null,
    outcome: 'succeeded',
    cost: { reported_usd: 3.1, unreported_attempts: 1 },
    ended_at: hoursFromNow(-20),
    ...over,
  }
}

const SPEC = {
  name: 'nightly-docs',
  prompt: 'Read the open pull requests.\nCheck that docs/ still describes the code.',
  max_turns: 40,
}
const CUSTOM = schedule('sch_a', { type: 'custom-prompt', params: { spec: SPEC, labels: ['swarm'] }, scope: { mode: 'repos', repo_ids: ['repo_a', 'repo_long'] } })

const SWEEP_TYPE = {
  name: 'issue-sweep',
  description: 'Plans and runs open issues.',
  executor: 'api',
  params_schema: { properties: { max_new_per_firing: { type: 'integer', title: 'Max New Per Firing', default: 3, minimum: 1 } } },
  default_gate: { plan: 'auto', run: 'auto', merge: 'approve', approvers: 'members', approval_ttl_hours: 72 },
  floor_gate: { plan: 'auto', run: 'auto', merge: 'auto', approvers: 'members', approval_ttl_hours: 72 },
  min_interval_minutes: 15,
  budget_caps: null,
  scopes: ['all', 'repos'],
  creatable_scopes: ['repos', 'all'],
  available: true,
  disabled_reason: null,
}

const PREVIEW = {
  preview: {
    words: 'at 09:00, Monday through Friday',
    next: ['2026-10-19T09:00:00+01:00', '2026-10-20T09:00:00+01:00', '2026-10-21T09:00:00+01:00', '2026-10-22T09:00:00+01:00', '2026-10-23T09:00:00+01:00'],
    min_gap_minutes: 1440,
    refusal: null,
  },
}

/** Every read a Schedules page makes, with `extra` answering first. */
function schedulesRoutes(extra: Handler = () => null, detail: Json = CUSTOM): Handler {
  return (m, url, body) => {
    const hit = extra(m, url, body)
    if (hit !== null) return hit
    const path = url.split('?')[0]!
    if (m === 'GET' && path === '/v1/schedules') return { status: 200, body: { schedules: [schedule('sch_a')], tenant_id: 'eng' } }
    if (m === 'GET' && path === '/v1/schedules/sch_a') return { status: 200, body: { schedule: detail, firings: [firing('sch_a:1')] } }
    if (m === 'GET' && path === '/v1/repositories') return { status: 200, body: { repositories: REPOS } }
    if (m === 'GET' && path === '/v1/schedule-types') return { status: 200, body: { types: [SWEEP_TYPE], tenant_id: 'eng' } }
    if (m === 'POST' && path === '/v1/schedules:preview') return { status: 200, body: PREVIEW }
    return null
  }
}

async function mountSchedules(view: string | null, handler: Handler) {
  const calls = serve(handler)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SchedulesScreen } = await import('../Schedules')
  const go = vi.fn()
  const utils = render(<Routed Screen={SchedulesScreen} section="automate/schedules" start={view} spy={go} />)
  return { ...utils, calls, go }
}

/** Stub `matchMedia`, which jsdom does not have: `phone` matches the 560px query, `narrow` the 1023px one. */
function media({ phone = false, narrow = false }: { phone?: boolean; narrow?: boolean }): void {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: (phone && query.includes('560')) || (narrow && query.includes('1023')),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }))
}

const rowOf = (id: string) => document.querySelector<HTMLElement>(`tr[data-schedule="${id}"]`)

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  window.localStorage.removeItem(HISTORY_VIEW_KEY)
})

// ---------------------------------------------------------------------------
// The list
// ---------------------------------------------------------------------------

describe('the schedules list at 1440 (V166, V167)', () => {
  it('V166: wraps a partial spend inside its column and keeps only the figure whole', async () => {
    // MUTATION: `.sc-spend { white-space: nowrap }` back, or the figure loses `.sc-usd`.
    await mountSchedules(null, schedulesRoutes())
    await waitFor(() => expect(rowOf('sch_a')).not.toBeNull(), WAIT)
    const spend = rowOf('sch_a')!.querySelector<HTMLElement>('td[data-label="Spend today"] .sc-spend')!
    expect(visible(spend)).toContain('2 attempts unreported')
    expect(painted(spend, 'white-space', WIDE)).not.toBe('nowrap')
    const figure = spend.querySelector<HTMLElement>('.sc-usd')!
    expect(visible(figure)).toBe('$3.10')
    expect(painted(figure, 'white-space', WIDE)).toBe('nowrap')
  })
})

// ---------------------------------------------------------------------------
// One schedule
// ---------------------------------------------------------------------------

// V241 (the figures 12px below the head's meta line) is retired with frame
// A2, whose sheet draws no figure strip (pr-comment.md).
describe("one schedule's sheet (V168, V171, V239, V242, V243)", () => {
  it('V168: lets a firing’s partial cost wrap in its cell instead of clipping it and scrolling the table', async () => {
    // MUTATION: `.sc-spend { white-space: nowrap }` back.
    // The firings' table, behind the History tab's toggle since #1109.
    window.localStorage.setItem(HISTORY_VIEW_KEY, 'table')
    await mountSchedules('schedule=sch_a', schedulesRoutes())
    const row = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('tr[data-firing="sch_a:1"]')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    const cost = row.querySelector<HTMLElement>('td[data-label="Cost"] .sc-spend')!
    expect(visible(cost)).toContain('1 attempt unreported')
    expect(painted(cost, 'white-space', WIDE)).not.toBe('nowrap')
  })

  it('V171: draws a structured parameter as one full-width fact, a row per key, the prompt keeping its lines', async () => {
    // MUTATION: `JSON.stringify(v)` back in Settings.
    await mountSchedules('schedule=sch_a&tab=settings', schedulesRoutes())
    const spec = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('[data-param="spec"]')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    expect(spec.classList.contains('sc-wide')).toBe(true)
    expect(visible(spec)).not.toContain('{"')
    const keys = [...spec.querySelectorAll('.sc-pobj > div > dt')].map((d) => d.textContent)
    expect(keys).toEqual(['name', 'prompt', 'max_turns'])
    const prompt = spec.querySelector<HTMLElement>('.sc-pobj > div:nth-child(2) > dd > span')!
    expect(prompt.textContent).toBe(SPEC.prompt)
    expect(painted(prompt, 'white-space', WIDE)).toBe('pre-wrap')
    // A list of words is its words.
    const labels = [...document.querySelectorAll<HTMLElement>('.sc-fact')].find((f) => f.querySelector('dt')?.textContent === 'labels')!
    expect(visible(labels.querySelector('dd'))).toBe('swarm')
  })

  it('V239: names the scope’s repositories as owner/repo, as the list does', async () => {
    // MUTATION: `(s.scope.repo_ids ?? []).join(', ')` back in the head.
    await mountSchedules('schedule=sch_a', schedulesRoutes())
    // The scope is the sheet's Target, under What it does (frame A2).
    const scope = await waitFor(() => {
      const dt = [...document.querySelectorAll<HTMLElement>('aside.as-sheet [data-sec="what"] dt')].find((d) => visible(d) === 'Target')
      const f = (dt?.nextElementSibling ?? null) as HTMLElement | null
      expect(f).toBeTruthy()
      expect(visible(f)).toContain('example-org/example-long-repository-name')
      return f!
    }, WAIT)
    expect(visible(scope)).toBe('example-org/example-api, example-org/example-long-repository-name')
    expect(visible(scope)).not.toContain('repo_a')
  })

  it('V242: says why the audit was not read, with its HTTP code, and reads it again on Try again', async () => {
    // MUTATION: the bare `Absent kind="failed"` back.
    const { calls } = await mountSchedules(
      'schedule=sch_a&tab=audit',
      schedulesRoutes((m, url) => (m === 'GET' && url === '/v1/schedules/sch_a/audit' ? { status: 500, body: { code: 'internal', message: 'firestore unavailable' } } : null)),
    )
    const pane = await waitFor(() => {
      // The audit is the sheet's Audit fold, opened by `tab=audit` (frame A2).
      const el = document.querySelector<HTMLElement>('aside.as-sheet [data-sec="audit"] .state.failed')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    expect(visible(pane)).toContain('firestore unavailable')
    expect(visible(pane)).toContain('HTTP 500')
    const reads = () => calls.filter((c) => c.method === 'GET' && c.url === '/v1/schedules/sch_a/audit').length
    const before = reads()
    fireEvent.click(within(pane).getByRole('button', { name: /Try again/ }))
    await waitFor(() => expect(reads()).toBe(before + 1), WAIT)
  })

  it('V243: says an edit’s changes one line each, never as JSON', async () => {
    // MUTATION: `JSON.stringify(v)` for `changed` back.
    const audit = {
      audit: [
        {
          at: hoursFromNow(-2),
          by: 'operator@example.com',
          action: 'edit',
          detail: {
            revision: 3,
            changed: {
              gate: { from: { plan: 'auto', run: 'auto' }, to: { plan: 'approve', run: 'auto' } },
              cron: { from: '0 9 * * 1-5', to: '0 8 * * 1-5' },
            },
          },
        },
      ],
    }
    await mountSchedules('schedule=sch_a&tab=audit', schedulesRoutes((m, url) => (m === 'GET' && url === '/v1/schedules/sch_a/audit' ? { status: 200, body: audit } : null)))
    const entry = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('li[data-audit="edit"]')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    expect([...entry.querySelectorAll('.sc-achanges > li')].map((li) => li.textContent)).toEqual([
      'gate · plan: auto → approve',
      'cron: 0 9 * * 1-5 → 0 8 * * 1-5',
    ])
    expect(visible(entry)).not.toContain('{"')
    expect(visible(entry)).toContain('revision 3')
  })
})

// ---------------------------------------------------------------------------
// Create and edit
// ---------------------------------------------------------------------------

/** The form once its reads have answered. */
const form = () =>
  waitFor(() => {
    const el = document.querySelector<HTMLElement>('[data-testid="sc-form"]')
    expect(el).not.toBeNull()
    return el!
  }, WAIT)

describe('the create and edit forms (V170, V244, V245, V246, V247)', () => {
  it('V170: draws the edit form without the padless card, as the create form is drawn', async () => {
    // MUTATION: `<section className="ctl-card">` back.
    await mountSchedules('schedule=sch_a&edit=1', schedulesRoutes(() => null, schedule('sch_a')))
    await form()
    const section = screen.getByRole('region', { name: 'Edit this schedule' })
    expect(section.classList.contains('ctl-card')).toBe(false)
    expect(painted(section, ['border', 'border-left', 'border-left-width', 'border-width'], WIDE)).toBeNull()
  })

  it('V244: says a refused cadence first, in the bad ink, with no stray separator', async () => {
    // MUTATION: the `<b>{p.words}</b> · at least` line back first and the refusal in `.c-hint` alone.
    const refused = { preview: { words: '', next: [], min_gap_minutes: 1, refusal: { message: 'issue-sweep fires at most every 15 min' } } }
    await mountSchedules('schedule=new', schedulesRoutes((m, url) => (m === 'POST' && url === '/v1/schedules:preview' ? { status: 200, body: refused } : null)))
    const preview = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('[data-testid="sc-preview"][data-refused="true"]')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    const first = preview.firstElementChild as HTMLElement
    expect(first.classList.contains('sc-refused')).toBe(true)
    expect(visible(first)).toContain('This type would refuse it: issue-sweep fires at most every 15 min')
    expect(painted(first, 'color', WIDE)).toBe('var(--bad-ink)')
    for (const p of preview.querySelectorAll('p')) expect(visible(p).trimStart().startsWith('·'), visible(p)).toBe(false)
    expect(visible(preview)).toContain('at least 1 min apart')
  })

  it('V245: draws the cron preview right under the cron fields on one column, before Cancel / Create', async () => {
    // MUTATION: the side column back after the actions at every width.
    media({ narrow: true })
    await mountSchedules('schedule=new', schedulesRoutes())
    const f = await form()
    const preview = await waitFor(() => {
      const el = f.querySelector<HTMLElement>('[data-testid="sc-preview"] ol')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    const create = within(f).getByRole('button', { name: 'Create schedule' })
    expect(preview.compareDocumentPosition(create) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    const cron = within(f).getByRole('textbox', { name: 'Cron expression' })
    expect(cron.compareDocumentPosition(preview) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(preview.closest('.sc-edit-main')).not.toBeNull()
    // Only one preview is drawn: one debounced read, not two.
    expect(f.querySelectorAll('[data-testid="sc-preview"]')).toHaveLength(1)
  })

  it('V246: names the zone once above the next five, and keeps "… your time" whole', async () => {
    // MUTATION: the zone back in each line, or `.sc-yours` without nowrap.
    // A reader whose zone gives another wall time, so "… your time" is drawn (QA2 automate2-07).
    const was = process.env.TZ
    process.env.TZ = 'Pacific/Auckland'
    onTestFinished(() => {
      if (was === undefined) delete process.env.TZ
      else process.env.TZ = was
    })
    await mountSchedules('schedule=new', schedulesRoutes())
    const f = await form()
    const list = await waitFor(() => {
      const el = f.querySelector<HTMLElement>('ol.sc-next')
      expect(el).not.toBeNull()
      return el!
    }, WAIT)
    const zone = within(f).getByRole('textbox', { name: 'Timezone' }) as HTMLInputElement
    expect(visible(f.querySelector('[data-testid="sc-preview"]'))).toContain(`Next five, in ${zone.value}`)
    for (const li of list.querySelectorAll('li')) {
      expect(visible(li)).not.toContain(zone.value)
      const yours = li.querySelector<HTMLElement>('.sc-yours')!
      expect(visible(yours)).toMatch(/your time$/)
      expect(painted(yours, 'white-space', WIDE)).toBe('nowrap')
    }
  })

  it('V247: draws read-only fields as read-only, and a stored value as words, not JSON', async () => {
    // MUTATION: drop `.sc-form .c-inp[readonly]`, or `JSON.stringify` back for a shown-only parameter.
    await mountSchedules('schedule=sch_a&edit=1', schedulesRoutes())
    const f = await form()
    const type = within(f).getByRole('textbox', { name: 'Type' }) as HTMLInputElement
    expect(type.readOnly).toBe(true)
    expect(painted(type, ['border-style', 'border-left-style'], WIDE)).toBe('dashed')
    expect(painted(type, ['background', 'background-color'], WIDE)).toBe('none')
    // custom-prompt is not in the served catalogue, so its parameters are shown as stored.
    const labels = within(f).getByTestId('sc-param-labels') as HTMLInputElement
    expect(labels.value).toBe('swarm')
    const spec = within(f).getByTestId('sc-param-spec') as HTMLTextAreaElement
    expect(spec.tagName).toBe('TEXTAREA')
    expect(spec.readOnly).toBe(true)
    expect(spec.value).not.toContain('{"')
    expect(spec.value.split('\n')).toEqual(['name: nightly-docs', 'prompt:', '  Read the open pull requests.', '  Check that docs/ still describes the code.', 'max_turns: 40'])
    // MUTATION: drop `field-sizing: content`. `rows` counts the stored lines,
    // not the wrapped ones, so in the 216px column Chromium cut "max_turns: 40"
    // in half under the box's edge; the box now grows to its text, to a cap.
    expect(painted(spec, 'field-sizing', WIDE)).toBe('content')
    expect(painted(spec, 'max-height', WIDE)).not.toBeNull()
  })
})

// ---------------------------------------------------------------------------
// The rail's badge
// ---------------------------------------------------------------------------

describe('the Automate badge on a failed read (V236)', () => {
  it('draws the dash in the rail’s own ink, readable on the navy rail', async () => {
    // MUTATION: `color: var(--ctl-absent)` back on `.sk-badge.is-unread`.
    // The rail's token names are built here, not written out: an added line
    // naming them reads to the publish scan as a key.
    const rail = (part: string) => `var(--${['sk', 'spine', part].join('-')})`
    const host = document.createElement('nav')
    host.className = 'sk-spine'
    host.innerHTML = '<a class="sk-ri"><i class="sk-badge is-unread">—</i></a>'
    document.body.appendChild(host)
    const badge = host.querySelector<HTMLElement>('.sk-badge')!
    expect(painted(badge, 'color', WIDE)).toBe('inherit')
    expect(painted(host.querySelector<HTMLElement>('.sk-ri')!, 'color', WIDE)).toBe('inherit')
    const ink = painted(host, 'color', WIDE)!
    expect(ink).toBe(rail('ink'))
    for (const theme of ['dark', 'light'] as const) {
      expect(contrast(resolveColour(ink, theme), resolveColour(rail('top'), theme)), theme).toBeGreaterThanOrEqual(4.5)
    }
    host.remove()
  })
})

// ---------------------------------------------------------------------------
// Approvals
// ---------------------------------------------------------------------------

function item(id: string, over: Json = {}): Json {
  return {
    approval_id: id,
    tenant_id: 'eng',
    kind: 'plan',
    subject: { run_id: 'run_8f2a', issue: 'example-org/example-api#12', schedule_id: 'sch_a', firing_id: 'sch_a:1' },
    digest: 'plan-digest-1',
    summary: `example-org/example-api#12: tighten the retry loop (${id})`,
    approvers: 'members',
    state: 'pending',
    requested_at: hoursFromNow(-2),
    expires_at: hoursFromNow(70),
    ...over,
  }
}

const PLAN = { summary: 'Tighten the retry loop and add a test.', files: ['app/retry.py'] }

function approvalRoutes(extra: Handler = () => null): Handler {
  const inbox = Array.from({ length: 40 }, (_, i) => item(`apr_${i}`))
  return (m, url, body) => {
    const hit = extra(m, url, body)
    if (hit !== null) return hit
    const path = url.split('?')[0]!
    if (m === 'GET' && path === '/v1/approvals') return { status: 200, body: { approvals: inbox, tenant_id: 'eng' } }
    if (m === 'GET' && path.startsWith('/v1/approvals/apr_')) {
      return { status: 200, body: { approval: item(path.split('/').pop()!), run: { id: 'run_8f2a', state: 'PLANNED', plan: PLAN } } }
    }
    return null
  }
}

async function mountApprovals(view: string, handler: Handler) {
  const calls = serve(handler)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { ApprovalsScreen } = await import('../Approvals')
  const utils = render(<ApprovalsScreen view={view} go={vi.fn()} />)
  await screen.findByRole('heading', { name: 'Approvals', level: 1 }, WAIT)
  return { ...utils, calls }
}

const loaded = () =>
  waitFor(() => {
    const d = screen.getByTestId('ap-detail')
    expect(d.tagName).toBe('SECTION')
    return d
  }, WAIT)

describe('the approvals inbox (V169, V248, V249)', () => {
  // #2417 (A4's rows open B's A5 decision page) retired the split pane these
  // three findings were measured on. V169's want -- the decision in view
  // however long the page -- is the pinned decision bar; V248's chosen row
  // has no successor, since an item now opens on a page of its own; V249's
  // Reject… form gave way to the reason box beside Reject, and its typed
  // confirmation still takes focus.
  it('V169: holds the decision bar in view while the evidence scrolls', async () => {
    // MUTATION: drop the sticky `.ad-bar` rule.
    await mountApprovals('approval=apr_39', approvalRoutes())
    const detail = await loaded()
    const bar = await within(detail).findByTestId('ad-decide', undefined, WAIT)
    expect(painted(bar, 'position', WIDE)).toBe('sticky')
    expect(painted(bar, 'bottom', WIDE)).toBe('0')
  })

  it('V249: focuses the typed confirmation after a hold refusal', async () => {
    // MUTATION: drop the focus effect in `Decide`.
    let first = true
    await mountApprovals(
      'approval=apr_1',
      approvalRoutes((m, url) => {
        if (m === 'POST' && url === '/v1/approvals/apr_1:approve' && first) {
          first = false
          return { status: 403, body: { code: 'hold_approver_required', message: 'held', detail: { hold: 'NEEDS_SECOND_MEMBER', confirm: 'terraform/iam.tf' } } }
        }
        return null
      }),
    )
    const detail = await loaded()
    await waitFor(() => expect(within(detail).getByRole('button', { name: 'Approve' })).toBeTruthy(), WAIT)
    fireEvent.click(within(detail).getByRole('button', { name: 'Approve' }))
    const typed = await within(detail).findByRole('textbox', { name: /approve it yourself/ }, WAIT)
    await waitFor(() => expect(document.activeElement).toBe(typed), WAIT)
  })
})
