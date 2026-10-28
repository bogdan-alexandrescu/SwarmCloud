// THE MERGE SWITCH DIALOG, BLOCKED STATE UP FRONT (owner review 2026-10-11):
// the owner, as the gate's last editor, opened "Merge automatically…" and met
// two paragraphs of rules, a cancel that read like a label floating right, a
// pale primary ending in "…" alone on the row below, and the one line that
// mattered -- that they could not switch -- in small grey text under it, with
// no word on who could. Behind it the page head's actions clipped at the right.
//
// WHAT EACH CASE HOLDS:
//   * a blocked viewer meets the warning heading first, then the reason in one
//     sentence, then who CAN switch, with a Copy link for this schedule;
//   * the full rules are behind a Details disclosure; the short "What changes"
//     summary is all that is open;
//   * one action row: Close (a real button) before the primary; blocked, the
//     primary is disabled with aria-disabled, reads "Switch to automatic" and
//     carries no ellipsis, and is painted grey rather than the pale accent;
//   * it says "workspace" to a person, never "tenant" (VQ-SCHEDULES re-review);
//   * an eligible viewer sees the same structure with no warning and an
//     enabled "Switch to automatic merging";
//   * the page head's actions are one wrapping row that cannot outgrow its
//     container, at 390, 1024 and 1440.
//
// Placeholders only; nothing is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import { Routed } from './screenroute'
import { painted } from './marks'

const WAIT = { timeout: 4000 }

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

const SWEEP = {
  name: 'issue-sweep',
  description: 'Plans and runs open issues.',
  executor: 'api',
  params_schema: { properties: {} },
  default_gate: { plan: 'auto', run: 'auto', merge: 'approve', approvers: 'members', approval_ttl_hours: 72 },
  floor_gate: { plan: 'auto', run: 'auto', merge: 'auto', approvers: 'members', approval_ttl_hours: 72 },
  min_interval_minutes: 15,
  budget_caps: null,
  scopes: ['all', 'repos'],
  creatable_scopes: ['repos', 'all'],
  available: true,
  disabled_reason: null,
}

const me = (email: string, isAdmin: boolean, kind = 'group') => ({
  tenant: { tenant_id: 'eng', kind, principal: 'eng@example.com', display_name: 'Engineering' },
  principal: { email, domain: 'example.com', groups: [], is_admin: isAdmin },
})

/** The gate's last editor: whoever created it, with no later gate edit. */
const AUDIT = [{ schedule_id: 'sch_a', action: 'create', by: 'operator@example.com', at: hoursFromNow(-1), detail: {} }]

async function gateDialog(viewer: Json, current: Json = schedule(), extra: Handler = () => null) {
  const calls = serve((m, url, body) => {
    const hit = extra(m, url, body)
    if (hit !== null) return hit
    const path = url.split('?')[0]!
    if (m === 'GET' && path === '/v1/schedule-types') return { status: 200, body: { types: [SWEEP], tenant_id: 'eng' } }
    if (m === 'GET' && path === '/v1/schedules/sch_a') return { status: 200, body: { schedule: current, firings: [] } }
    if (m === 'GET' && path === '/v1/schedules/sch_a/audit') return { status: 200, body: { audit: AUDIT } }
    if (m === 'GET' && path === '/v1/tenants/me') return { status: 200, body: viewer }
    return null
  })
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { SchedulesScreen } = await import('../Schedules')
  const go = vi.fn()
  render(<Routed Screen={SchedulesScreen} section="automate/schedules" start="schedule=sch_a" spy={go} />)
  await screen.findByRole('heading', { name: 'Weekday sweep', level: 1 }, WAIT)
  // Frame A2 draws the gate as a section of the sheet, not a tab to open.
  await waitFor(() => expect(document.querySelector('aside.as-sheet [data-sec="gate"]')).not.toBeNull(), WAIT)
  fireEvent.click(await screen.findByRole('button', { name: 'Merge automatically…' }, WAIT))
  const dialog = await screen.findByRole('dialog', undefined, WAIT)
  // Let the viewer and audit reads land before reading what the dialog says.
  await waitFor(() => expect(calls.some((c) => c.url.startsWith('/v1/schedules/sch_a/audit'))).toBe(true), WAIT)
  await new Promise((r) => setTimeout(r, 20))
  return { dialog, calls, go }
}

/** The dialog's one action row, and the buttons in it, in order. */
function actionRow(dialog: HTMLElement): HTMLButtonElement[] {
  const rows = dialog.querySelectorAll('.c-acts')
  expect(rows).toHaveLength(1)
  return [...rows[0]!.querySelectorAll<HTMLButtonElement>('button')]
}

afterEach(() => {
  delete (navigator as unknown as { clipboard?: unknown }).clipboard
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('the merge switch dialog, for the gate’s last editor', () => {
  it('opens with the warning, then the reason, then who can switch it', async () => {
    const { dialog } = await gateDialog(me('operator@example.com', false))
    const warning = await within(dialog).findByRole('heading', { name: 'You can’t make this switch' }, WAIT)
    // MUTATION: draw the rules first and the warning is not the dialog's first block.
    const body = [...dialog.children].filter((e) => e.tagName !== 'H2')
    expect(body[0]!.contains(warning)).toBe(true)
    const said = visible(body[0]!)
    const reason = said.indexOf('You last changed this schedule’s gate, scope or spec')
    const who = said.indexOf('Another member of Engineering can switch it')
    expect(reason).toBeGreaterThan(said.indexOf('You can’t make this switch'))
    expect(who).toBeGreaterThan(reason)
    // A non-admin cannot read Admin › People's list, so it is pointed at, not quoted.
    // The console's word to a person is "workspace", never "tenant"
    // (VQ-SCHEDULES re-review minor). MUTATION: "this tenant" back and this fails.
    expect(said).toMatch(/another member of this workspace must make this switch/)
    expect(said).toMatch(/platform admin of this workspace \(Admin › People lists them\)/)
    expect(visible(dialog)).not.toMatch(/tenant/i)
    expect(within(body[0] as HTMLElement).getByRole('link', { name: 'Admin › People' }).getAttribute('href')).toBe('/admin/people')
    // The rules sit behind Details, after the short summary.
    const details = dialog.querySelector('details')
    expect(details).not.toBeNull()
    expect(details!.open).toBe(false)
    expect(details!.querySelector('summary')?.textContent).toBe('Details')
    expect(visible(dialog)).toMatch(/What changes/)
  })

  it('names the platform admins when this viewer can read them', async () => {
    const admins = [
      { email: 'owner@example.com', role: 'owner', granted_by: null, granted_at: null },
      { email: 'operator@example.com', role: 'admin', granted_by: 'owner@example.com', granted_at: null },
      { email: 'second@example.com', role: 'admin', granted_by: 'owner@example.com', granted_at: null },
    ]
    const { dialog } = await gateDialog(me('operator@example.com', true), schedule(), (m, url) =>
      m === 'GET' && url.split('?')[0] === '/v1/admin/people'
        ? { status: 200, body: { people: [], count: 0, pending: 0, audit: [], lendable_accounts: null, lendable_error: null, admins } }
        : null,
    )
    const warning = await within(dialog).findByTestId('sc-switch-blocked', undefined, WAIT)
    // MUTATION: list the viewer too, and the dialog names the person it refuses.
    await waitFor(() => expect(visible(warning)).toMatch(/or a platform admin: owner@example\.com, second@example\.com\./), WAIT)
    expect(visible(warning)).not.toMatch(/operator@example\.com/)
  })

  it('copies this schedule’s link, so it can be sent to whoever can switch it', async () => {
    const writeText = vi.fn(async () => undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    const { dialog } = await gateDialog(me('operator@example.com', false))
    fireEvent.click(await within(dialog).findByRole('button', { name: 'Copy link' }, WAIT))
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1), WAIT)
    const url = String((writeText.mock.calls[0] as unknown[])[0])
    expect(url).toMatch(/^https?:\/\/[^/]+\/automate\/schedules/)
    expect(url).toContain('sch_a')
    expect(url).toContain('tab=gate')
    await within(dialog).findByText('Link copied', undefined, WAIT)
  })

  it('has one action row: Close, then a disabled “Switch to automatic” with no ellipsis', async () => {
    const { dialog, calls } = await gateDialog(me('operator@example.com', false))
    await within(dialog).findByTestId('sc-switch-blocked', undefined, WAIT)
    const [close, go, ...rest] = actionRow(dialog)
    expect(rest).toHaveLength(0)
    expect(close!.textContent).toBe('Close')
    // A real button, not a ghost that reads as a label.
    expect(close!.className).not.toMatch(/is-ghost/)
    expect(go!.textContent).toBe('Switch to automatic')
    expect(go!.textContent).not.toMatch(/…|\.\.\./)
    expect(go!.className).toMatch(/is-primary/)
    expect(go!.disabled).toBe(true)
    expect(go!.getAttribute('aria-disabled')).toBe('true')
    // The reason is the warning, said once up top, not a second copy wrapped round the button.
    expect(go!.closest('.c-btn-wrap')).toBeNull()
    expect(go!.getAttribute('aria-describedby')).toBe(within(dialog).getByTestId('sc-switch-blocked').id)
    // Grey, not the pale accent that read as enabled.
    expect(painted(go!, 'background', { width: 1440 })).toBe('var(--surface-2)')
    expect(painted(go!, 'cursor', { width: 1440 })).toBe('not-allowed')
    fireEvent.click(go!)
    expect(within(dialog).queryByRole('textbox')).toBeNull()
    // The sheet's cadence asks `:preview` for the next firings, a read sent as a POST; nothing else is sent.
    expect(calls.filter((c) => c.method === 'POST' && !c.url.startsWith('/v1/schedules:preview'))).toHaveLength(0)
    fireEvent.click(close!)
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), WAIT)
  })

  it('blocks a non-admin in a platform repository, and says a platform admin must switch it', async () => {
    const { dialog } = await gateDialog(me('someone@example.com', false), schedule({ scope: { mode: 'platform' } }))
    const warning = await within(dialog).findByTestId('sc-switch-blocked', undefined, WAIT)
    expect(visible(warning)).toMatch(/platform repository/)
    expect(visible(warning)).toMatch(/A platform admin of this workspace must switch it \(Admin › People lists them\)/)
    expect(visible(dialog)).not.toMatch(/tenant/i)
    expect(actionRow(dialog)[1]!.disabled).toBe(true)
  })
})

describe('the merge switch dialog, for a viewer who can switch', () => {
  it('has the same structure with no warning, and an enabled primary', async () => {
    const { dialog } = await gateDialog(me('second@example.com', false))
    expect(within(dialog).queryByTestId('sc-switch-blocked')).toBeNull()
    expect(within(dialog).queryByRole('heading', { name: 'You can’t make this switch' })).toBeNull()
    expect(visible(dialog)).toMatch(/What changes/)
    expect(dialog.querySelector('details > summary')?.textContent).toBe('Details')
    const [close, go, ...rest] = actionRow(dialog)
    expect(rest).toHaveLength(0)
    expect(close!.textContent).toBe('Close')
    expect(go!.textContent).toBe('Switch to automatic merging')
    expect(go!.disabled).toBe(false)
    expect(go!.getAttribute('aria-disabled')).toBeNull()
    // The typed confirmation is kept.
    fireEvent.click(go!)
    await within(dialog).findByRole('textbox', undefined, WAIT)
    // MUTATION: "In a tenant of one person" back and this fails.
    expect(visible(dialog)).toMatch(/In a workspace of one person, this typed name stands in/)
    expect(visible(dialog)).not.toMatch(/tenant/i)
  })
})

describe('the sheet’s actions', () => {
  it('are one wrapping row that cannot outgrow its container, at 390, 1024 and 1440', async () => {
    await gateDialog(me('second@example.com', false))
    fireEvent.keyDown(document, { key: 'Escape' })
    // Frame A2: the sheet's action row (`as-acts`) is the old head row.
    const row = screen.getByTestId('as-acts')
    const acts = screen.getByTestId('sc-acts-head')
    expect(row.contains(acts)).toBe(true)
    expect(within(row).getByRole('button', { name: 'Edit' })).toBeTruthy()
    for (const width of [390, 1024, 1440]) {
      const env = { width }
      // MUTATION: drop the wrap and Edit runs off the right edge (owner's screenshot, 1440-1500px).
      expect(painted(row, 'flex-wrap', env)).toBe('wrap')
      expect(painted(row, 'min-width', env)).toBe('0')
      expect(painted(row, 'max-width', env)).toBe('100%')
      // Run now … Delete… and Edit are siblings of the one wrapping row, not a nested row Edit sits beside.
      expect(painted(acts, 'display', env)).toBe('contents')
      for (const wrap of row.querySelectorAll('.c-btn-wrap')) expect(painted(wrap, 'max-width', env)).toBe('100%')
    }
  })
})
