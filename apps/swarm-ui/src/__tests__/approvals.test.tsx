// AUTOMATE › APPROVALS (docs/schedules.md §4.5, §6.1-§6.2, lane S7): the
// tenant's inbox at `/approvals`.
//
// WHAT EACH CASE HOLDS:
//   * Approvals is Automate's second tab, at `/approvals`, and one item is
//     `/approvals/<id>`, both ways; the panel's Approvals page carries the
//     inbox count the section does;
//   * a measured empty inbox is the `Absent` state, not a list of nothing;
//   * on a desktop the list and the chosen item sit side by side (variant B);
//     the item is read from `GET /v1/approvals/{id}` and shows its plan's
//     files and its hold before the verbs;
//   * Approve sends back exactly the digest that read showed, and the inbox
//     is read again; a refusal is the server's rule, said in place;
//   * a one-person tenant's own hold asks for the words the server names, and
//     the retry sends them as `confirm`;
//   * Reject needs a reason and sends it;
//   * at 390px the chosen item opens in place under its row (variant A), and
//     a row is a 44px target.
//
// The bodies are `routes/approvals.py`'s shapes. Placeholders only; nothing
// is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { SECTIONS } from '../App'
import { addressToPath, pathToAddress } from '../paths'
import { PANEL_PAGES } from '../Spine'

const WAIT = { timeout: 4000 }
const PHONE: CascadeEnv = { width: 390 }

type Json = Record<string, unknown>
type Reply = { status: number; body: unknown } | null
type Handler = (method: string, url: string, body: unknown) => Reply

const hoursFromNow = (h: number) => new Date(Date.now() + h * 3_600_000).toISOString()

function item(id: string, over: Json = {}): Json {
  return {
    approval_id: id,
    tenant_id: 'eng',
    kind: 'plan',
    subject: { run_id: 'run_8f2a', issue: 'example-org/example-api#12', schedule_id: 'sch_a', firing_id: 'sch_a:1' },
    digest: 'plan-digest-1',
    summary: 'example-org/example-api#12: tighten the retry loop',
    approvers: 'members',
    state: 'pending',
    requested_at: hoursFromNow(-2),
    expires_at: hoursFromNow(70),
    ...over,
  }
}

const PLAN = { summary: 'Tighten the retry loop and add a test.', files: ['app/retry.py', 'tests/test_retry.py'] }

function routes(extra: Handler, inbox: Json[] = [item('run:run_8f2a'), item('apr_merge1', { kind: 'merge', summary: 'example-org/example-api#9 is green' })]): Handler {
  return (m, url, body) => {
    const hit = extra(m, url, body)
    if (hit !== null) return hit
    const path = url.split('?')[0]!
    if (m === 'GET' && path === '/v1/approvals') return { status: 200, body: { approvals: inbox, tenant_id: 'eng' } }
    if (m === 'GET' && path === '/v1/approvals/run%3Arun_8f2a') return { status: 200, body: { approval: { ...item('run:run_8f2a'), projected: true }, run: { id: 'run_8f2a', state: 'PLANNED', plan: PLAN } } }
    return null
  }
}

async function mount(view: string, handler: Handler) {
  const calls = serve(handler)
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { ApprovalsScreen } = await import('../Approvals')
  const go = vi.fn()
  const utils = render(<ApprovalsScreen view={view} go={go} />)
  await screen.findByRole('heading', { name: 'Approvals', level: 1 }, WAIT)
  return { ...utils, calls, go }
}

const sent = (calls: { method: string; url: string; body: unknown }[], method: string, path: string) =>
  calls.filter((c) => c.method === method && c.url.split('?')[0] === path)

/** Stub `matchMedia`, which jsdom does not have. */
function media(phone: boolean): void {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: phone && query.includes('560'),
    media: query,
    onchange: null,
    addEventListener: () => {},
    removeEventListener: () => {},
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => false,
  }))
}

/** The item once its read has answered: the loading line is a different element. */
const loaded = () =>
  waitFor(() => {
    const d = screen.getByTestId('ap-detail')
    expect(d.tagName).toBe('SECTION')
    return d
  }, WAIT)

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

describe('the Approvals page', () => {
  it('is Automate’s second tab at /approvals, one item at /approvals/<id>, and a panel page', () => {
    const automate = SECTIONS.find((s) => s.id === 'automate')!
    expect(automate.tabs.map((t) => t.id)).toEqual(['schedules', 'approvals'])
    expect(addressToPath('automate/approvals')).toBe('/approvals')
    expect(pathToAddress('/approvals')?.address).toBe('automate/approvals')
    expect(addressToPath('automate/approvals?approval=run%3Arun_8f2a')).toBe('/approvals/run%3Arun_8f2a')
    expect(pathToAddress('/approvals/run%3Arun_8f2a')?.address).toBe('automate/approvals?approval=run%3Arun_8f2a')
    expect(PANEL_PAGES.automate.map((p) => p.key)).toEqual(['schedules', 'approvals'])
  })

  it('carries the inbox count on its panel page', async () => {
    serve((m, url) => (m === 'GET' && url.startsWith('/v1/approvals') ? { status: 200, body: { approvals: [{ approval_id: 'a1' }, { approval_id: 'a2' }, { approval_id: 'a3' }] } } : null))
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { SkyShell } = await import('../Spine')
    render(
      <SkyShell section="automate" tab="approvals" title="Approvals" go={vi.fn()} foot={null}>
        {null}
      </SkyShell>,
    )
    // MUTATION: drop `approvals: waiting` from the panel's counts.
    await waitFor(() => {
      const page = [...document.querySelectorAll('.sk-panel .sk-pk')].find((p) => p.querySelector('.sk-pl')?.textContent === 'Approvals')
      expect(page?.querySelector('.sk-cnt')?.textContent).toBe('3')
    }, WAIT)
    expect(document.querySelector('.sk-panel .sk-pk.is-on .sk-pl')?.textContent).toBe('Approvals')
  })

  it('is the Absent state when nothing waits', async () => {
    await mount('', routes(() => null, []))
    // MUTATION: draw an empty list for a measured zero.
    await screen.findByText('Nothing waiting', undefined, WAIT)
    expect(document.querySelector('.ap-list')).toBeNull()
  })

  it('lists every kind oldest first, and asks for a choice before deciding', async () => {
    await mount('', routes(() => null))
    await waitFor(() => expect(document.querySelectorAll('li[data-approval]')).toHaveLength(2), WAIT)
    const rows = [...document.querySelectorAll<HTMLElement>('li[data-approval]')]
    expect(rows.map((r) => r.dataset.approval)).toEqual(['run:run_8f2a', 'apr_merge1'])
    expect(visible(rows[1]!)).toContain('Merge')
    expect(visible(document.querySelector('.ap-pane'))).toContain('Choose an item')
  })
})

describe('one item', () => {
  it('shows the plan beside the list, approves with the digest it showed, and reads the inbox again', async () => {
    const { calls, go } = await mount(
      'approval=run%3Arun_8f2a',
      routes((m, url) =>
        m === 'POST' && url === '/v1/approvals/run%3Arun_8f2a:approve' ? { status: 200, body: { approval: { approval_id: 'run:run_8f2a', state: 'approved' } } } : null,
      ),
    )
    const detail = await loaded()
    await waitFor(() => expect(visible(detail)).toContain('app/retry.py'), WAIT)
    // Variant B: the item is read in the pane beside the list, not under its row.
    expect(detail.closest('.ap-pane')).not.toBeNull()
    expect(visible(detail)).toContain('Tighten the retry loop')
    fireEvent.click(within(detail).getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(sent(calls, 'POST', '/v1/approvals/run%3Arun_8f2a:approve')).toHaveLength(1), WAIT)
    // MUTATION: send no digest, or one the page made up, and a changed plan is approved unread.
    expect(sent(calls, 'POST', '/v1/approvals/run%3Arun_8f2a:approve')[0]!.body).toEqual({ digest: 'plan-digest-1' })
    expect(go).toHaveBeenCalledWith('automate/approvals')
    await waitFor(() => expect(sent(calls, 'GET', '/v1/approvals').length).toBe(2), WAIT)
    await screen.findByText(/Approved\. The inbox was read again\./, undefined, WAIT)
  })

  it('says the server’s rule when it refuses', async () => {
    await mount(
      'approval=run%3Arun_8f2a',
      routes((m, url) =>
        m === 'POST' && url === '/v1/approvals/run%3Arun_8f2a:approve' ? { status: 409, body: { code: 'already_decided', message: 'run run_8f2a was decided first' } } : null,
      ),
    )
    const detail = await loaded()
    await waitFor(() => expect(within(detail).getByRole('button', { name: 'Approve' })).toBeTruthy(), WAIT)
    fireEvent.click(within(detail).getByRole('button', { name: 'Approve' }))
    await screen.findByText(/Someone decided this first: run run_8f2a was decided first/, undefined, WAIT)
  })

  it('asks a one-person tenant for the words its own hold names, and sends them', async () => {
    const held = { ...item('run:run_8f2a', { kind: 'hold' }), hold: { code: 'NEEDS_SECOND_MEMBER', approvers: 'second_member', matched: ['terraform/iam.tf'] } }
    let first = true
    const { calls } = await mount(
      'approval=run%3Arun_8f2a',
      routes((m, url) => {
        if (m === 'GET' && url === '/v1/approvals/run%3Arun_8f2a') return { status: 200, body: { approval: held, run: { id: 'run_8f2a', plan: PLAN } } }
        if (m === 'POST' && url === '/v1/approvals/run%3Arun_8f2a:approve') {
          if (first) {
            first = false
            return { status: 403, body: { code: 'hold_approver_required', message: 'run run_8f2a is held NEEDS_SECOND_MEMBER', detail: { hold: 'NEEDS_SECOND_MEMBER', confirm: 'terraform/iam.tf' } } }
          }
          return { status: 200, body: { approval: { approval_id: 'run:run_8f2a', state: 'approved' } } }
        }
        return null
      }),
    )
    const detail = await loaded()
    await waitFor(() => expect(visible(detail)).toContain('Held NEEDS_SECOND_MEMBER'), WAIT)
    expect(visible(detail)).toContain('terraform/iam.tf')
    fireEvent.click(within(detail).getByRole('button', { name: 'Approve' }))
    const field = await within(detail).findByRole('textbox', undefined, WAIT)
    expect(visible(detail)).toContain('Type terraform/iam.tf')
    expect((within(detail).getByRole('button', { name: 'Approve' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(field, { target: { value: 'terraform/iam.tf' } })
    fireEvent.click(within(detail).getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(sent(calls, 'POST', '/v1/approvals/run%3Arun_8f2a:approve')).toHaveLength(2), WAIT)
    expect(sent(calls, 'POST', '/v1/approvals/run%3Arun_8f2a:approve')[1]!.body).toEqual({ digest: 'plan-digest-1', confirm: 'terraform/iam.tf' })
  })

  it('rejects only with a reason, and sends it', async () => {
    const { calls } = await mount(
      'approval=run%3Arun_8f2a',
      routes((m, url) =>
        m === 'POST' && url === '/v1/approvals/run%3Arun_8f2a:reject' ? { status: 200, body: { approval: { approval_id: 'run:run_8f2a', state: 'rejected' } } } : null,
      ),
    )
    const detail = await loaded()
    await waitFor(() => expect(within(detail).getByRole('button', { name: 'Reject…' })).toBeTruthy(), WAIT)
    fireEvent.click(within(detail).getByRole('button', { name: 'Reject…' }))
    const reject = within(detail).getByRole('button', { name: 'Reject' }) as HTMLButtonElement
    // MUTATION: allow a blank reason, which the server refuses with a 422.
    expect(reject.disabled).toBe(true)
    fireEvent.change(within(detail).getByRole('textbox'), { target: { value: 'the plan touches the wrong module' } })
    fireEvent.click(within(detail).getByRole('button', { name: 'Reject' }))
    await waitFor(() => expect(sent(calls, 'POST', '/v1/approvals/run%3Arun_8f2a:reject')).toHaveLength(1), WAIT)
    expect(sent(calls, 'POST', '/v1/approvals/run%3Arun_8f2a:reject')[0]!.body).toEqual({ reason: 'the plan touches the wrong module' })
  })
})

describe('the inbox on a phone', () => {
  it('opens the chosen item in place under its row, each row a 44px target', async () => {
    // MUTATION: draw the split pane at 390px and the item sits after the whole list.
    media(true)
    await mount('approval=run%3Arun_8f2a', routes(() => null))
    const detail = await loaded()
    expect(detail.closest('li[data-approval="run:run_8f2a"]')).not.toBeNull()
    expect(document.querySelector('.ap-pane')).toBeNull()
    const pick = document.querySelector<HTMLElement>('.ap-pick')!
    expect(painted(pick, 'min-height', PHONE)).toBe('44px')
    expect(painted(document.querySelector<HTMLElement>('.ap-detail .sc-facts')!, 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
  })
})
