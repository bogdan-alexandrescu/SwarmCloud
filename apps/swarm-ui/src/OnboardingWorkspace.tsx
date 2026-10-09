import { useEffect, useRef, useState, type ReactNode } from 'react'
import { loadWorkspace, requestLoan, requestWorkspace } from './api'
import { Button, ButtonLink, Dash, ToneMark } from './components'
import { instant, spanText } from './duration'
import type { ApiError } from './fetch'
import { addressToPath } from './paths'
import { Mark } from './primitives'
import { useUrRead } from './RepositoriesParts'
import { usePoll } from './Shell'
import type { OnboardingStep, WorkspaceJobStep, WorkspaceView } from './types'
import { timeAgo } from './types'
import { useNow } from './useNow'

/**
 * THE TWO WORKSPACE STEPS OF THE SETUP CHECKLIST (#847, lane W8;
 * docs/workspaces.md §6.1). `Onboarding.tsx`'s checklist draws every step's
 * row; this file is the body of two of them, kept apart so that file's own
 * steps can change without touching these:
 *
 *   * `workspace`: request a personal workspace, then see it waited on,
 *     approved, set up step by step by the workspace job, and ready -- or
 *     denied with the admin's reason, held for the platform owner, or failed
 *     with §4.3's copy;
 *   * `claude_account`: a ready workspace runs nothing until it has a Claude
 *     account, the person's own (added on Capacity › Accounts) or one an admin
 *     lends after a loan request.
 *
 * EVERY STATE IS THE SERVER'S. The checklist's own read (`GET /v1/onboarding`)
 * carries the record as the step's evidence; while the record is not `ready`
 * this also reads `GET /v1/workspace` every 5 s (`usePoll`, which pauses in a
 * hidden tab and stops on unmount) and draws the newer of the two. When the
 * record's state moves, the checklist is read again, so the step's own state
 * and next-step mark follow. Nothing here marks a step done.
 *
 * THE BUTTONS POST ONCE. Each is disabled while its request is in flight, and
 * both routes are idempotent anyway: a second request answers the record as it
 * stands (§1.3).
 */

/** How often the record is read while it is not ready (§6.1: "every 5 seconds"). */
export const WORKSPACE_POLL_MS = 5_000

/**
 * §4.2's console labels, in order. A3/A4 are both "Identity" and A5/A6 both
 * "Access": one row each, done only when both of its job steps are.
 */
const JOB_ROWS: ReadonlyArray<{ label: string; ids: readonly string[] }> = [
  { label: 'Approved', ids: ['A1'] },
  { label: 'Checking the name is free', ids: ['A2'] },
  { label: 'Identity', ids: ['A3', 'A4'] },
  { label: 'Access', ids: ['A5', 'A6'] },
  { label: 'Namespace', ids: ['A7'] },
  { label: 'Limits', ids: ['A8'] },
  { label: 'Final check', ids: ['A9'] },
]

type RowState = 'done' | 'running' | 'failed' | 'held' | 'todo'

const ROW_TONE: Readonly<Record<RowState, string>> = { done: 'ok', running: 'live', failed: 'bad', held: 'warn', todo: 'unknown' }
const ROW_WORD: Readonly<Record<RowState, string>> = {
  done: 'done',
  running: 'running',
  failed: 'failed',
  held: 'held for the owner',
  todo: 'not started',
}

/** A row's state from the job's own step records. A step the job has not written is not started. */
function rowState(steps: Record<string, WorkspaceJobStep>, ids: readonly string[]): RowState {
  const states = ids.map((id) => steps[id]?.state ?? 'todo')
  if (states.includes('failed')) return 'failed'
  if (states.includes('held')) return 'held'
  if (states.every((s) => s === 'done')) return 'done'
  if (states.includes('running') || states.includes('done')) return 'running'
  return 'todo'
}

function str(v: unknown): string | null {
  return typeof v === 'string' && v !== '' ? v : null
}

/** The record from the checklist's evidence: the same `workspaces.view` fields, as the step carries them. */
function fromEvidence(step: OnboardingStep): WorkspaceView {
  return step.evidence as unknown as WorkspaceView
}

/** A refused write, as the API said it: its code and its sentence. */
function Refused({ error }: { error: ApiError }) {
  return (
    <p className="ur-small ob-issue" role="alert" data-code={error.code ?? undefined}>
      {error.code !== null && <b className="ur-bad">{error.code}</b>} {error.message}
    </p>
  )
}

/** The job's §4.2 steps as rows: shared with Admin › People, which shows an approval's run live. */
export function JobSteps({ record }: { record: WorkspaceView }) {
  const steps = record.steps ?? {}
  return (
    <ol className="ob-ws-steps" aria-label="Workspace set-up steps">
      {JOB_ROWS.map((row) => {
        const state = rowState(steps, row.ids)
        return (
          <li key={row.label} data-state={state}>
            <ToneMark tone={ROW_TONE[state]} hidden /> {row.label} <span className="ob-state">{ROW_WORD[state]}</span>
          </li>
        )
      })}
    </ol>
  )
}

/** The `workspace` step's body. `reload` reads the checklist again. */
export function WorkspaceStepBody({ step, reload }: { step: OnboardingStep; reload: () => void }) {
  const evidence = fromEvidence(step)
  const live = useUrRead(loadWorkspace, 'workspace')
  const polled = live.state.status === 'ok' || live.state.status === 'stale' ? live.state.data : null
  const record: WorkspaceView = polled ?? evidence
  const ready = record.state === 'ready'
  // A 4xx on the record (a service account has no workspace; an API without
  // the route) will not change by asking again every 5 s, so the poll stops
  // there; a 5xx or an unreachable API keeps it going.
  const refusedRead = live.state.status === 'error' && live.state.error.httpStatus !== null && live.state.error.httpStatus < 500
  usePoll(WORKSPACE_POLL_MS, live.reload, ready || refusedRead)
  const now = useNow(1_000)
  const [posting, setPosting] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)

  // The record moved: read the checklist again, so the step's state, the
  // next-step mark and the steps after it follow the record.
  const seen = useRef(evidence.state)
  useEffect(() => {
    if (polled === null || polled.state === seen.current) return
    seen.current = polled.state
    reload()
  }, [polled, reload])

  const request = async () => {
    if (posting) return
    setPosting(true)
    setRefused(null)
    const r = await requestWorkspace()
    setPosting(false)
    if (r.status === 'error') {
      setRefused(r.error)
      return
    }
    live.reload()
  }

  const id = str(record.workspace_id)
  const requestedAt = str(record.requested_at)
  let body: ReactNode
  switch (record.state) {
    case 'none':
      body = (
        <>
          <p className="ur-hint ob-help">
            Your own isolated space to run agents in: an identity, a storage area and a Kubernetes namespace that only
            your work uses. An admin approves it, and it is ready a few minutes later. Until then you can look around and
            connect GitHub, but nothing of yours runs. Team work is not affected.
          </p>
          <span className="ob-acts">
            <Button kind="primary" size="sm" disabled={posting} onClick={() => void request()}>
              {posting ? 'Requesting…' : 'Request my workspace'}
            </Button>
          </span>
        </>
      )
      break
    case 'requested':
      body = (
        <>
          <p className="ur-hint ob-line">
            Workspace requested{id !== null && <> (<code>{id}</code>)</>}
            {record.held != null ? ' — being migrated' : ' — waiting for an admin to approve'}
            {requestedAt !== null && <span className="ob-at"> · {timeAgo(requestedAt, now)}</span>}
          </p>
          {record.held != null && <p className="ur-small">{record.held.copy}</p>}
        </>
      )
      break
    case 'approved':
    case 'applying':
    case 'needs_owner': {
      const since = instant(record.decision?.at ?? null) ?? instant(requestedAt)
      body = (
        <>
          <p className="ur-hint ob-line">
            Setting up your workspace{id !== null && <> — <code>{id}</code></>}
            {since !== null ? (
              <span className="ob-at"> · {spanText(Math.max(0, now - since))}</span>
            ) : (
              <>
                {' '}
                <Dash why="The record carries no time it was approved or requested" />
              </>
            )}
          </p>
          {record.decision?.auto === true && (
            <p className="ur-small">Approved automatically, because you are an admin.</p>
          )}
          {record.state === 'needs_owner' && (
            <p className="ur-small">Approved. A change needs the platform owner&apos;s review before it can finish.</p>
          )}
          <JobSteps record={record} />
          <p className="ur-hint">You can carry on with the steps below meanwhile.</p>
        </>
      )
      break
    }
    case 'ready':
      body = (
        <p className="ur-hint ob-line">
          Workspace ready{id !== null && <> (<code>{id}</code>)</>}
        </p>
      )
      break
    case 'denied': {
      const reason = str(record.decision?.reason)
      const again = instant(record.request_again_at ?? null)
      const allowed = again === null || again <= now
      body = (
        <>
          <p className="ur-small">Your workspace request was not approved</p>
          {reason !== null && <blockquote className="ob-ws-reason">“{reason}”</blockquote>}
          {allowed ? (
            <span className="ob-acts">
              <Button size="sm" disabled={posting} onClick={() => void request()}>
                {posting ? 'Requesting…' : 'Request again'}
              </Button>
            </span>
          ) : (
            <p className="ur-hint">
              You can ask again from {new Date(again).toLocaleString()}; an admin can approve it before then.
            </p>
          )}
        </>
      )
      break
    }
    case 'failed': {
      const copy = record.failure?.copy
      body = (
        <>
          <p className="ur-small">Setting up your workspace stopped{id !== null && <> (<code>{id}</code>)</>}.</p>
          {/* The checklist row prints the step's code and copy when its own
              read already says failed; this prints it only when the newer
              record got there first, so the sentence is never shown twice. */}
          {step.code === null && typeof copy === 'string' && <p className="ur-small">{copy}</p>}
        </>
      )
      break
    }
    default:
      body = <Dash why="The API answered a workspace state this console does not know" />
  }

  return (
    <div id="workspace" className="ob-ws" data-workspace-state={record.state}>
      {body}
      {refused !== null && <Refused error={refused} />}
      {step.required === false && record.state !== 'ready' && (
        <p className="ur-hint">Submissions are not held for this step right now.</p>
      )}
    </div>
  )
}

/** A count from the server: a measured zero is drawn as one, never as a blank. */
function Count({ n, what }: { n: unknown; what: string }) {
  if (typeof n !== 'number') return <Dash why={`The checklist did not say how many ${what}`} />
  return n === 0 ? <Mark kind="zero" say={`No ${what}: a count of zero`} /> : <b>{n}</b>
}

/** The `claude_account` step's body. `reload` reads the checklist again. */
export function ClaudeAccountStepBody({ step, reload }: { step: OnboardingStep; reload: () => void }) {
  const ev = step.evidence ?? {}
  const [posting, setPosting] = useState(false)
  const [asked, setAsked] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const loanRequested = asked || ev.loan_request === 'requested'

  const loan = async () => {
    if (posting) return
    setPosting(true)
    setRefused(null)
    const r = await requestLoan()
    setPosting(false)
    if (r.status === 'error') {
      setRefused(r.error)
      return
    }
    setAsked(true)
    reload()
  }

  return (
    <div id="claude-account" className="ob-ws" data-claude-state={step.state}>
      <p className="ur-hint ob-line">
        your own <Count n={ev.own} what="accounts of your own" /> · lent to you <Count n={ev.lent} what="accounts lent to you" />
        {ev.provider_key === true && ' · a provider key'}
      </p>
      {step.state !== 'done' && (
        <>
          <p className="ur-hint ob-help">Your workspace runs nothing until it has a Claude account to run on.</p>
          {loanRequested ? (
            <p className="ur-small">Loan requested</p>
          ) : (
            <span className="ob-acts">
              <ButtonLink size="sm" href={addressToPath('capacity/accounts')}>
                Add key
              </ButtonLink>
              <Button size="sm" disabled={posting} onClick={() => void loan()}>
                {posting ? 'Requesting…' : 'Request a loan'}
              </Button>
            </span>
          )}
        </>
      )}
      {refused !== null && <Refused error={refused} />}
      {step.required === false && step.state !== 'done' && (
        <p className="ur-hint">Submissions are not held for this step right now.</p>
      )}
    </div>
  )
}

/** The body for one of the two steps, or nothing for any other step. */
export function WorkspaceStepDetail({ step, reload }: { step: OnboardingStep; reload: () => void }) {
  if (step.step === 'workspace') return <WorkspaceStepBody step={step} reload={reload} />
  if (step.step === 'claude_account') return <ClaudeAccountStepBody step={step} reload={reload} />
  return null
}
