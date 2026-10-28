// THE SCHEDULE SHEET, ITS ACTIONS AND THE TYPE TILES AFTER VISUAL QA PASS 1
// (lane QA1-FIX-37, part of #2596 and #2391; findings automate-03, -04, -11,
// -12, -14, -15, -17, -18 and -20 of the sweep on the fixture console).
//
// WHAT EACH CASE HOLDS:
//   * a sheet opened from its row starts with focus on its own heading, not
//     on the list's heading behind it; Escape closes it wherever focus is;
//     Close, Escape and `‹ Schedules` hand focus back to the row's link;
//   * the head's actions are one plain row, with no phone fold's wrapper left
//     over and no comment promising actions on a list row (frame A7b draws
//     all five on a phone);
//   * Pause asks for an optional one-line reason and sends it;
//   * after an action answers, focus is on the notice that says what
//     happened, not dropped to <body>;
//   * the next firings say which clock they are in, beside them, with a Z
//     on a UTC schedule's times;
//   * a gate the schedule leaves unset says what applies: the type's
//     default, any member, the 72 h expiry, each marked as a default;
//   * an unavailable type's tile is not faded, so its reason reads at 4.5:1;
//   * a firing's run links on a phone keep near their text height, each a
//     24px touch target no taller than the list's pitch, so no link's
//     target covers its neighbour.
//
// The bodies are `routes/schedules.py`'s shapes. Placeholders only; nothing
// is token-shaped.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { Routed } from './screenroute'
import type { CascadeEnv } from './cssgate'
import { THEMES, painted, resolveColour } from './marks'
import { contrast, over } from './spaceprobe'
import { holdScheduleClock, minutesAgo } from './scheduleclock'
import { TemplateTiles } from '../ScheduleTemplates'
import type { ScheduleTypeRow } from '../api'
import SCHEDULES_TSX from '../Schedules.tsx?raw'

const WAIT = { timeout: 4000 }
const WIDE: CascadeEnv = { width: 1440 }
const PHONE: CascadeEnv = { width: 390 }

type Json = Record<string, unknown>

function schedule(id: string, over: Json = {}): Json {
  return {
    schedule_id: id,
    tenant_id: 'eng',
    name: `Sweep ${id}`,
    type: 'issue-sweep',
    scope: { mode: 'all' },
    cron: '0 2 * * *',
    timezone: 'Europe/London',
    params: {},
    gate: { run: 'auto', plan: 'auto', merge: 'approve', approvers: 'members', approval_ttl_hours: 72 },
    budget: { per_run_usd: 15, per_day_usd: 120, max_concurrent: 4 },
    policy: { overlap: 'skip', catch_up: 'skip', jitter: true, dry_run: false },
    state: 'enabled',
    pause: null,
    owner: 'operator@example.com',
    created_by: 'operator@example.com',
    created_at: minutesAgo(10_000),
    next_run_at: minutesAgo(-300),
    last_firing: null,
    consecutive_failures: 0,
    revision: 3,
    words: 'every day at 02:00',
    tier: 'R2',
    spend_today: { day: '2026-10-27', reported_usd: 0, unreported_attempts: 0, coverage: 'complete' },
    pending_approvals: 0,
    state_word: 'active',
    ...over,
  }
}

type Routes = Record<string, { status: number; body: unknown }>

const TYPE_ROW = { name: 'issue-sweep', description: 'Start an issue run for each ready open issue.', default_gate: { run: 'approve', plan: 'auto', merge: 'approve' }, floor_gate: { merge: 'approve' } }

function routes(s: Json): Routes {
  const id = String(s.schedule_id)
  return {
    '/v1/schedules': { status: 200, body: { schedules: [s], tenant_id: 'eng', summary: { by_state: { active: 1 }, pending_approvals: 0, oldest_expires_at: null } } },
    [`/v1/schedules/${id}`]: { status: 200, body: { schedule: s, firings: [] } },
    '/v1/schedule-types': { status: 200, body: { types: [TYPE_ROW] } },
    '/v1/approvals': { status: 200, body: { approvals: [], tenant_id: 'eng' } },
    '/v1/repositories': { status: 200, body: { repositories: [] } },
  }
}

const NEXT_LONDON = ['2026-10-28T02:00:00+00:00', '2026-10-29T02:00:00+00:00', '2026-10-30T02:00:00+00:00', '2026-10-31T02:00:00+00:00', '2026-11-01T02:00:00+00:00']

async function mount(r: Routes, start: string | null, writes: (m: string, path: string) => { status: number; body: unknown } | null = () => null) {
  const calls = serve((m, url) => {
    const path = url.split('?')[0]!
    if (m === 'POST' && path === '/v1/schedules:preview') return { status: 200, body: { preview: { words: 'every day at 02:00', next: NEXT_LONDON, min_gap_minutes: 1440, refusal: null } } }
    if (m !== 'GET') return writes(m, path)
    return r[path] ?? null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SchedulesScreen } = await import('../Schedules')
  const go = vi.fn()
  const utils = render(<Routed Screen={SchedulesScreen} section="automate/schedules" start={start} spy={go} />)
  return { ...utils, calls, go }
}

async function open(s: Json, writes?: (m: string, path: string) => { status: number; body: unknown } | null) {
  const m = await mount(routes(s), `schedule=${String(s.schedule_id)}`, writes)
  await screen.findByRole('heading', { name: String(s.name), level: 1 }, WAIT)
  await waitFor(() => expect(document.querySelector('[data-sec="gate"]')).not.toBeNull(), WAIT)
  return m
}

const sheet = () => document.querySelector<HTMLElement>('aside.as-sheet')
const sec = (name: string) => sheet()!.querySelector<HTMLElement>(`[data-sec="${name}"]`)!
function kv(root: HTMLElement, name: string): string {
  const dt = [...root.querySelectorAll('dt')].find((d) => visible(d) === name)
  expect(dt, `no ${name}`).toBeTruthy()
  return visible(dt!.nextElementSibling)
}
const posts = (calls: { method: string; url: string; body: unknown }[], path: string) => calls.filter((c) => c.method === 'POST' && c.url.split('?')[0] === path)

const hosts: HTMLElement[] = []
beforeEach(() => holdScheduleClock())
afterEach(() => {
  for (const h of hosts.splice(0)) h.remove()
  vi.useRealTimers()
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('automate-03: the sheet takes focus, closes on Escape anywhere, and gives focus back to its row', () => {
  /** Open the sheet from the list's row link, as a reader does. */
  async function openFromRow() {
    const s = schedule('sch_a')
    const m = await mount(routes(s), null)
    fireEvent.click(await screen.findByRole('link', { name: 'Sweep sch_a' }, WAIT))
    const h = await screen.findByRole('heading', { name: 'Sweep sch_a', level: 1 }, WAIT)
    return { ...m, h }
  }
  const rowLink = () => document.querySelector<HTMLElement>('tr[data-schedule="sch_a"] a.al-name')

  it('opens with focus on the sheet’s own heading', async () => {
    const { h } = await openFromRow()
    // MUTATION: focus `.as-close, .as-back` again and focus lands on `‹ Schedules`, which a desktop does not draw.
    await waitFor(() => expect(document.activeElement).toBe(h), WAIT)
    expect(sheet()!.contains(document.activeElement)).toBe(true)
  })

  it('closes on Escape with focus outside it, and Close hands focus back to the row’s link', async () => {
    await openFromRow()
    ;(document.activeElement as HTMLElement | null)?.blur()
    expect(document.activeElement).toBe(document.body)
    fireEvent.keyDown(document, { key: 'Escape' })
    await waitFor(() => expect(sheet()).toBeNull(), WAIT)
    // MUTATION: drop `sheetClosed` from the sheet's close and focus stays on <body>.
    await waitFor(() => expect(document.activeElement).toBe(rowLink()), WAIT)
    fireEvent.click(rowLink()!)
    await screen.findByRole('heading', { name: 'Sweep sch_a', level: 1 }, WAIT)
    fireEvent.click(within(sheet()!).getByRole('button', { name: 'Close this schedule' }))
    await waitFor(() => expect(sheet()).toBeNull(), WAIT)
    await waitFor(() => expect(document.activeElement).toBe(rowLink()), WAIT)
    // `‹ Schedules` (the phone's way back) does the same.
    fireEvent.click(rowLink()!)
    await screen.findByRole('heading', { name: 'Sweep sch_a', level: 1 }, WAIT)
    fireEvent.click(within(sheet()!).getByRole('link', { name: '‹ Schedules' }))
    await waitFor(() => expect(sheet()).toBeNull(), WAIT)
    await waitFor(() => expect(document.activeElement).toBe(rowLink()), WAIT)
  })

  it('does not move focus on a later visit to the list (control)', async () => {
    await mount(routes(schedule('sch_a')), null)
    await waitFor(() => expect(rowLink()).not.toBeNull(), WAIT)
    expect(document.activeElement).not.toBe(rowLink())
  })
})

describe('automate-04 and automate-17: the head draws its actions as one row, and nothing promises them on a list row', () => {
  it('leaves no phone fold wrapper in the head, and the actions are its own children', async () => {
    await open(schedule('sch_a'))
    const head = screen.getByTestId('sc-acts-head')
    // MUTATION: put `<span className="sc-fold" id={foldId}>` back around Dry run, Pause and Delete.
    expect(head.querySelector('.sc-fold')).toBeNull()
    const parents = [...head.querySelectorAll('button')].map((b) => (b.parentElement === head ? 'head' : b.parentElement!.className))
    expect(parents.every((p) => p === 'head' || p === 'c-btn-wrap')).toBe(true)
    expect(within(head).getAllByRole('button').map((b) => visible(b))).toEqual(['Run now', 'Dry run', 'Pause', 'Delete…'])
  })

  it('keeps no row branch or fold id in ScheduleActions, nor the comment promising them', () => {
    const body = SCHEDULES_TSX.slice(SCHEDULES_TSX.indexOf("A SCHEDULE'S ACTIONS"), SCHEDULES_TSX.indexOf('export function Said'))
    // MUTATION: restore "On a row: Run now, and Resume" or the `where === 'row'` branch.
    expect(body).not.toMatch(/On a row: Run now/)
    expect(body).not.toMatch(/where === 'row'|foldId/)
  })
})

describe('automate-12: Pause asks for an optional reason and sends it', () => {
  const paused = (m: string, path: string) =>
    m === 'POST' && path === '/v1/schedules/sch_a:pause' ? { status: 200, body: { schedule: schedule('sch_a', { state: 'paused', next_run_at: null }) } } : null

  it('sends the reason written, trimmed', async () => {
    const { calls } = await open(schedule('sch_a'), paused)
    fireEvent.click(within(screen.getByTestId('sc-acts-head')).getByRole('button', { name: 'Pause' }))
    // MUTATION: Pause sends at once again and there is no dialog to write a reason in.
    const dialog = await screen.findByRole('dialog', undefined, WAIT)
    expect(posts(calls, '/v1/schedules/sch_a:pause')).toHaveLength(0)
    const box = within(dialog).getByRole('textbox')
    expect(box.getAttribute('maxlength')).toBe('500')
    fireEvent.change(box, { target: { value: '  holiday week ' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Pause' }))
    await waitFor(() => expect(posts(calls, '/v1/schedules/sch_a:pause')).toHaveLength(1), WAIT)
    expect(posts(calls, '/v1/schedules/sch_a:pause')[0]!.body).toEqual({ reason: 'holiday week' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), WAIT)
  })

  it('sends nothing on Keep it running', async () => {
    const { calls } = await open(schedule('sch_a'), paused)
    fireEvent.click(within(screen.getByTestId('sc-acts-head')).getByRole('button', { name: 'Pause' }))
    const dialog = await screen.findByRole('dialog', undefined, WAIT)
    fireEvent.click(within(dialog).getByRole('button', { name: 'Keep it running' }))
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(posts(calls, '/v1/schedules/sch_a:pause')).toHaveLength(0)
  })
})

describe('automate-15: after an action answers, focus is on what it said', () => {
  it('focuses the notice once Dry run answers', async () => {
    await open(schedule('sch_a'), (m, path) =>
      m === 'POST' && path === '/v1/schedules/sch_a:run'
        ? { status: 200, body: { firing: { firing_id: 'sch_a:dry', schedule_id: 'sch_a', trigger: 'run_now', state: 'done', work: [], skip: { code: 'DRY_RUN' }, outcome: 'skipped', cost: null } } }
        : null,
    )
    const dry = within(screen.getByTestId('sc-acts-head')).getByRole('button', { name: 'Dry run' })
    dry.focus()
    fireEvent.click(dry)
    const said = await screen.findByTestId('sc-said', undefined, WAIT)
    // MUTATION: drop Said's focus effect and focus falls to <body> as the pressed button turns disabled.
    await waitFor(() => expect(document.activeElement).toBe(said), WAIT)
    expect(said.getAttribute('tabindex')).toBe('-1')
  })
})

describe('automate-14: the next firings say which clock they are in', () => {
  it('names the schedule’s zone beside the chips', async () => {
    await open(schedule('sch_a'))
    const chips = await screen.findByRole('list', { name: 'The next firings, in Europe/London' }, WAIT)
    // MUTATION: drop the caption and the chips stand beside nothing that names their clock.
    expect(visible(screen.getByTestId('as-next-zone'))).toBe('Next five, in Europe/London:')
    expect(visible(chips.querySelector('li'))).toBe('Oct 28 02:00')
  })

  it('writes a UTC schedule’s times with Z', async () => {
    await open(schedule('sch_a', { timezone: 'UTC' }))
    const chips = await screen.findByRole('list', { name: 'The next firings, in UTC' }, WAIT)
    // MUTATION: drop the Z and a UTC time reads as any other clock's.
    expect(visible(chips.querySelector('li'))).toBe('Oct 28 02:00Z')
  })
})

describe('automate-20: a gate the schedule leaves unset says what applies', () => {
  it('reads the type’s default, any member, and the 72 h expiry, each marked as a default', async () => {
    await open(schedule('sch_a', { gate: { plan: 'approve' } }))
    const g = sec('gate')
    // MUTATION: say "— not set" again and a gate that waits on an approval reads as if nobody could approve it.
    await waitFor(() => expect(kv(g, 'Run')).toBe('waits for an approval (the type’s default)'), WAIT)
    expect(kv(g, 'Plan')).toBe('waits for an approval')
    expect(kv(g, 'Who approves')).toBe('any member of this workspace (default)')
    expect(kv(g, 'Expiry')).toBe('an approval nobody decides expires after 72 h (default)')
    expect(visible(g)).not.toMatch(/not set/)
  })

  it('says set values without "(default)" (control)', async () => {
    await open(schedule('sch_a'))
    const g = sec('gate')
    expect(kv(g, 'Run')).toBe('starts by itself')
    expect(kv(g, 'Who approves')).toBe('any member of this workspace')
    expect(kv(g, 'Expiry')).toBe('an approval nobody decides expires after 72 h')
  })

  it('says the type’s default could not be read when the catalogue does not name the type', async () => {
    const s = schedule('sch_a', { gate: {} })
    const r = { ...routes(s), '/v1/schedule-types': { status: 200, body: { types: [] } } }
    await mount(r, 'schedule=sch_a')
    await waitFor(() => expect(document.querySelector('[data-sec="gate"]')).not.toBeNull(), WAIT)
    await waitFor(() => expect(kv(sec('gate'), 'Run')).toBe('— the type’s default, which the catalogue did not say'), WAIT)
  })
})

describe('automate-11: an unavailable type’s tile keeps its reason readable', () => {
  const ROW = (over: Partial<ScheduleTypeRow>): ScheduleTypeRow =>
    ({ name: 'pr-shepherd', description: 'Shepherd pull requests.', params_schema: {}, default_gate: {}, floor_gate: {}, min_interval_minutes: 60, budget_caps: null, scopes: ['repos'], available: true, disabled_reason: null, ...over }) as ScheduleTypeRow

  it('is not faded by the app’s disabled button, and its reason is at least 4.5:1 in both themes', () => {
    const app = document.createElement('div')
    app.className = 'app'
    document.body.appendChild(app)
    hosts.push(app)
    render(<TemplateTiles types={[ROW({ available: false, disabled_reason: 'needs a GitHub App with write access' })]} value="" onPick={() => undefined} />, { container: app })
    const tile = app.querySelector<HTMLButtonElement>('.sc-tile.is-off')!
    expect(tile.disabled).toBe(true)
    let o = 1
    for (let n: Element | null = tile.querySelector('small'); n !== null; n = n.parentElement) {
      const v = painted(n, 'opacity', WIDE)
      if (v !== null) o *= Number(v)
    }
    // MUTATION: drop `.sc-tile.is-off:disabled { opacity: 1 }` and styles.css fades the tile to .45.
    expect(o).toBe(1)
    const ink = painted(tile.querySelector('small')!, 'color', WIDE)!
    for (const theme of THEMES) {
      const card = resolveColour('var(--surface)', theme)
      expect(contrast(over(resolveColour(ink, theme), card), card), theme).toBeGreaterThanOrEqual(4.5)
    }
  })
})

describe('automate-18: a firing’s run links on a phone keep their text height', () => {
  it('makes each link its own 24px target, no taller than the list’s pitch', () => {
    const host = document.createElement('div')
    host.innerHTML =
      '<table class="sc-table is-firings as-firings"><tbody><tr class="sc-row"><td data-label="Runs">' +
      '<ul class="as-work"><li><a class="mono" href="/work/runs?run=r1">run_1</a></li><li><a class="mono" href="/work/runs?run=r2">run_2</a></li></ul>' +
      '</td></tr></tbody></table>'
    document.body.appendChild(host)
    hosts.push(host)
    const list = host.querySelector('.as-work')!
    const link = host.querySelector('.as-work a')!
    const px = (v: string | null): number => (v === null || v === '0' ? 0 : Number.parseFloat(v))
    // The target: the link's own box, or a pseudo-element drawn round it.
    const before = painted(link, 'content', PHONE, 'before')
    const pseudo = before === null || before === 'none' ? 0 : px(painted(link, 'height', PHONE, 'before'))
    const linkBox = px(painted(link, 'min-height', PHONE))
    const target = Math.max(linkBox, pseudo)
    // The pitch: one li (sized by its link) plus the list's gap.
    const pitch = linkBox + px(painted(list, 'gap', PHONE))
    // MUTATION: restore `.sc-row .as-work a::before { height: 44px }` centred
    // on the link and the target (44) exceeds the pitch (24): run_2's box
    // covers run_1, the review's tap that opened the wrong run.
    expect(target).toBeLessThanOrEqual(pitch)
    // MUTATION: drop `.sc-row .as-work a { min-height: 24px }` and the link
    // takes the row's 44px again, a blank band round each one-line run.
    expect(linkBox).toBe(24)
    expect(target).toBeGreaterThanOrEqual(24)
    // The other links in a phone row keep their 44px (control).
    const other = document.createElement('a')
    host.querySelector('td')!.appendChild(other)
    expect(painted(other, 'min-height', PHONE)).toBe('44px')
  })
})
