// AUTOMATE › APPROVALS (docs/schedules.md §4.5, §6.1-§6.2, lane S7): the
// tenant's inbox at `/automate/approvals`.
//
// WHAT EACH CASE HOLDS:
//   * Approvals is Automate's second tab, at `/automate/approvals`, and one item is
//     `/automate/approvals/<id>`, both ways (APPROVALS-MOVE; the old addresses
//     are approvals.move.test.tsx's); the panel's Approvals page carries the
//     inbox count the section does;
//   * a measured empty inbox is the `Absent` state, not a list of nothing;
//   * a row opens the item on its own page (#2417: A4's rows open B's A5
//     decision page, which replaced the split pane); the item is read from
//     `GET /v1/approvals/{id}` and shows its plan's files and its hold before
//     the verbs;
//   * Approve sends back exactly the digest that read showed, and the inbox
//     is read again; a refusal is the server's rule, said in place;
//   * a one-person tenant's own hold asks for the words the server names, and
//     the retry sends them as `confirm`;
//   * Reject needs a reason, typed in the box beside it, and sends it;
//   * a merge draws the mockup's checklist, a mark per check -- CI green at
//     the head, the verdict, each requirement (an unmet one a ✗ row of its
//     own), the hard-stop gate -- and Approve names the head it approves and
//     stays enabled whatever the checks say: the server holds every rule
//     (owner decisions 2026-10-11);
//   * at 390px the item's page is one column (its stacked rows and 44px
//     targets are automate.approvals.test.tsx's).
//
// The bodies are `routes/approvals.py`'s shapes. Placeholders only; nothing
// is token-shaped.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { serve, visible } from './repofixture'
import type { CascadeEnv } from './cssgate'
import { painted } from './marks'
import { SECTIONS, canonical, fromAddress } from '../App'
import { addressToPath, pathToAddress } from '../paths'
import { PANEL_PAGES } from '../Spine'
import { APPROVAL_KIND } from '../ApprovalDecision'

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
  it('is Automate’s second tab at /automate/approvals, one item at /automate/approvals/<id>, and a panel page', () => {
    const automate = SECTIONS.find((s) => s.id === 'automate')!
    expect(automate.tabs.map((t) => t.id)).toEqual(['schedules', 'approvals'])
    expect(addressToPath('automate/approvals')).toBe('/automate/approvals')
    expect(pathToAddress('/automate/approvals')?.address).toBe('automate/approvals')
    expect(addressToPath('automate/approvals?approval=run%3Arun_8f2a')).toBe('/automate/approvals/run%3Arun_8f2a')
    expect(pathToAddress('/automate/approvals/run%3Arun_8f2a')?.address).toBe('automate/approvals?approval=run%3Arun_8f2a')
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
    expect(document.querySelector('table')).toBeNull()
  })

  it('QA-048: names, in its empty sentence, exactly the kinds its filter chips name', async () => {
    await mount('', routes(() => null, []))
    await screen.findByText('Nothing waiting', undefined, WAIT)
    // An empty inbox draws only the Every kind chip (visual QA automate-22,
    // 2026-10-28: a kind with nothing waiting has no chip), so the words are
    // the chips' own table, the one the filter draws from.
    expect(within(screen.getByRole('group', { name: 'Kind' })).getAllByRole('button').map((b) => visible(b))).toEqual(['Every kind · 0'])
    const chips = Object.values(APPROVAL_KIND)
    expect(chips.length).toBeGreaterThan(3)
    const said = visible(document.body)
    // MUTATION: put back "No firing, plan, merge, proposal or spec ..." -- a
    // word no chip carries, and three chips the sentence leaves out.
    expect(said).not.toMatch(/firing/i)
    for (const c of chips) expect(said, `the empty sentence leaves out "${c}"`).toContain(c.toLowerCase())
    // MUTATION: join the list with commas only ("spec, answer is waiting").
    expect(said).toMatch(/, spec or answer in this workspace is waiting for a person/)
  })

  it('lists every kind oldest first, each row opening the item’s own page', async () => {
    await mount('', routes(() => null))
    await waitFor(() => expect(document.querySelectorAll('tr[data-approval]')).toHaveLength(2), WAIT)
    const rows = [...document.querySelectorAll<HTMLElement>('tr[data-approval]')]
    expect(rows.map((r) => r.dataset.approval)).toEqual(['run:run_8f2a', 'apr_merge1'])
    expect(visible(rows[1]!)).toContain('Merge')
    // #2417: no pane beside the list; the row is a link to the item's page.
    expect(document.querySelector('[data-testid="ap-detail"]')).toBeNull()
    expect(rows[1]!.querySelector('a')!.getAttribute('href')).toBe('/automate/approvals/apr_merge1')
  })
})

describe('one item', () => {
  it('shows the plan on the item’s page, approves with the digest it showed, and goes back to the inbox', async () => {
    const { calls, go } = await mount(
      'approval=run%3Arun_8f2a',
      routes((m, url) =>
        m === 'POST' && url === '/v1/approvals/run%3Arun_8f2a:approve' ? { status: 200, body: { approval: { approval_id: 'run:run_8f2a', state: 'approved' } } } : null,
      ),
    )
    const detail = await loaded()
    await waitFor(() => expect(visible(detail)).toContain('app/retry.py'), WAIT)
    // #2417 A5: the item is its own page, with no list beside it.
    expect(document.querySelector('.apl-card')).toBeNull()
    expect(visible(detail)).toContain('Tighten the retry loop')
    fireEvent.click(within(detail).getByRole('button', { name: 'Approve' }))
    await waitFor(() => expect(sent(calls, 'POST', '/v1/approvals/run%3Arun_8f2a:approve')).toHaveLength(1), WAIT)
    // MUTATION: send no digest, or one the page made up, and a changed plan is approved unread.
    expect(sent(calls, 'POST', '/v1/approvals/run%3Arun_8f2a:approve')[0]!.body).toEqual({ digest: 'plan-digest-1' })
    expect(go).toHaveBeenCalledWith('automate/approvals')
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

  it('says who may decide when the last editor is refused, not "Not decided" (#2259)', async () => {
    const message = "the member who last changed this schedule's gate, scope or spec cannot approve its merge-tier items; another of the schedule's named approvers must: dave@example.com"
    await mount(
      'approval=run%3Arun_8f2a',
      routes((m, url) =>
        m === 'POST' && url === '/v1/approvals/run%3Arun_8f2a:approve' ? { status: 403, body: { code: 'second_person_required', message } } : null,
      ),
    )
    const detail = await loaded()
    await waitFor(() => expect(within(detail).getByRole('button', { name: 'Approve' })).toBeTruthy(), WAIT)
    fireEvent.click(within(detail).getByRole('button', { name: 'Approve' }))
    const said = await screen.findByText(/named approvers must: dave@example\.com/, undefined, WAIT)
    // Rendered 2026-10-25: a prefix restating the server's own first clause
    // read "You last changed this schedule’s gate, scope or spec: the member
    // who last changed this schedule's gate, scope or spec cannot ...".
    expect(said.textContent).toBe(`Not decided: ${message}`)
  })

  it('says an owner-only item is the owner’s even as its last editor (#2259)', async () => {
    const owner = { ...item('run:run_8f2a'), approvers: 'owner_only', projected: true }
    await mount(
      'approval=run%3Arun_8f2a',
      routes((m, url) =>
        m === 'GET' && url.split('?')[0] === '/v1/approvals/run%3Arun_8f2a' ? { status: 200, body: { approval: owner, run: { id: 'run_8f2a', state: 'PLANNED', plan: PLAN } } } : null,
      ),
    )
    const detail = await loaded()
    await within(detail).findByText(
      'the platform owner alone, who may decide even as the schedule’s last editor; that decision is recorded in the audit as an owner waiver',
      undefined,
      WAIT,
    )
  })

  it('says an owner-only hold’s approver once, not twice and not "only by ... only" (#2259)', async () => {
    const held = { ...item('run:run_8f2a', { kind: 'hold' }), approvers: 'owner_only', hold: { code: 'NEEDS_OWNER', approvers: 'owner_only', matched: ['.github/workflows'] } }
    await mount(
      'approval=run%3Arun_8f2a',
      routes((m, url) =>
        m === 'GET' && url.split('?')[0] === '/v1/approvals/run%3Arun_8f2a' ? { status: 200, body: { approval: held, run: { id: 'run_8f2a', state: 'PLANNED', plan: PLAN } } } : null,
      ),
    )
    const detail = await loaded()
    const note = await within(detail).findByRole('note', undefined, WAIT)
    // Rendered 2026-10-25: "Who may approve" said the owner-only sentence and
    // the hold's note said it again as "Approved only by the platform owner
    // only, ...". Said once, in "Who may approve".
    expect(detail.textContent?.split('the platform owner alone, who may decide').length).toBe(2)
    expect(note.textContent).not.toMatch(/only by|owner only/)
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
    // Visual QA automate2-05: the hold leads with its words, not its code.
    await waitFor(() => expect(visible(detail)).toContain('Held for a second member.'), WAIT)
    expect(visible(detail)).toContain('terraform/iam.tf')
    fireEvent.click(within(detail).getByRole('button', { name: 'Approve' }))
    const field = await within(detail).findByRole('textbox', { name: /approve it yourself/ }, WAIT)
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
    // #2417 A5: the reason box sits beside Reject, which is one click, never "Reject…".
    const reject = (await within(detail).findByRole('button', { name: 'Reject' }, WAIT)) as HTMLButtonElement
    // MUTATION: allow a blank reason, which the server refuses with a 422.
    expect(reject.disabled).toBe(true)
    fireEvent.change(within(detail).getByRole('textbox', { name: /Reason/ }), { target: { value: 'the plan touches the wrong module' } })
    fireEvent.click(within(detail).getByRole('button', { name: 'Reject' }))
    await waitFor(() => expect(sent(calls, 'POST', '/v1/approvals/run%3Arun_8f2a:reject')).toHaveLength(1), WAIT)
    expect(sent(calls, 'POST', '/v1/approvals/run%3Arun_8f2a:reject')[0]!.body).toEqual({ reason: 'the plan touches the wrong module' })
  })

  it('puts focus on what the decision said once Reject answers (QA1 automate-15)', async () => {
    await mount(
      'approval=run%3Arun_8f2a',
      routes((m, url) =>
        m === 'POST' && url === '/v1/approvals/run%3Arun_8f2a:reject' ? { status: 200, body: { approval: { approval_id: 'run:run_8f2a', state: 'rejected' } } } : null,
      ),
    )
    const detail = await loaded()
    await within(detail).findByRole('button', { name: 'Reject' }, WAIT)
    fireEvent.change(within(detail).getByRole('textbox', { name: /Reason/ }), { target: { value: 'the plan touches the wrong module' } })
    const reject = within(detail).getByRole('button', { name: 'Reject' })
    reject.focus()
    fireEvent.click(reject)
    const said = await screen.findByText(/Rejected\. The inbox was read again\./, undefined, WAIT)
    // MUTATION: drop the focus effect in Approvals.tsx and focus falls to <body> as Reject turns disabled.
    await waitFor(() => expect(document.activeElement).toBe(said), WAIT)
    expect(said.getAttribute('tabindex')).toBe('-1')
    expect(said.getAttribute('role')).toBe('status')
  })
})

// A head built at runtime: a 40-character hex literal is not something this
// file needs to carry.
const HEAD = '4c1e9a0' + 'd'.repeat(33)
const OLD = '9b0e771' + 'e'.repeat(33)

function mergeRoutes(merge: Json, extra: Handler = () => null, digest: unknown = { head_sha: HEAD, verdict: 'MERGE' }): Handler {
  const approval = item('apr_merge1', { kind: 'merge', summary: 'example-org/example-api#207 is green', digest })
  return routes((m, url, body) => {
    const hit = extra(m, url, body)
    if (hit !== null) return hit
    if (m === 'GET' && url === '/v1/approvals/apr_merge1') return { status: 200, body: { approval, merge: { pull_request: { number: 144, url: 'https://github.com/example-org/example-api/pull/144' }, ...merge } } }
    return null
  })
}

const checks = (detail: HTMLElement) =>
  [...detail.querySelectorAll<HTMLElement>('.ap-checks > li')].map((li) => ({
    key: li.dataset.check,
    mark: li.dataset.mark,
    said: li.querySelector('.ap-mark')?.getAttribute('aria-label'),
    glyph: li.querySelector('.ap-mark')?.textContent,
    text: visible(li),
  }))

describe('a merge', () => {
  it('draws a ✓ row per check that holds, and names the head on Approve', async () => {
    const { calls } = await mount(
      'approval=apr_merge1',
      mergeRoutes(
        { green_sha: HEAD, requirements_met: true, requirements_unmet: [], files_checked_head: HEAD, approval_hold: null },
        (m, url) => (m === 'POST' && url === '/v1/approvals/apr_merge1:approve' ? { status: 200, body: { approval: { approval_id: 'apr_merge1', state: 'approved' } } } : null),
      ),
    )
    const detail = await loaded()
    await waitFor(() => expect(detail.querySelectorAll('.ap-checks > li').length).toBeGreaterThan(0), WAIT)
    // MUTATION: drop a row, or draw a met check without its mark.
    expect(checks(detail).map((c) => [c.key, c.mark, c.glyph])).toEqual([
      ['ci', 'met', '✓'],
      ['review', 'met', '✓'],
      ['requirements', 'met', '✓'],
      ['gate', 'met', '✓'],
    ])
    expect(checks(detail)[0]!.text).toContain('Required checks green at the head')
    expect(checks(detail)[0]!.text).toContain('4c1e9a0')
    expect(checks(detail)[3]!.text).toContain('No protected path changed')
    // MUTATION: a plain "Approve", and the reader cannot see which head they approve.
    const approve = within(detail).getByRole('button', { name: 'Approve merge at 4c1e9a0' })
    fireEvent.click(approve)
    await waitFor(() => expect(sent(calls, 'POST', '/v1/approvals/apr_merge1:approve')).toHaveLength(1), WAIT)
    expect(sent(calls, 'POST', '/v1/approvals/apr_merge1:approve')[0]!.body).toEqual({ digest: { head_sha: HEAD, verdict: 'MERGE' } })
  })

  it('draws each unmet requirement, a red CI and a matched hard stop as ✗ rows, and leaves Approve enabled', async () => {
    const { calls } = await mount(
      'approval=apr_merge1',
      mergeRoutes(
        {
          green_sha: null,
          requirements_met: false,
          requirements_unmet: ['review MERGE', 'Closes #207 in the body'],
          files_checked_head: HEAD,
          approval_hold: { code: 'NEEDS_SECOND_MEMBER', approvers: 'second_member', reasons: ['iam'], matched: ['terraform/iam.tf'] },
        },
        (m, url) => (m === 'POST' && url === '/v1/approvals/apr_merge1:approve' ? { status: 409, body: { code: 'merge_changed', message: 'run run_8f2a’s pull request is no longer at 4c1e9a0' } } : null),
      ),
    )
    const detail = await loaded()
    await waitFor(() => expect(detail.querySelectorAll('.ap-checks > li').length).toBeGreaterThan(0), WAIT)
    // MUTATION: join the unmet requirements into one line of plain text.
    expect(checks(detail).map((c) => [c.key, c.mark, c.glyph])).toEqual([
      ['ci', 'unmet', '✗'],
      ['review', 'met', '✓'],
      ['requirement-0', 'unmet', '✗'],
      ['requirement-1', 'unmet', '✗'],
      ['gate', 'unmet', '✗'],
    ])
    expect(checks(detail)[2]!.text).toContain('Requirement unmet: review MERGE')
    expect(checks(detail)[3]!.text).toContain('Requirement unmet: Closes #207 in the body')
    expect(checks(detail)[4]!.text).toContain('held for a second member')
    expect(checks(detail)[4]!.text).toContain('terraform/iam.tf')
    expect(visible(detail)).not.toContain('unmet: review MERGE, Closes')
    // MUTATION: disable Approve on an unmet check. The owner chose to keep it
    // enabled (2026-10-11): the server holds every rule, and says its refusal.
    const approve = within(detail).getByRole('button', { name: 'Approve merge at 4c1e9a0' }) as HTMLButtonElement
    expect(approve.disabled).toBe(false)
    expect(approve.getAttribute('aria-disabled')).not.toBe('true')
    fireEvent.click(approve)
    await waitFor(() => expect(sent(calls, 'POST', '/v1/approvals/apr_merge1:approve')).toHaveLength(1), WAIT)
    await within(detail).findByRole('alert', undefined, WAIT)
  })

  it('says a check it has not read as not read, not as met, and an older green head as unmet', async () => {
    await mount('approval=apr_merge1', mergeRoutes({ green_sha: OLD, requirements_unmet: [], files_checked_head: null }, () => null, `${HEAD}:MERGE`))
    const detail = await loaded()
    await waitFor(() => expect(detail.querySelectorAll('.ap-checks > li').length).toBeGreaterThan(0), WAIT)
    // MUTATION: read a missing `requirements_met` or `files_checked_head` as met.
    expect(checks(detail).map((c) => [c.key, c.mark, c.glyph])).toEqual([
      ['ci', 'unmet', '✗'],
      ['review', 'met', '✓'],
      ['requirements', 'unread', '–'],
      ['gate', 'unread', '–'],
    ])
    // The mark is said to a screen reader too, not only drawn.
    expect(checks(detail).map((c) => c.said)).toEqual(['unmet', 'met', 'not read', 'not read'])
    expect(checks(detail)[0]!.text).toContain('9b0e771')
    // The digest sent as `<head_sha>:<verdict>` still names its head.
    expect(within(detail).getByRole('button', { name: 'Approve merge at 4c1e9a0' })).toBeTruthy()
  })

  it('paints a met mark in the ok ink and an unmet one in the bad ink', async () => {
    await mount('approval=apr_merge1', mergeRoutes({ green_sha: HEAD, requirements_met: false, requirements_unmet: ['review MERGE'], files_checked_head: HEAD }))
    const detail = await loaded()
    await waitFor(() => expect(detail.querySelectorAll('.ap-checks > li').length).toBeGreaterThan(0), WAIT)
    const mark = (key: string) => detail.querySelector<HTMLElement>(`li[data-check="${key}"] > .ap-mark`)!
    // MUTATION: leave the marks the text colour, and ✗ reads as plain text again.
    expect(painted(mark('ci'), 'color', { width: 1440 })).toContain('--ok-ink')
    expect(painted(mark('requirement-0'), 'color', { width: 1440 })).toContain('--bad-ink')
  })

  it('leaves a plan’s button as plain Approve', async () => {
    await mount('approval=run%3Arun_8f2a', routes(() => null))
    const detail = await loaded()
    await waitFor(() => expect(within(detail).getByRole('button', { name: 'Approve' })).toBeTruthy(), WAIT)
    expect(within(detail).queryByRole('button', { name: /Approve merge/ })).toBeNull()
  })
})

describe('the inbox on a phone', () => {
  it('opens the item on its own page, its facts and evidence one column', async () => {
    // MUTATION: keep two columns at 390px, and the page scrolls sideways.
    media(true)
    await mount('approval=run%3Arun_8f2a', routes(() => null))
    const detail = await loaded()
    expect(detail.closest('tr')).toBeNull()
    expect(painted(detail.querySelector<HTMLElement>('.ad-qfacts')!, 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
    expect(painted(detail.querySelector<HTMLElement>('.ad-ev')!, 'grid-template-columns', PHONE)).toBe('minmax(0, 1fr)')
  })
})

// LIVE QA, AUTOMATE (2026-10-11): a link to one item, and the kind filter.
describe('live QA: automate, approvals', () => {
  it('reads an item the inbox does not hold and says it was not found, with the way back to the list (AUT-6)', async () => {
    const { calls } = await mount('approval=nope-123', routes(() => null, []))
    // MUTATION: let the Screen's error stand in for the page's sentence.
    await waitFor(() => expect(sent(calls, 'GET', '/v1/approvals/nope-123')).toHaveLength(1), WAIT)
    await screen.findByText(/No approval nope-123 in this workspace/, undefined, WAIT)
    expect(screen.getByRole('link', { name: '‹ Approvals' }).getAttribute('href')).toBe('/automate/approvals')
  })

  it('shows what became of a linked item someone else decided, or that expired (AUT-6)', async () => {
    const decided = (state: string, over: Json = {}) => ({
      status: 200,
      body: { approval: item('apr_old', { kind: 'merge', state, decided_by: 'teammate@example.com', decided_at: hoursFromNow(-1), ...over }) },
    })
    for (const [state, words] of [
      ['rejected', /Rejected by teammate@example\.com.*not now.*Nothing is left to decide\./],
      ['expired', /Expired .*: nobody decided it in time/],
      ['approved', /Approved by teammate@example.com/],
    ] as const) {
      const view = await mount(
        'approval=apr_old',
        routes((m, url) => (m === 'GET' && url === '/v1/approvals/apr_old' ? decided(state, state === 'rejected' ? { reason: 'not now' } : {}) : null), []),
      )
      const said = await screen.findByTestId('ap-decided', undefined, WAIT)
      expect(said.getAttribute('data-state')).toBe(state)
      expect(visible(said)).toMatch(words)
      expect(screen.queryByRole('button', { name: /Approve/ })).toBeNull()
      expect(screen.getByTestId('ap-state').textContent).toBe(state)
      view.unmount()
    }
  })

  it('opens a listed item once, on its own page (AUT-6 control)', async () => {
    await mount('approval=run%3Arun_8f2a', routes(() => null))
    await screen.findByTestId('ap-detail', undefined, WAIT)
    expect(screen.queryByTestId('ap-linked')).toBeNull()
  })

  it('filters by kind through the route, and keeps the kind in the address (AUT-15)', async () => {
    expect(addressToPath('automate/approvals?kind=merge')).toBe('/automate/approvals?kind=merge')
    expect(pathToAddress('/automate/approvals', '?kind=merge')?.address).toBe('automate/approvals?kind=merge')
    expect(addressToPath('automate/approvals?approval=apr_1&kind=merge')).toBe('/automate/approvals/apr_1?kind=merge')
    expect(pathToAddress('/automate/approvals/apr_1', '?kind=merge')?.address).toBe('automate/approvals?approval=apr_1&kind=merge')
    // MUTATION: leave `kind` out of keptKeys and the filter is dropped from the URL.
    expect(canonical(fromAddress('automate/approvals?kind=merge'))).toBe('automate/approvals?kind=merge')
    const { calls, go } = await mount('kind=merge', routes(() => null))
    // The list asks the inbox and its history (`state=all`, QA2-FIX-07 automate2-01).
    await waitFor(() => expect(calls.some((c) => c.url === '/v1/approvals?kind=merge&state=all')).toBe(true), WAIT)
    const group = screen.getByRole('group', { name: 'Kind' })
    // The chosen chip carries its count ("Merge · 2", #2417 A4).
    expect(within(group).getByRole('button', { name: /^Merge/ }).getAttribute('aria-pressed')).toBe('true')
    // The other chips keep their counts from the whole inbox (automate-05).
    fireEvent.click(within(group).getByRole('button', { name: 'Plan · 1' }))
    expect(go).toHaveBeenLastCalledWith('automate/approvals?kind=plan')
    fireEvent.click(within(group).getByRole('button', { name: 'Every kind · 2' }))
    expect(go).toHaveBeenLastCalledWith('automate/approvals')
    // An item opened from a filtered list keeps the filter.
    await waitFor(() => expect(document.querySelectorAll('tr[data-approval]').length).toBeGreaterThan(0), WAIT)
    expect(document.querySelector('tr[data-approval="apr_merge1"] a')!.getAttribute('href')).toBe('/automate/approvals/apr_merge1?kind=merge')
  })

  it('asks for the whole inbox when no kind is named', async () => {
    const { calls } = await mount('', routes(() => null))
    await waitFor(() => expect(calls.some((c) => c.url === '/v1/approvals?state=all')).toBe(true), WAIT)
    expect(calls.some((c) => c.url.startsWith('/v1/approvals?kind'))).toBe(false)
  })
})
