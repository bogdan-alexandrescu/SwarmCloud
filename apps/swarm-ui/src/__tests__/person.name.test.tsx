// A PERSON'S NAME, ALWAYS WITH THEIR EMAIL (docs/design/user-profile.md §4.4,
// Q5; build-plan lane PROFILE-NAMES-UI, part of #1505).
//
// WHAT EACH CASE HOLDS:
//   * `PersonName` prints `Name · email`, and the email alone when the name
//     is null: the name is a label the person chose, never the identity, so
//     the verified address is always beside it;
//   * a 64-character name (the longest `display_names.check` admits) in a
//     narrow box leaves the whole email in the DOM, and the name is the part
//     that is cut -- it shrinks and ends in an ellipsis while the email does
//     not shrink;
//   * the four places the design names print a person through `PersonName`:
//     Approvals' `decided_by`, People's row, the workflow page's owner and
//     the task page's submitter. Each is found by the component's marker,
//     `data-person-name`, so a caller that goes back to printing the email by
//     hand fails here;
//   * People prints the FULL name and the other three the FIRST name (owner,
//     2026-10-19, PROFILE-FIRSTNAME; user-profile.md §7.1 amendment), each
//     with the whole name in the marker's title.
//
// The bodies are the API's shapes with the `*_name` siblings
// PROFILE-NAMES-API added (`people.add_names`). Placeholders only.

import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import type { AgentRun, WorkflowBoard } from '../api'
import type { Result } from '../fetch'
import type { Workflow } from '../types'
import { serve } from './repofixture'
import { at, attempt, ev, task } from './runfixture'
import { ownFromBoard } from './workflowsById'

const api = vi.hoisted(() => ({
  loadCheckpoints: vi.fn(),
  loadTaskLogs: vi.fn(),
  loadWorkflowBoard: vi.fn(),
  loadWorkflowUsage: vi.fn(),
  loadWorkflow: vi.fn(),
  loadAttempts: vi.fn(),
}))
vi.mock('../api', async (importOriginal) => {
  const real = await importOriginal<typeof import('../api')>()
  return { ...real, ...api }
})

const { PersonName } = await import('../PersonName')

const WAIT = { timeout: 4000 }
const NAME = 'Ada Lovelace'
/** `NAME`'s first word: what every page but Admin › People and Profile prints. */
const FIRST = 'Ada'
const EMAIL = 'ada@example.com'
/** 64 characters: `display_names.check`'s ceiling, all letters and spaces. */
const LONG_NAME = `${'Augusta '.repeat(8)}`.slice(0, 63) + 'x'

afterEach(() => {
  vi.unstubAllEnvs()
  vi.unstubAllGlobals()
})

const marker = (root: ParentNode) => root.querySelector<HTMLElement>('[data-person-name]')

/**
 * Assert `root` prints `email` (and `name`, when given) through `PersonName`,
 * with the whole saved name `full` in the marker's title.
 */
function printsThrough(root: ParentNode, email: string, name: string | null, where: string, full: string | null = name): void {
  const el = marker(root)
  expect(el, `${where} prints the person by hand, not through PersonName`).not.toBeNull()
  expect(el!.getAttribute('data-email')).toBe(email)
  expect(el!.textContent, `${where} prints the wrong form of the name`).toBe(name === null ? email : `${name} · ${email}`)
  expect(el!.getAttribute('title')).toBe(full === null ? email : `${full} · ${email}`)
}

describe('PersonName', () => {
  it('prints `First · email` by default, and `Full Name · email` as `full`', () => {
    const { container, unmount } = render(<PersonName email={EMAIL} name={NAME} />)
    expect(marker(container)!.textContent).toBe(`${FIRST} · ${EMAIL}`)
    expect(container.querySelector('.person-name-name')!.textContent).toBe(FIRST)
    expect(container.querySelector('.person-name-email')!.textContent).toBe(EMAIL)
    unmount()
    const full = render(<PersonName email={EMAIL} name={NAME} variant="full" />).container
    expect(marker(full)!.textContent).toBe(`${NAME} · ${EMAIL}`)
    expect(full.querySelector('.person-name-name')!.textContent).toBe(NAME)
    expect(full.querySelector('.person-name-email')!.textContent).toBe(EMAIL)
  })

  it('prints the email alone when the name is null, absent or blank', () => {
    for (const name of [null, undefined, '', '   ']) {
      const { container, unmount } = render(<PersonName email={EMAIL} name={name} />)
      expect(marker(container)!.textContent, `name=${JSON.stringify(name)}`).toBe(EMAIL)
      expect(container.querySelector('.person-name-name')).toBeNull()
      unmount()
    }
  })

  it('with a 64-character name in a narrow box, keeps the whole email and cuts the name', () => {
    // MUTATION: give the email `flex-shrink: 1` or `overflow: hidden`, or take
    // the name's `min-width: 0` / ellipsis away -- the email is then the part
    // that gives way.
    expect(LONG_NAME).toHaveLength(64)
    const { container } = render(
      <div style={{ width: '120px', overflow: 'hidden' }}>
        <PersonName email={EMAIL} name={LONG_NAME} variant="full" />
      </div>,
    )
    const box = marker(container)!
    const name = box.querySelector<HTMLElement>('.person-name-name')!
    const email = box.querySelector<HTMLElement>('.person-name-email')!
    // The full email is in the DOM, not an abbreviation of it.
    expect(email.textContent).toBe(EMAIL)
    expect(box.textContent).toContain(EMAIL)
    expect(box.getAttribute('title')).toBe(`${LONG_NAME} · ${EMAIL}`)
    // The box fits its container, so something inside it must give way...
    expect(box.style.display).toBe('inline-flex')
    expect(box.style.maxWidth).toBe('100%')
    expect(['0', '0px']).toContain(box.style.minWidth)
    // ...and it is the name: it shrinks, to nothing if it must, and says so.
    expect(name.style.flexShrink).toBe('1')
    expect(['0', '0px']).toContain(name.style.minWidth)
    expect(name.style.overflow).toBe('hidden')
    expect(name.style.textOverflow).toBe('ellipsis')
    expect(name.style.whiteSpace).toBe('nowrap')
    // The email never shrinks and is never clipped.
    expect(email.style.flexShrink).toBe('0')
    expect(email.style.overflow).toBe('')
    expect(email.style.textOverflow).toBe('')
    expect(email.style.whiteSpace).toBe('nowrap')
  })
})

describe('every place the design names prints a person through PersonName', () => {
  it("Approvals' decided_by", async () => {
    serve((m, url) =>
      m === 'GET' && url.split('?')[0] === '/v1/approvals/apr_old'
        ? {
            status: 200,
            body: {
              approval: {
                approval_id: 'apr_old', tenant_id: 'eng', kind: 'merge', subject: {}, digest: 'd1',
                summary: 'example-org/example-api#9 is green', approvers: 'members', state: 'approved',
                requested_at: at(-60), expires_at: at(600), decided_at: at(-5),
                decided_by: EMAIL, decided_by_name: NAME,
              },
            },
          }
        : m === 'GET' && url.split('?')[0] === '/v1/approvals'
          ? { status: 200, body: { approvals: [], tenant_id: 'eng' } }
          : null,
    )
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { ApprovalsScreen } = await import('../Approvals')
    render(<ApprovalsScreen view="approval=apr_old" go={vi.fn()} />)
    const said = await screen.findByTestId('ap-decided', undefined, WAIT)
    printsThrough(said, EMAIL, FIRST, 'Approvals', NAME)
  })

  it("People's row", async () => {
    const row = {
      email: EMAIL, display_name: NAME, teams: ['eng'], github: 'connected',
      workspace: { state: 'none', setup_url: '/setup#workspace', setup_command: '/sc:setup' },
      claude_account: { source: 'none', own: 0, lent: 0, lent_by: [], provider_key: false },
      loan_request: null, first_seen: at(-600), last_seen: at(-5), last_submitted: null, last_active: at(-5),
    }
    serve((m, url) =>
      m === 'GET' && url === '/v1/admin/people'
        ? {
            status: 200,
            body: { people: [row], count: 1, pending: 0, audit: [], lendable_accounts: [], lendable_error: null, admins: [], admins_error: null },
          }
        : null,
    )
    vi.stubEnv('VITE_LIVE', '1')
    vi.resetModules()
    const { PeopleScreen } = await import('../People')
    render(<PeopleScreen />)
    const tr = await waitFor(() => {
      const r = document.querySelector<HTMLElement>(`tr[data-email="${EMAIL}"]`)
      expect(r).not.toBeNull()
      return r!
    }, WAIT)
    printsThrough(tr, EMAIL, NAME, "People's row (full: Admin › People)")
  })

  it("the workflow page's owner", async () => {
    const w: Workflow = {
      workflow_id: 'wf_pn', state: 'RUNNING', tenant_id: 'eng', stored_state: 'RUNNING', state_source: 'derived',
      rollup: { state: 'RUNNING', complete: true, reason: 'test', counts: {}, unreadable_steps: [], unstarted_steps: [], steps_read: 1 },
      created_at: at(-60), updated_at: at(-5), submitted_by: EMAIL, submitted_by_name: NAME,
      priority: 0, on_step_failure: 'fail_workflow', cancel_requested: false,
      steps: [{ step_id: 'only', runner_profile: 'claude-code', resource_class: 'standard', depends_on: [], input_from: {}, task_id: 't_pn' }],
    }
    const t = task({ id: 't_pn', state: 'RUNNING', workflow_id: 'wf_pn', step_id: 'only', submitted_by: EMAIL, submitted_by_name: NAME })
    serve(() => null)
    api.loadWorkflowBoard.mockResolvedValue({
      status: 'ok',
      data: { workflows: [w], taskById: new Map([['t_pn', t]]), statesDetail: null },
      fetchedAt: Date.now(),
    } satisfies Result<WorkflowBoard>)
    api.loadWorkflow.mockImplementation(ownFromBoard(api.loadWorkflowBoard))
    api.loadWorkflowUsage.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    api.loadAttempts.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    vi.resetModules()
    const { WorkflowsScreen } = await import('../Workflows')
    function Routed() {
      const [view, setView] = useState('wf=wf_pn')
      return <WorkflowsScreen view={view} onView={setView} />
    }
    render(<Routed />)
    const note = await waitFor(() => {
      const n = document.querySelector<HTMLElement>('.c-count-note')
      expect(n?.textContent ?? '').toContain(EMAIL)
      return n!
    }, WAIT)
    printsThrough(note, EMAIL, FIRST, "the workflow page's owner", NAME)
    // The note's title is its whole text, the owner included.
    expect(note.getAttribute('title')).toContain(`by ${FIRST} · ${EMAIL}`)
  })

  it("the task page's submitter", async () => {
    api.loadCheckpoints.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    api.loadTaskLogs.mockResolvedValue({ status: 'empty', fetchedAt: Date.now() })
    vi.resetModules()
    const { Run } = await import('../AgentDetail')
    const run: AgentRun = {
      task: task({ state: 'SUCCEEDED', attempt_count: 1, completed_at: at(12), submitted_by: EMAIL, submitted_by_name: NAME }),
      events: [ev('submitted', at(-1), null)],
      eventsDetail: null,
      attempts: [attempt(1)],
      attemptsDetail: null,
      classes: { standard: { name: 'standard', cpu: 2, memory_gib: 8, disk_gib: 4, units: 1 } },
      classesDetail: null,
      classesRouteMissing: false,
    }
    const { container } = render(<Run run={run} />)
    const note = await waitFor(() => {
      const n = [...container.querySelectorAll<HTMLElement>('.dt-progress .dt-note')].find((p) => p.textContent?.startsWith('submitted '))
      expect(n).toBeTruthy()
      return n!
    }, WAIT)
    printsThrough(note, EMAIL, FIRST, "the task page's submitter", NAME)
  })
})
