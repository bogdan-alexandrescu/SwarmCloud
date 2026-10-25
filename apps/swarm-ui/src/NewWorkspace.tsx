import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { createAdminWorkspace, loadAdminWorkspace } from './api'
import { Button, Dash, Dialog, Tabs, ToneMark } from './components'
import type { ApiError } from './fetch'
import { ApprovedWaiting, WORKSPACE_POLL_MS, waitingOf } from './OnboardingWorkspace'
import { usePoll } from './Shell'
import type { AdminWorkspaceView, WorkspaceJobStep, WorkspaceKind } from './types'
import { timeAgo } from './types'
import { useNow } from './useNow'

/**
 * ADMIN › TENANTS › NEW WORKSPACE (#847, lane WK-UI-DIALOG;
 * docs/design/workspace-kinds.md §4.2.4). An admin files a personal, group or
 * service workspace, and then watches the workspace job build it.
 *
 * THREE TABS, ONE PER KIND. Each takes one address and an optional display
 * name, and says before submit what the kind will get -- and, for a service,
 * that the PLATFORM OWNER approves it (Q2 (a), 2026-10-19): a service account
 * is the one kind that admits a new principal at the API's front door, so an
 * admin's request for one waits for the owner, where a personal or group
 * request by an admin is approved in the same transaction that files it.
 *
 * THE POST IS `{kind, principal, display_name}` AND NOTHING ELSE. Limits are
 * the API's per-kind default (§4.2.2), so the dialog does not send them, and
 * nothing a caller could send here names an image, a command, a resource spec
 * or a backend parameter (invariant 10). Every shape check is the API's
 * (§4.2.3): a refusal is drawn as its code and its sentence, word for word,
 * and the console restates none of the rules. The shared deny-list is checked
 * by the job's S1 alone, never here (rule 2: it lives once, in
 * `scripts/lib/common.sh`). The button posts once: it is disabled while the
 * request is in flight, and the route is idempotent anyway.
 *
 * AFTER SUBMIT, THE RECORD'S PROGRESS. The job's steps are drawn as a
 * timeline, in the order the job runs them for the record's kind (§4.3: S1
 * after the claim and S2 before the final check, for a service only), with any
 * step id the record carries that this console has no label for appended
 * under its id, so a step the job gains later is shown rather than dropped.
 * The record is read every 5 s (`usePoll`: paused in a hidden tab, stopped on
 * unmount, so closing the dialog stops it) until it is `ready` -- or `denied`
 * or `failed`, which the job does not move by itself: the answer to either is
 * Admin › Tenants' Retry or a new request. An approval nothing will build is
 * said in words (`ApprovedWaiting`), never drawn as in progress.
 */

/** §4.3's step ids, in the order the job runs them for each kind. */
export const STEP_ORDER: Readonly<Record<WorkspaceKind, readonly string[]>> = {
  personal: ['A1', 'A2', 'A3', 'A4', 'A5', 'A6', 'A7', 'A8', 'A9'],
  group: ['A1', 'A2', 'A3', 'A4', 'A5', 'A6', 'A7', 'A8', 'A9'],
  service: ['A1', 'S1', 'A2', 'A3', 'A4', 'A5', 'A6', 'A7', 'A8', 'S2', 'A9'],
}

/** §4.3's rows, as an admin reads them. A6 is the person's App slot for a personal workspace and the team slot otherwise. */
export const STEP_LABEL: Readonly<Record<string, string>> = {
  A1: 'Approved and claimed',
  S1: 'Checking the service account',
  A2: 'Checking the name is free',
  A3: 'Worker account',
  A4: 'Its own IAM',
  A5: 'Storage prefix',
  A6: 'Forge slot',
  A7: 'Namespace',
  A8: 'Tenant and pool',
  S2: 'Admitted at the front door',
  A9: 'Final check',
}

type StepState = WorkspaceJobStep['state']

const STEP_TONE: Readonly<Record<StepState, string>> = { done: 'ok', running: 'live', failed: 'bad', held: 'warn', todo: 'unknown' }
const STEP_WORD: Readonly<Record<StepState, string>> = {
  done: 'done',
  running: 'running',
  failed: 'failed',
  held: 'held for the owner',
  todo: 'not started',
}

/** The tab's words: what the address is, and what the kind will get. */
const KINDS: ReadonlyArray<{ kind: WorkspaceKind; tab: string; field: string; example: string }> = [
  { kind: 'personal', tab: 'Personal', field: 'Person’s email address', example: 'name@your-domain' },
  { kind: 'group', tab: 'Group', field: 'Group address', example: 'team@your-domain' },
  { kind: 'service', tab: 'Service', field: 'Service account address', example: 'name@project.iam.gserviceaccount.com' },
]

/** `workspaces.MAX_DISPLAY_NAME`; the API refuses a longer one as 422. */
const MAX_DISPLAY_NAME = 60

/** The states the job does not move by itself: polling stops at each. */
const SETTLED: ReadonlySet<string> = new Set(['ready', 'denied', 'failed'])

function str(v: unknown): string | null {
  return typeof v === 'string' && v !== '' ? v : null
}

/** What the kind will get, shown before submit (§4.2.4). `who` is the address as typed, or the kind's placeholder. */
function Gets({ kind, who }: { kind: WorkspaceKind; who: string }) {
  const address = <code>{who}</code>
  let body: ReactNode
  switch (kind) {
    case 'personal':
      body = (
        <>
          <b>Personal</b> · {address} gets its own tenant, worker account, storage prefix and namespace. It is approved at
          once, because you are an admin, and is ready when they first sign in. It runs nothing until a Claude account is
          lent to them or they add their own.
        </>
      )
      break
    case 'group':
      body = (
        <>
          <b>Group</b> · {address} gets its own tenant, worker account, storage prefix and namespace, shared by the
          group&apos;s members, and an empty team forge slot. The API first checks that the group can be read in the
          directory. It is approved at once, because you are an admin.
        </>
      )
      break
    case 'service':
      body = (
        <>
          <b>Service</b> · {address} gets its own tenant, worker account, storage prefix and namespace, and is admitted at
          the API’s front door. <b>The platform owner approves it.</b> It runs nothing until a Claude account is lent to
          it or added.
        </>
      )
      break
  }
  return (
    <p className="ur-hint" data-testid="new-workspace-gets">
      {body}
    </p>
  )
}

/** A refused write, as the API said it: its code and its sentence. */
function Refused({ error }: { error: ApiError }) {
  return (
    <p className="ur-small ob-issue" role="alert" data-code={error.code ?? undefined}>
      {error.code !== null && <b className="ur-bad">{error.code}</b>} {error.message}
    </p>
  )
}

/** The ids to draw: the kind's order, then any id the record carries that the order does not name, in the API's order. */
export function stepIds(record: AdminWorkspaceView): string[] {
  const order = STEP_ORDER[record.kind ?? 'personal'] ?? STEP_ORDER.personal
  const extra = Object.keys(record.steps ?? {}).filter((id) => !order.includes(id))
  return [...order, ...extra]
}

/** The job's steps as a timeline: one row per step, its state, when the job last wrote it, and its code. */
export function WorkspaceTimeline({ record, now }: { record: AdminWorkspaceView; now: number }) {
  const steps = record.steps ?? {}
  return (
    <ol className="ob-ws-steps" aria-label="Workspace job steps">
      {stepIds(record).map((id) => {
        const step = steps[id]
        const state: StepState = step?.state ?? 'todo'
        const at = str(step?.at)
        const code = str(step?.code)
        return (
          <li key={id} data-step={id} data-state={state}>
            <ToneMark tone={STEP_TONE[state] ?? 'unknown'} hidden /> <code>{id}</code> {STEP_LABEL[id] ?? id}{' '}
            <span className="ob-state">{STEP_WORD[state] ?? state}</span>
            {at !== null && <span className="ob-at"> · {timeAgo(at, now)}</span>}
            {code !== null && (
              <>
                {' '}
                <b className="ur-bad">{code}</b>
              </>
            )}
          </li>
        )
      })}
    </ol>
  )
}

/** The record's state, in words, above its timeline. */
function Progress({ record, now }: { record: AdminWorkspaceView; now: number }) {
  const id = str(record.workspace_id)
  const idMark = id !== null ? <code>{id}</code> : <Dash why="The record carries no workspace id" />
  let line: ReactNode
  switch (record.state) {
    case 'requested':
      line = (
        <>
          <p className="ur-hint ob-line">
            Requested ({idMark}).{' '}
            {record.kind === 'service' ? 'Waiting for the platform owner to approve it.' : 'Waiting for an admin to approve it.'}
          </p>
          {record.held != null && <p className="ur-small">{record.held.copy}</p>}
        </>
      )
      break
    case 'approved':
    case 'applying':
      line =
        waitingOf(record) !== null ? (
          <ApprovedWaiting record={record} />
        ) : (
          <>
            <p className="ur-hint ob-line">Setting up ({idMark})</p>
            {record.decision?.auto === true && <p className="ur-small">Approved automatically, because you are an admin.</p>}
          </>
        )
      break
    case 'needs_owner': {
      const at = str(record.needs_owner_step)
      line = (
        <>
          <p className="ur-hint ob-line">
            {at !== null ? <>Stopped at {at} for the platform owner’s review</> : <>Stopped for the platform owner’s review</>}{' '}
            ({idMark}).
          </p>
          {typeof record.failure?.copy === 'string' && <p className="ur-small">{record.failure.copy}</p>}
        </>
      )
      break
    }
    case 'ready':
      line = <p className="ur-hint ob-line">Workspace ready ({idMark})</p>
      break
    case 'denied': {
      const reason = str(record.decision?.reason)
      line = (
        <>
          <p className="ur-small">Not approved ({idMark}).</p>
          {reason !== null && <blockquote className="ob-ws-reason">“{reason}”</blockquote>}
        </>
      )
      break
    }
    case 'failed':
      line = (
        <>
          <p className="ur-small">Setting up stopped ({idMark}).</p>
          {typeof record.failure?.copy === 'string' && <p className="ur-small">{record.failure.copy}</p>}
        </>
      )
      break
    default:
      line = <Dash why="The API answered a workspace state this console does not know" />
  }
  const name = str(record.display_name)
  const principal = str(record.principal)
  return (
    <div className="ob-ws" data-workspace-state={record.state} data-kind={record.kind}>
      {(name !== null || principal !== null) && (
        <p className="ur-small">
          {name !== null && <b>{name}</b>}
          {name !== null && principal !== null && ' · '}
          {principal !== null && <code>{principal}</code>}
        </p>
      )}
      {line}
      <WorkspaceTimeline record={record} now={now} />
    </div>
  )
}

/**
 * The dialog. Rendered by Admin › Tenants' New workspace button (lane WK-UI-TENANTS), which unmounts it on
 * `onClose`. `onFiled` is told the record once it is filed, so the opener can read its "Being created" list again.
 */
export function NewWorkspaceDialog({ onClose, onFiled }: { onClose: () => void; onFiled?: (record: AdminWorkspaceView) => void }) {
  return <NewWorkspaceForm onClose={onClose} onFiled={onFiled} />
}

/**
 * The form itself, ONE FORM FOR BOTH PLACES (WS-SETTINGS-E-REST; part of #2157): framed by the dialog above, or,
 * with `inline`, drawn on the page as Config › Workspace Settings › Add a new workspace draws it (S6c), its buttons
 * under its fields. Same fields, same post, same progress either way. Inline it has no Close unless `onClose` is given.
 */
export function NewWorkspaceForm({ onClose, onFiled, inline = false }: { onClose?: () => void; onFiled?: (record: AdminWorkspaceView) => void; inline?: boolean }) {
  const [kind, setKind] = useState<WorkspaceKind>('personal')
  const [principal, setPrincipal] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [posting, setPosting] = useState(false)
  const inFlight = useRef(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const [record, setRecord] = useState<AdminWorkspaceView | null>(null)
  // A 4xx on the record will not change by asking again every 5 s; a 5xx or an unreachable API keeps the poll going.
  const [readRefused, setReadRefused] = useState(false)
  const now = useNow(1_000)

  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  const workspaceId = str(record?.workspace_id)
  const reload = useCallback(() => {
    if (workspaceId === null) return
    void loadAdminWorkspace(workspaceId).then((r) => {
      if (!mounted.current) return
      if (r.status === 'ok' || r.status === 'stale') setRecord(r.data)
      else if (r.status === 'error' && r.error.httpStatus !== null && r.error.httpStatus < 500) setReadRefused(true)
    })
  }, [workspaceId])
  usePoll(WORKSPACE_POLL_MS, reload, record === null || workspaceId === null || SETTLED.has(record.state) || readRefused)

  const address = principal.trim()
  const submit = async () => {
    if (inFlight.current || address === '') return
    inFlight.current = true
    setPosting(true)
    setRefused(null)
    const name = displayName.trim()
    const r = await createAdminWorkspace({ kind, principal: address, display_name: name === '' ? null : name })
    inFlight.current = false
    if (!mounted.current) return
    setPosting(false)
    if (r.status === 'ok' || r.status === 'stale') {
      setRecord(r.data)
      onFiled?.(r.data)
      return
    }
    if (r.status === 'error') setRefused(r.error)
  }

  const current = KINDS.find((k) => k.kind === kind) ?? KINDS[0]!
  const actions = (
    <>
      {onClose !== undefined && <Button onClick={onClose}>Close</Button>}
      {record === null && (
        <Button kind="primary" disabled={posting || address === ''} onClick={() => void submit()}>
          {posting ? 'Creating…' : 'Create workspace'}
        </Button>
      )}
    </>
  )
  const body =
    record === null ? (
      <form
        onSubmit={(e) => {
          e.preventDefault()
          void submit()
        }}
      >
        <Tabs
          label="Workspace kind"
          tabs={KINDS.map((k) => ({ key: k.kind, label: k.tab }))}
          current={kind}
          onSelect={(key) => {
            setKind(key as WorkspaceKind)
            setRefused(null)
          }}
        />
        <label className="c-field">
          <span className="c-lbl">{current.field}</span>
          <input
            className="c-inp"
            type="email"
            value={principal}
            maxLength={254}
            placeholder={current.example}
            autoComplete="off"
            spellCheck={false}
            disabled={posting}
            onChange={(e) => setPrincipal(e.target.value)}
          />
        </label>
        <label className="c-field">
          <span className="c-lbl">Display name (optional)</span>
          <input
            className="c-inp"
            type="text"
            value={displayName}
            maxLength={MAX_DISPLAY_NAME}
            disabled={posting}
            onChange={(e) => setDisplayName(e.target.value)}
          />
        </label>
        <Gets kind={kind} who={address === '' ? current.example : address} />
        {refused !== null && <Refused error={refused} />}
      </form>
    ) : (
      <Progress record={record} now={now} />
    )
  if (inline || onClose === undefined) {
    return (
      <div className="nw-inline">
        {body}
        {record === null && <div className="nw-acts">{actions}</div>}
      </div>
    )
  }
  return (
    <Dialog title="New workspace" onClose={onClose} actions={actions}>
      {body}
    </Dialog>
  )
}
