/**
 * #587: AN OVERLAP SAYS WHETHER ANYONE MUST ACT ON IT, AS A FIELD.
 *
 * run_7a37942a19aa4d2c80d1's plan carried five overlaps whose notes each said,
 * somewhere after their first sentence, that nothing needed doing. The card
 * read the verdict only off a note that OPENED with "No action", so it drew
 * all five amber and its heading said "5 checked · 5 need action". Owner
 * decision, 2026-10-05: add a field, not better guessing. `PlanOverlap.action`
 * is `none` or `required`; a plan stored before it is read off its note, where
 * a "No action" sentence counts ANYWHERE (start, or after a sentence end).
 *
 * WORDING (owner, 2026-10-05): `none` reads "Not in this run" in neutral grey
 * with the planner's note under it; `required` keeps amber "Needs action"; the
 * heading reads "N checked · none need action" when none do.
 */
import { render, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { overlapVerdict } from '../Runs'

const WAIT = { timeout: 4000 }
const JSON_HEADERS = { 'content-type': 'application/json' }
const visible = (el: Element | null) => (el?.textContent ?? '').replace(/\s+/g, ' ').trim()
// Built at runtime: a 64-hex literal reads as a key to a credential scan.
const DIGEST = 'sha256:' + 'cd'.repeat(32)
const REPO = 'bogdan-alexandrescu/SwarmCloud'
const RUN_ID = 'run_7a37942a19aa4d2c80d1'

/**
 * run_7a37942a19aa4d2c80d1's five stored overlap notes, verbatim, read
 * 2026-10-05: ref, kind and note as stored before #587, no `action`.
 */
const RUN_7A37_OVERLAPS = [
  { ref: "bogdan-alexandrescu/SwarmCloud#185", kind: "issue", note: "The Timeline outcome ledger reads GET /v1/outcomes, which has its own coverage section. It is a different route from /v1/attempts, and this plan touches none of its files. No action. #185's builder can reuse the not_recorded wording this plan settles." },
  { ref: "bogdan-alexandrescu/SwarmCloud#322", kind: "issue", note: "Same spend-honesty family: the Tokens tile leaves out cached tokens. It is UI code (AgentDetail), not the attempts route or these docs. No action." },
  { ref: "bogdan-alexandrescu/SwarmCloud#323", kind: "issue", note: "Prompt-cache cost of claude-code steps. It is about spend, but it is worker and runner configuration, with no shared files. No action." },
  { ref: "bogdan-alexandrescu/SwarmCloud#503", kind: "issue", note: "The UI-vs-mock-ups epic may edit docs/web-ui/ui-audit-and-build-prompt.md for other screens. No open PR touches that file now. This plan's edits there are limited to §A0.1's coverage sentence. If a #503 lane opens a PR on the file first, rebase onto it rather than editing in parallel." },
  { ref: "bogdan-alexandrescu/SwarmCloud#177", kind: "issue", note: "This epic collects comments that contradict the code, and the stale record_spend passages are in that family. If the epic has a box for them, the PR comment (not the body) should name this PR as its evidence." },
]

function run(overlaps: unknown[], state = 'PLANNED') {
  return {
    id: RUN_ID, tenant_id: 'eng', state, terminal: ['DONE', 'FAILED', 'REJECTED', 'CANCELLED'].includes(state),
    issue: { ref: `${REPO}#454`, owner: 'bogdan-alexandrescu', repo: 'SwarmCloud', number: 454,
      url: `https://github.com/${REPO}/issues/454`, repository_url: `https://github.com/${REPO}` },
    plan_approval: 'required', auto_merge: false, fix_rounds: 3, planner_task_id: 'task_7f3e2c9a1b8d4e6f0a5c',
    plan: {
      summary: 'Draw each overlap from its action.',
      steps: [{ step_id: 'impl', title: 'Draw the overlaps', prompt: 'Edit Runs.tsx.' }],
      overlaps,
    },
    plan_digest: DIGEST, plan_revision: 1, plan_edited_by: null, workflow_id: null,
    created_by: 'operator@example.com', created_at: '2026-10-05T09:00:00Z', updated_at: '2026-10-05T09:05:00Z',
    approved_by: null, approved_at: null, approved_digest: null, rejected_by: null, rejection_reason: null,
    error: null,
    history: [{ at: '2026-10-05T09:00:00Z', from: null, to: 'PLANNING', by: 'operator@example.com' }],
    issue_read: null,
    issue_read_error: null,
  }
}

async function mount(served: unknown) {
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url === `/v1/runs/${RUN_ID}`) return new Response(JSON.stringify({ run: served }), { status: 200, headers: JSON_HEADERS })
    return new Response(JSON.stringify({ code: 'not_found', message: url }), { status: 404, headers: JSON_HEADERS })
  }) as unknown as typeof fetch
  vi.stubEnv('VITE_LIVE', '1')
  vi.resetModules()
  const { RunsScreen } = await import('../Runs')
  const utils = render(<RunsScreen view={`run=${RUN_ID}`} go={vi.fn()} />)
  await waitFor(() => expect(utils.container.querySelector('.rn-overlaps')).not.toBeNull(), WAIT)
  return utils.container.querySelector('.rn-overlaps')!
}

const heading = (card: Element) => visible(card.querySelector('h3, h2, .c-card-title'))

afterEach(() => {
  vi.unstubAllEnvs()
})

describe('#587: run_7a37942a19aa4d2c80d1\'s five stored notes, on the run as it ended and as it was planned', () => {
  it('draws them on a DONE run as 0 of 5 needing action, all "Not in this run", each with its note', async () => {
    const card = await mount(run(RUN_7A37_OVERLAPS, 'DONE'))
    expect(card.classList.contains('is-found')).toBe(false)
    expect(heading(card)).toContain('5 checked · none need action')
    const rows = card.querySelectorAll('details.rn-overlap')
    expect(rows.length).toBe(5)
    rows.forEach((row, i) => {
      expect(row.classList.contains('is-needs')).toBe(false)
      expect(visible(row.querySelector('.rn-overlap-v'))).toBe('Not in this run')
      expect(visible(row.querySelector('.rn-overlap-note'))).toBe(RUN_7A37_OVERLAPS[i]!.note)
    })
  })

  it('draws the same five on a PLANNED run with exactly #503 and #177 needing action', async () => {
    const card = await mount(run(RUN_7A37_OVERLAPS, 'PLANNED'))
    expect(card.classList.contains('is-found')).toBe(true)
    expect(heading(card)).toContain('5 checked · 2 need action')
    const rows = Array.from(card.querySelectorAll('details.rn-overlap'))
    expect(rows.length).toBe(5)
    rows.forEach((row, i) => {
      const o = RUN_7A37_OVERLAPS[i]!
      const needs = o.ref.endsWith('#503') || o.ref.endsWith('#177')
      expect(row.classList.contains('is-needs'), o.ref).toBe(needs)
      expect(visible(row.querySelector('.rn-overlap-v')), o.ref).toBe(needs ? 'Needs action' : 'Not in this run')
    })
  })

  it('counts the five one at a time: ended run none, running run the note rule', () => {
    for (const o of RUN_7A37_OVERLAPS) expect(overlapVerdict(o, true).needs, o.note).toBe(false)
    expect(RUN_7A37_OVERLAPS.filter((o) => overlapVerdict(o, false).needs).map((o) => o.ref.split('#')[1])).toEqual(['503', '177'])
  })
})

describe('#587: the action field decides, when the plan has it', () => {
  it('draws action=required amber, counted in the heading', async () => {
    const card = await mount(run([
      { ref: `${REPO}#540`, kind: 'pull_request', action: 'required', note: 'Edits the same route; rebase onto it before step impl.' },
      { ref: `${REPO}#541`, kind: 'issue', action: 'none', note: 'Touches another screen.' },
    ]))
    expect(card.classList.contains('is-found')).toBe(true)
    expect(heading(card)).toContain('2 checked · 1 needs action')
    const [needs, none] = Array.from(card.querySelectorAll('details.rn-overlap'))
    expect(needs!.classList.contains('is-needs')).toBe(true)
    expect(visible(needs!.querySelector('.rn-overlap-v'))).toBe('Needs action')
    expect(none!.classList.contains('is-needs')).toBe(false)
    expect(visible(none!.querySelector('.rn-overlap-v'))).toBe('Not in this run')
    expect(visible(none!.querySelector('.rn-overlap-note'))).toBe('Touches another screen.')
  })

  it('shows action=required amber even on a DONE run', async () => {
    const card = await mount(run([
      { ref: `${REPO}#540`, kind: 'pull_request', action: 'required', note: 'Edits the same route.' },
    ], 'DONE'))
    expect(heading(card)).toContain('1 checked · 1 needs action')
    expect(card.querySelector('details.rn-overlap')!.classList.contains('is-needs')).toBe(true)
    expect(overlapVerdict({ note: 'x', action: 'required' }, true).needs).toBe(true)
  })

  it('believes the field over the note, either way', () => {
    expect(overlapVerdict({ note: 'No action: different file.', action: 'required' }).needs).toBe(true)
    expect(overlapVerdict({ note: 'Rebase onto it.', action: 'none' }).needs).toBe(false)
    expect(overlapVerdict({ note: 'Rebase onto it.', action: 'none' }).why).toBe('Rebase onto it.')
  })
})

describe('#587: a legacy note with no "No action" sentence still needs action', () => {
  it('draws it amber', async () => {
    const card = await mount(run([
      { ref: `${REPO}#540`, kind: 'pull_request', note: 'PR #540 edits the same route. Rebase onto it before step impl.' },
    ]))
    expect(card.classList.contains('is-found')).toBe(true)
    expect(heading(card)).toContain('1 checked · 1 needs action')
    const row = card.querySelector('details.rn-overlap')!
    expect(row.classList.contains('is-needs')).toBe(true)
    expect(visible(row.querySelector('.rn-overlap-v'))).toBe('Needs action')
  })

  it('finds "no action" only at the start or after a sentence end', () => {
    expect(overlapVerdict({ note: 'Same file. No action: different hunk.' }).needs).toBe(false)
    expect(overlapVerdict({ note: 'Same file!  no action needed.' }).needs).toBe(false)
    expect(overlapVerdict({ note: 'Same file.\nNo action.' }).needs).toBe(false)
    expect(overlapVerdict({ note: 'Taking no action here would break step impl.' }).needs).toBe(true)
    expect(overlapVerdict({ note: 'Same file; rebase first, no action is not an option.' }).needs).toBe(true)
    expect(overlapVerdict({ note: 'Same file, rebase first.' }).needs).toBe(true)
    expect(overlapVerdict({ note: undefined as unknown as string }).needs).toBe(true)
  })
})
