/**
 * ADMIN › SETUP'S STEP PANELS (DAYONE-SETUP-UI-1; part of #2688;
 * docs/design/day-one-plan.md §5.1, D26, frames B06-B09a, B11).
 *
 * The people part of an install: swarm install did the cloud part from a
 * terminal and waits on this page. Each console step is a row of the P1-B
 * checklist (`ob-list`), drawn from `GET /v1/admin/setup` and nothing else:
 * nothing here is marked done by hand, every row is re-derived on every read.
 *
 * THE ORDER IS GitHub App -> first team -> Claude pool accounts -> verify ->
 * Send to your team (BGD, 2026-10-29; plan review-fix finding 1). The P1-B
 * frames drew the accounts before the team; a pool account is filed into the
 * first team's tenant and its owner must be a member there, so the accounts
 * come after the team and their link is refused before it exists.
 *
 * THE VERIFY ROW (the gap P1 found) counts from the engine's own `verify`
 * field: the current check, done of total, elapsed, a countdown to the
 * expected end and to the deadline. "Send to your team" stays locked until
 * verify's result is `passed`: not on `finished`, not on every row being done.
 *
 * THE PLUGIN LINE carries one value, the deployment URL; the plugin reads the
 * rest from the deployment's discovery document.
 *
 * SAYS ONLY WHAT EXISTS (DAYONE-SETUP-UI-4): the verify row names the checks
 * stage 14 runs and "Run the console checks" starts the console's two runs
 * (SETUP-API-2's route), each drawn with its state and linked; the first
 * team's row shows its grant over the App installation's repositories and each
 * invitee as waiting until the Groups API sees them in the group; an invite
 * opens the group's members page in Google Admin (decision 4: swarm-api adds
 * no one). The Capacity card shows stage 0's `sizing`, read-only, and a
 * one-person install (`sizing.mode == "individual"`, decision 7) has no first
 * team and no invites: one line says its personal workspace is ready.
 *
 * REACHES THE HAND-OVER EITHER WAY (DAYONE-SETUP-UI-5A, audit findings 12,
 * 20, 27, 28). For one person the route's `personal` row (the installer's
 * personal workspace) takes the first team's place: its grant, its pool
 * accounts and its smoke. An account counts as signed in once the route sees
 * its label in the first workspace's pool, not only once `finish` stamped
 * `signed_in_at`. The hand-over waits for the smoke's `succeeded` (the route's
 * word), as `finish` does, and a failed smoke has a Retry
 * (`POST /v1/admin/setup/smoke`). The team form links Google Admin's groups
 * page and shows the route's refusal when the group cannot be read or the
 * installer is not in it. The App self-check waits, disabled with its reason,
 * until stage 9 reports infra pass 3, and `app_id_not_configured` reads as
 * that wait.
 *
 * `GitHubAppCallback` is the page GitHub sends the person back to after it
 * created the App from the manifest: it posts `{code, state}` to the
 * conversion route ONCE and says the secrets were stored, never what they are.
 *
 * The page around these panels (the strip, the lead, the layout) is
 * SETUP-UI-2's `AdminSetup.tsx`; the route and the nav are SETUP-UI-3's.
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import {
  checkGitHubAppInstallation,
  convertGitHubApp,
  createSetupTeam,
  finishSetup,
  inviteToSetupTeam,
  loadMe,
  loadSetup,
  loadTask,
  loadWorkflow,
  mintSetupLink,
  restartSetupSmoke,
  runGitHubAppSelfCheck,
  SMOKE_SUCCEEDED,
  startGitHubApp,
  startSetupVerifyRuns,
  type ApiError,
  type Result,
  type SetupAccount,
  type SetupGrant,
  type SetupInvite,
  type SetupLink,
  type SetupMember,
  type SetupSizing,
  type SetupSmoke,
  type SetupState,
  type SetupTeam,
  type SetupVerify,
  type SetupVerifyRuns,
  VERIFY_RUN_RETRIES,
} from './api'
import { Banner, Button, ButtonLink, Card, Chip, CodeBlock, ToneMark, type MarkTone } from './components'
import { agentPath } from './OverviewRegions'
import { OWN_SIGN_IN_WINDOW } from './SetupLink'
import type { TaskState } from './types'
import './styles/onboarding.css'

/** The stable channel's marketplace: the release bucket's root (DX3 B), as `swarm install` prints it. */
export const PLUGIN_MARKETPLACE = 'https://storage.googleapis.com/swarmcloud/marketplace.json'

/**
 * The channel a release name came from: a candidate (`YYYY.MM.N-rc.N`) is
 * published to `rc` only; every other release is `stable`, the channel
 * install.sh uses when SWARM_CHANNEL is unset.
 */
export function releaseChannel(release: string | null): string {
  return release !== null && /-rc\.\d+$/.test(release.trim()) ? 'rc' : 'stable'
}

/**
 * The marketplace of the deployment's own channel (audit 3, N10): stable's at
 * the bucket root, any other channel's at `channels/<channel>.marketplace.json`,
 * so an rc deployment installs its own candidate's plugin before any promotion.
 */
export function pluginMarketplace(channel: string): string {
  return channel === 'stable' ? PLUGIN_MARKETPLACE : `https://storage.googleapis.com/swarmcloud/channels/${channel}.marketplace.json`
}

/** How often the page re-reads while it is open: the verify row's refresh (§5.1). */
export const SETUP_REFRESH_MS = 5_000

/** How often the GitHub step asks whether the App is on the org yet. */
const INSTALL_POLL_MS = 10_000

/** How often the console checks' run states are read again while either is running. */
const RUN_POLL_MS = 10_000

/**
 * What stage 14 checks, as VERIFY-ENGINE runs it (steps/verify.py:
 * QUICK_CHECKS, then RUNS: the four product runs and the three check runs).
 */
export const VERIFY_CHECKS =
  'the Scheduler targets, the image digests, the reconciler’s reach into each workspace namespace and the Firestore migrations, then seven runs on this deployment: a task and a workflow from the plugin and from this console, a task on GKE, a live-browser task and a code-intelligence read'

/** The one line a one-person install shows in place of the first-team and invites steps (decision 7). */
export const ONE_PERSON_LINE = 'One-person install: your personal workspace is ready.'

/** Google Admin's groups page, where a Workspace group is created (audit finding 27). */
export const ADMIN_GROUPS_URL = 'https://admin.google.com/ac/groups'

/** What the GitHub step says while infra pass 3 has not given swarm-api the App's id (audit finding 28). */
export const WAITING_FOR_PASS_3 = 'waiting for swarm install’s infra pass 3, which gives swarm-api the App’s id'

/** A one-person install (decision 7): no team group, no first team, no invites. The route's `mode`, else the sizing's. */
export function isOnePerson(state: SetupState): boolean {
  return state.mode === 'individual'
}

/** What the grant, the pool accounts and the smoke hang on: the first team, or for one person the personal workspace. */
export interface FirstWorkspace {
  workspace: string
  progress: Record<string, unknown>
  grant: SetupGrant | null
  smoke: SetupSmoke | null
  pool_accounts: string[]
}

/** The first workspace, or null until there is one: the first team, or the personal workspace once swarm-api filed it. */
export function firstWorkspace(state: SetupState): FirstWorkspace | null {
  if (isOnePerson(state)) {
    const p = state.personal
    return p === null || p.workspace === null ? null : { workspace: p.workspace, progress: p.progress, grant: p.grant, smoke: p.smoke, pool_accounts: p.pool_accounts }
  }
  return state.teams[0] ?? null
}

/**
 * A pool account is signed in once `finish` stamped it, or once the route
 * sees its label in the first workspace's own pool: `signed_in_at` is written
 * only by the hand-over, so waiting on it alone would never offer the hand-over.
 */
export function accountSignedIn(state: SetupState, a: SetupAccount): boolean {
  return a.signed_in_at !== null || (firstWorkspace(state)?.pool_accounts.includes(a.label) ?? false)
}

export function signedInCount(state: SetupState): number {
  return state.accounts.filter((a) => accountSignedIn(state, a)).length
}

/** The first workspace's smoke passed, in the route's own word (`finish` waits for exactly this). */
export function smokePassed(state: SetupState): boolean {
  return firstWorkspace(state)?.smoke?.state === SMOKE_SUCCEEDED
}

/**
 * What the first workspace's smoke holds its step at, since the hand-over
 * waits for it: failed on a failed smoke, in progress once it has started or
 * a pool account is signed in to run it. Nothing while it waits for the first
 * account: the accounts step, next, is what it waits on.
 */
export function smokeHold(smoke: SetupSmoke | null, signedIn: boolean): StepState | null {
  if (smoke?.state === SMOKE_SUCCEEDED) return null
  if (smoke?.state === 'failed') return 'failed'
  return smoke !== null || signedIn ? 'in_progress' : null
}

/**
 * Infra pass 3 has run: stage 9's record (its first step is pass 3, and pass 4
 * and the hook come only after a passing self-check), or a self-check that
 * passed. Before it swarm-api has no App id, so the self-check cannot pass.
 */
export function pass3Ran(state: SetupState): boolean {
  const s = state.engine?.stages[9]?.state
  return s === 'done' || s === 'switches_on' || s === 'drift' || state.github_app?.self_check?.ok === true
}

/** The cloud stages swarm install finishes before the hand-off (§5.1: 0-8, 10, 13). */
const CLI_STAGES = [0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 13]

export type StepState = 'done' | 'in_progress' | 'failed' | 'todo'

const TONE: Record<StepState, MarkTone> = { done: 'ok', in_progress: 'live', failed: 'bad', todo: 'unknown' }
const WORD: Record<StepState, string> = { done: 'done', in_progress: 'in progress', failed: 'failed', todo: 'to do' }

// ---------------------------------------------------------------------------
// words
// ---------------------------------------------------------------------------

/** A span of seconds in words: "48 s", "4 min", "1 h 5 min". */
export function spanWords(seconds: number): string {
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s} s`
  const min = Math.round(s / 60)
  if (min < 60) return `${min} min`
  const h = Math.floor(min / 60)
  const m = min % 60
  return m === 0 ? `${h} h` : `${h} h ${m} min`
}

function agoWords(iso: string | null, nowMs: number): string | null {
  if (iso === null) return null
  const at = Date.parse(iso)
  return Number.isNaN(at) ? null : `${spanWords((nowMs - at) / 1000)} ago`
}

function untilWords(iso: string | null, nowMs: number): string | null {
  if (iso === null) return null
  const at = Date.parse(iso)
  if (Number.isNaN(at)) return null
  return at <= nowMs ? 'expired' : `expires in ${spanWords((at - nowMs) / 1000)}`
}

/** The one line a teammate runs: the deployment URL is its only value, from the marketplace of the release the engine recorded. */
export function pluginLine(hostname: string, release: string | null): string {
  return `claude plugin marketplace add ${pluginMarketplace(releaseChannel(release))} && claude plugin install sc@swarmcloud --config deployment_url=https://${hostname}`
}

/** The page's clock: the given instant (a test, a capture) or the browser's, ticking each second. */
function useNow(fixed: number | undefined): number {
  const [now, setNow] = useState(() => fixed ?? Date.now())
  useEffect(() => {
    if (fixed !== undefined) return undefined
    const id = window.setInterval(() => setNow(Date.now()), 1_000)
    return () => window.clearInterval(id)
  }, [fixed])
  return fixed ?? now
}

/**
 * `GET /v1/admin/setup`, read now and every 5 s while the page is open, so
 * the verify row and every step follow the record without a reload.
 */
export function useSetupState(intervalMs: number = SETUP_REFRESH_MS): { result: Result<SetupState>; reload: () => void } {
  const [result, setResult] = useState<Result<SetupState>>(() => ({ status: 'loading', since: Date.now() }))
  const [tick, setTick] = useState(0)
  const reload = useCallback(() => setTick((t) => t + 1), [])
  useEffect(() => {
    let alive = true
    void loadSetup().then((r) => {
      if (alive) setResult(r)
    })
    const id = window.setTimeout(() => setTick((t) => t + 1), intervalMs)
    return () => {
      alive = false
      window.clearTimeout(id)
    }
  }, [tick, intervalMs])
  return { result, reload }
}

// ---------------------------------------------------------------------------
// the parts each row is drawn from
// ---------------------------------------------------------------------------

interface Row {
  key: string
  title: string
  state: StepState
  by: string
  line: ReactNode
  next?: boolean
  body?: ReactNode
  act?: ReactNode
}

/** One fact under a row: a mark, a name and what is true of it. */
export interface Fact {
  state: StepState
  name: string
  text: ReactNode
}

export function Facts({ items, label }: { items: Fact[]; label?: string }) {
  return (
    <ul className="ob-list" aria-label={label}>
      {items.map((f, i) => (
        <li key={i} className={`ob-step is-${f.state}`}>
          <ToneMark tone={TONE[f.state]} hidden />
          <p className="ob-line ur-hint">
            <b>{f.name}</b> {f.text}
          </p>
        </li>
      ))}
    </ul>
  )
}

/** A failed step: its one-line cause, the fix and a Retry that re-runs the step's check first (§5.1). */
export function StepFailure({ cause, fix, onRetry, busy, retryLabel = 'Retry' }: { cause: string; fix: ReactNode; onRetry?: () => void; busy?: boolean; retryLabel?: string }) {
  return (
    <div className="ob-issue" role="alert">
      <b>{cause}</b>
      <span className="ur-hint">Fix: {fix}</span>
      {onRetry !== undefined && (
        <span className="ob-acts">
          <Button size="sm" onClick={onRetry} busy={busy === true ? 'Retrying' : false}>
            {retryLabel}
          </Button>
        </span>
      )}
    </div>
  )
}

function ErrorLine({ error }: { error: ApiError | null }) {
  if (error === null) return null
  return (
    <div className="ob-issue" role="alert">
      <b>{error.message}</b>
      {error.kind === 'admin_required' && <span className="ur-hint">Only a platform admin can run Admin › Setup.</span>}
    </div>
  )
}

/** One write in flight at a time per panel, its error kept until the next try. */
function useAction(onChanged: () => void): { busy: string | null; error: ApiError | null; run: <T>(name: string, call: () => Promise<Result<T>>, then?: (data: T) => void) => void } {
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<ApiError | null>(null)
  const run = useCallback(
    <T,>(name: string, call: () => Promise<Result<T>>, then?: (data: T) => void) => {
      setBusy(name)
      setError(null)
      void call().then((r) => {
        setBusy(null)
        if (r.status === 'error') {
          setError(r.error)
          return
        }
        if ((r.status === 'ok' || r.status === 'stale') && then !== undefined) then(r.data)
        onChanged()
      })
    },
    [onChanged],
  )
  return { busy, error, run }
}

/** Post the App manifest to github.com as the manifest flow requires: a form POST, never a fetch. */
function postManifest(action: string, manifest: Record<string, unknown>): void {
  const form = document.createElement('form')
  form.method = 'post'
  form.action = action
  const input = document.createElement('input')
  input.type = 'hidden'
  input.name = 'manifest'
  input.value = JSON.stringify(manifest)
  form.appendChild(input)
  document.body.appendChild(form)
  form.submit()
}

// ---------------------------------------------------------------------------
// each step
// ---------------------------------------------------------------------------

function cloudRow(state: SetupState): Row {
  const stages = state.engine?.stages ?? []
  const done = CLI_STAGES.filter((n) => stages[n]?.state === 'done')
  const last = CLI_STAGES.map((n) => stages[n]?.at ?? null).filter((a): a is string => a !== null).sort().pop() ?? null
  const all = done.length === CLI_STAGES.length
  return {
    key: 'cloud',
    title: 'Project, infrastructure, identity, edge and secrets',
    state: all ? 'done' : 'in_progress',
    by: 'swarm install',
    line: all
      ? `swarm install did the cloud part: stages 0-8, 10 and 13${last === null ? '' : `, finished ${new Date(last).toLocaleString()}`}`
      : `swarm install has done ${done.length} of ${CLI_STAGES.length} cloud stages; it hands over to this page when they are done`,
  }
}

function GitHubAppPanel({ state, onChanged, act }: { state: SetupState; onChanged: () => void; act: ReturnType<typeof useAction> }) {
  const app = state.github_app
  const org = state.github_org ?? 'your GitHub organisation'
  const created = app !== null && app.id !== null
  const polling = created && app.installed_on === null
  useEffect(() => {
    if (!polling) return undefined
    const id = window.setInterval(() => void checkGitHubAppInstallation().then((r) => r.status === 'ok' && r.data.installed && onChanged()), INSTALL_POLL_MS)
    return () => window.clearInterval(id)
  }, [polling, onChanged])
  if (!created) return null
  const facts: Fact[] = [
    { state: 'done', name: 'Created from the manifest', text: `${app.slug ?? 'the App'}${app.id === null ? '' : ` (app id ${app.id})`} on ${org}` },
    {
      state: app.secrets_written_at === null ? 'in_progress' : 'done',
      name: 'Secrets stored',
      text: 'the private key, the client secret and the webhook secret went from GitHub’s answer straight into Secret Manager; none is shown here or anywhere',
    },
    {
      state: app.installed_on === null ? 'in_progress' : 'done',
      name: app.installed_on === null ? `Install on ${org}` : `Installed on ${app.installed_on}`,
      text: app.installed_on === null ? 'this page checks every 10 s until GitHub shows the installation' : 'GitHub shows the installation',
    },
  ]
  if (app.installed_on !== null) {
    const switched = pass3Ran(state)
    const check = app.self_check
    const waiting = check === null || check.state === 'waiting_for_infra_pass_3'
    facts.push({
      state: check?.state === 'passed' ? 'done' : check?.state === 'failed' ? 'failed' : waiting && switched ? 'todo' : 'in_progress',
      name: 'Self-check',
      text:
        check?.state === 'passed'
          ? 'the App’s key signs and GitHub answers as the App'
          : check?.state === 'failed'
            ? check.reason ?? 'GitHub did not answer as the App'
            : switched
              ? 'swarm install’s infra pass 3 gave swarm-api the App’s id; this page runs the self-check by itself'
              : WAITING_FOR_PASS_3,
    })
  }
  return (
    <>
      <Facts items={facts} label="GitHub App" />
      <ErrorLine error={act.error} />
    </>
  )
}

function githubRow(state: SetupState, onChanged: () => void, act: ReturnType<typeof useAction>): Row {
  const app = state.github_app
  const org = state.github_org ?? 'your GitHub organisation'
  const create = (
    <Button
      size="sm"
      kind="primary"
      busy={act.busy === 'create' ? 'Opening GitHub' : false}
      onClick={() => act.run('create', startGitHubApp, (d) => d.action !== '' && postManifest(d.action, d.manifest))}
    >
      Create the GitHub App
    </Button>
  )
  // Before infra pass 3 swarm-api has no App id, and the check answers `app_id_not_configured` (finding 28).
  const selfCheck = (label: string, kind: 'ghost' | 'secondary' = 'secondary') => (
    <Button
      size="sm"
      kind={kind === 'ghost' ? 'ghost' : undefined}
      busy={act.busy === 'self-check' ? 'Checking' : false}
      // A few words: the reason sits in the row's action column, and the row's line and Self-check fact say the rest.
      disabledReason={pass3Ran(state) ? undefined : 'After infra pass 3'}
      onClick={() => act.run('self-check', runGitHubAppSelfCheck)}
    >
      {label}
    </Button>
  )
  const body = <GitHubAppPanel state={state} onChanged={onChanged} act={act} />
  if (app === null || !app.started) {
    return {
      key: 'github',
      title: 'GitHub App',
      state: 'todo',
      by: 'this page',
      line: `One button: GitHub creates the App on ${org} from a manifest with its permissions filled in, and sends you back here; its secrets go straight into Secret Manager`,
      body: <ErrorLine error={act.error} />,
      act: create,
    }
  }
  if (app.id === null) {
    return {
      key: 'github',
      title: 'GitHub App',
      state: 'in_progress',
      by: 'this page',
      line: 'Waiting for GitHub to send you back here with the new App. If you closed that tab, start again',
      body: <ErrorLine error={act.error} />,
      act: create,
    }
  }
  if (app.self_check !== null && app.self_check.state === 'failed') {
    return {
      key: 'github',
      title: 'GitHub App',
      state: 'failed',
      by: 'this page',
      line: `${app.slug ?? 'The App'} is installed on ${app.installed_on ?? org}, but its self-check failed`,
      body: (
        <>
          {body}
          <StepFailure
            cause={app.self_check.reason ?? 'GitHub did not answer as the App'}
            fix={`check the App still exists on ${app.installed_on ?? org} and is installed there; if its private key was revoked, create the App again here. Retry runs the self-check again.`}
            onRetry={() => act.run('self-check', runGitHubAppSelfCheck)}
            busy={act.busy === 'self-check'}
          />
        </>
      ),
    }
  }
  if (app.installed_on === null) {
    return {
      key: 'github',
      title: 'GitHub App',
      state: 'in_progress',
      by: 'this page',
      line: `Created and its secrets stored; install it on ${org}`,
      body,
      act: (
        <span className="ob-acts">
          {app.slug !== null && (
            <ButtonLink size="sm" kind="primary" href={`https://github.com/apps/${encodeURIComponent(app.slug)}/installations/new`} target="_blank" rel="noreferrer">
              Install on {org}
            </ButtonLink>
          )}
          <Button size="sm" kind="ghost" busy={act.busy === 'installation' ? 'Checking' : false} onClick={() => act.run('installation', checkGitHubAppInstallation)}>
            Check again
          </Button>
        </span>
      ),
    }
  }
  if (app.self_check === null || app.self_check.state === 'waiting_for_infra_pass_3') {
    return {
      key: 'github',
      title: 'GitHub App',
      state: 'in_progress',
      by: 'this page',
      line: pass3Ran(state) ? `Installed on ${app.installed_on}; this page runs the self-check now` : `Installed on ${app.installed_on}; ${WAITING_FOR_PASS_3}`,
      body,
      act: selfCheck('Run the self-check'),
    }
  }
  return {
    key: 'github',
    title: 'GitHub App',
    state: 'done',
    by: 'this page',
    line: `${app.slug ?? 'The App'} · installed on ${app.installed_on} · self-check passed`,
    act: selfCheck('Re-check', 'ghost'),
  }
}

// The workspace record's own states (swarm_api/workspaces.py STATES): `ready`
// is the apply's success; `failed` and `denied` are the two it does not
// recover from on its own, so each draws its fix and a Retry.
const WORKSPACE_READY = 'ready'
const FAILED_WORKSPACE = new Set(['failed', 'denied'])

/**
 * The first team's form. `created` is the group swarm install's stage 8
 * created, with the installer as its owner (N20): the field starts from it,
 * even when the record arrives on a later poll, until the person types.
 */
function TeamForm({ created, onChanged }: { created: string | null; onChanged: () => void }) {
  const act = useAction(onChanged)
  const [typed, setGroup] = useState<string | null>(null)
  const group = typed ?? created ?? ''
  const [name, setName] = useState('')
  const [admin, setAdmin] = useState('')
  const [invites, setInvites] = useState('')
  const field = (label: string, value: string, set: (v: string) => void, hint: string, placeholder: string, aside?: ReactNode) => (
    <label className="c-field">
      <span className="c-lbl">{label}</span>
      <input className="c-inp" aria-label={label} value={value} placeholder={placeholder} onChange={(e) => set(e.target.value)} />
      <p className="c-hint">
        {hint}
        {aside}
      </p>
    </label>
  )
  const submit = () =>
    act.run('team', () =>
      createSetupTeam({
        group: group.trim(),
        ...(name.trim() === '' ? {} : { display_name: name.trim() }),
        ...(admin.trim() === '' ? {} : { first_admin: admin.trim() }),
        invites: invites.split(/[\s,]+/).filter(Boolean),
      }),
      // The route answers each invite with the group's members page (#26): open it, as the Invite hint says.
      (d) => {
        const page = d.invites.map((i) => i.add_members_url).find((u): u is string => u !== null)
        if (page !== undefined) window.open(page, '_blank', 'noopener,noreferrer')
      },
    )
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        submit()
      }}
    >
      {field(
        'Workspace group',
        group,
        setGroup,
        group !== '' && group === created
          ? 'The group swarm install created, with you as its owner; its members are the team. Another group? '
          : 'A Google Workspace group you are a member of; its members are the team. No group yet? ',
        'engineering@your-domain',
        <a href={ADMIN_GROUPS_URL} target="_blank" rel="noreferrer">
          Create group in Google Admin
        </a>,
      )}
      {field('Workspace name', name, setName, 'Shown in the console; the group’s name when left empty', 'engineering')}
      {field('First workspace admin', admin, setAdmin, 'Administers the team’s workspace', 'name@your-domain')}
      {field('Invite', invites, setInvites, 'Each is added to the group on its members page in Google Admin, which opens when you create the team; the team’s row shows them as joined once the Groups API sees them', 'one or more emails')}
      <span className="ob-acts">
        <Button size="sm" kind="primary" type="submit" busy={act.busy === 'team' ? 'Creating' : false} disabledReason={group.trim().length < 3 ? 'Name the Workspace group first' : undefined}>
          Create the team
        </Button>
      </span>
      <ErrorLine error={act.error} />
      <GroupRefusal error={act.error} />
    </form>
  )
}

/** A Google Admin page from the route's refusal; anything else is not drawn as a link. */
const adminPage = (v: unknown): string | null => (typeof v === 'string' && v.startsWith('https://admin.google.com/') ? v : null)

/**
 * The team route's refusal (`check_team_group`, finding 27): swarm-api could
 * not read the group, or the installer is not in it. The message is the
 * route's (`ErrorLine`); this adds the Google Admin page that fixes it.
 */
function GroupRefusal({ error }: { error: ApiError | null }) {
  const d = error !== null && typeof error.detail === 'object' && error.detail !== null ? (error.detail as Record<string, unknown>) : null
  if (d === null) return null
  if (d.reason === 'installer_not_member') {
    const page = adminPage(d.add_members_url) ?? adminPage(d.admin_groups_url) ?? ADMIN_GROUPS_URL
    return (
      <span className="ob-acts">
        <ButtonLink size="sm" href={page} target="_blank" rel="noreferrer">
          Add yourself to the group in Google Admin
        </ButtonLink>
      </span>
    )
  }
  if (d.reason === 'group_unreadable') {
    return (
      <span className="ob-acts">
        <ButtonLink size="sm" href={adminPage(d.admin_groups_url) ?? ADMIN_GROUPS_URL} target="_blank" rel="noreferrer">
          Create or find the group in Google Admin
        </ButtonLink>
      </span>
    )
  }
  return null
}

/** Who the team row waits on: the route's live membership, or the recorded invites when it served none. */
function teamPeople(team: SetupTeam): SetupMember[] {
  return team.members.length > 0 ? team.members : team.invites.map((email) => ({ email, member: null }))
}

function grantFact(g: SetupGrant | null, org: string, whose: string = 'the team'): Fact {
  if (g === null) return { state: 'in_progress', name: 'Repository grant', text: `${whose}’s grant over the repositories the GitHub App installation on ${org} covers; read on the next refresh` }
  if (g.state === 'granted' && g.repositories.length > 0) {
    const n = g.repositories.length
    return {
      state: 'done',
      name: 'Repository grant',
      text: `${whose} may write to the ${n === 1 ? 'repository' : `${n} repositories`} the App installation${g.account === null ? '' : ` on ${g.account}`} covers: ${g.repositories.join(', ')}`,
    }
  }
  if (g.state === 'waiting_for_repositories') {
    return { state: 'in_progress', name: 'Repository grant', text: `the App is installed${g.account === null ? '' : ` on ${g.account}`} on no repository yet: add one to the installation on GitHub, and the grant follows on the next refresh` }
  }
  return { state: 'in_progress', name: 'Repository grant', text: `waits for the GitHub App to be installed on ${org}` }
}

/** The first workspace's smoke, in the route's words: `succeeded` is done, `failed` is failed (finding 20). */
function smokeFact(smoke: SetupSmoke | null, signedIn: boolean): Fact {
  return {
    state: smoke === null ? 'todo' : smoke.state === SMOKE_SUCCEEDED ? 'done' : smoke.state === 'failed' ? 'failed' : 'in_progress',
    name: 'Smoke agent',
    text:
      smoke === null
        ? signedIn
          ? 'starts now that a pool account is signed in'
          : 'waits for a pool account (the next step): it needs one to run'
        : `${smoke.state}${smoke.detail === null ? '' : ` · ${smoke.detail}`}`,
  }
}

/** A failed smoke's fix and its Retry: `POST /v1/admin/setup/smoke` starts it again (finding 20). */
function SmokeRetry({ smoke, onChanged }: { smoke: SetupSmoke | null; onChanged: () => void }) {
  const act = useAction(onChanged)
  if (smoke?.state !== 'failed') return null
  return (
    <>
      <StepFailure
        cause={`The smoke task failed${smoke.detail === null ? '' : `: ${smoke.detail}`}`}
        fix="its task’s page says why; once that is fixed, Retry starts the smoke again. The hand-over waits for it to succeed."
        onRetry={() => act.run('smoke', restartSetupSmoke)}
        busy={act.busy === 'smoke'}
        retryLabel="Retry the smoke"
      />
      <ErrorLine error={act.error} />
    </>
  )
}

function teamFacts(team: SetupTeam, signedIn: boolean, nowMs: number, org: string): Fact[] {
  const ws = typeof team.progress.workspace_state === 'string' ? team.progress.workspace_state : null
  const git = typeof team.progress.git_link === 'object' && team.progress.git_link !== null ? (team.progress.git_link as Record<string, unknown>) : null
  return [
    { state: 'done', name: 'Tenant record', text: `${team.workspace} · from ${team.group}, checked by membership per registered group` },
    {
      state: ws === null ? 'in_progress' : FAILED_WORKSPACE.has(ws) ? 'failed' : ws === WORKSPACE_READY ? 'done' : 'in_progress',
      name: 'Namespace, quota, verify-keys and worker identity',
      text: ws === null ? 'swarm-workspace-apply has not reported yet' : `swarm-workspace-apply: ${ws}`,
    },
    { state: team.first_admin === null ? 'todo' : 'done', name: 'First admin', text: team.first_admin ?? 'not named' },
    grantFact(team.grant, org),
    {
      state: git === null ? 'todo' : 'in_progress',
      name: 'Team git slot',
      text:
        git === null
          ? 'served by the GitHub App; a one-time link lets its owner enter a token instead'
          : `one-time link for ${String(git.owner ?? 'its owner')}, ${untilWords(typeof git.expires_at === 'string' ? git.expires_at : null, nowMs) ?? 'open'}`,
    },
    smokeFact(team.smoke, signedIn),
  ]
}

/** One fact per invitee: joined once the Groups API says so, waiting until then. */
function memberFacts(people: SetupMember[]): Fact[] {
  return people.map((p) =>
    p.member === true
      ? { state: 'done', name: p.email, text: 'is in the group' }
      : { state: 'in_progress', name: p.email, text: `waiting for ${p.email} to join${p.member === null ? ' (the Groups API did not answer this read)' : ''}` },
  )
}

/**
 * Invite one more person (decision 4): records the invite and opens the
 * group's members page in Google Admin, where the person adds them; the row
 * shows them as waiting until the Groups API sees them in the group.
 */
function InviteControl({ team, onChanged }: { team: SetupTeam; onChanged: () => void }) {
  const act = useAction(onChanged)
  const [email, setEmail] = useState('')
  const [asked, setAsked] = useState<SetupInvite[]>([])
  const known = new Set(teamPeople(team).map((p) => p.email))
  const pending = asked.filter((a) => !known.has(a.email))
  const page = asked.map((a) => a.add_members_url).filter((u): u is string => u !== null).pop() ?? null
  const people = teamPeople(team)
  return (
    <>
      {(people.length > 0 || pending.length > 0) && (
        <Facts items={memberFacts([...people, ...pending.map((a) => ({ email: a.email, member: a.member }))])} label="Invited" />
      )}
      <form
        onSubmit={(e) => {
          e.preventDefault()
          const who = email.trim().toLowerCase()
          act.run('invite', () => inviteToSetupTeam(team.id, who), (d) => {
            setAsked((xs) => [...xs.filter((x) => x.email !== d.email), d])
            setEmail('')
            if (d.add_members_url !== null) window.open(d.add_members_url, '_blank', 'noopener,noreferrer')
          })
        }}
      >
        <label className="c-field">
          <span className="c-lbl">Invite someone</span>
          <input className="c-inp" aria-label="Invite someone" value={email} placeholder="name@your-domain" onChange={(e) => setEmail(e.target.value)} />
          <p className="c-hint">Opens {team.group}’s members page in Google Admin, where you add them; this row shows them as joined once the Groups API sees them</p>
        </label>
        <span className="ob-acts">
          <Button size="sm" type="submit" busy={act.busy === 'invite' ? 'Inviting' : false} disabledReason={email.trim().length < 3 ? 'Enter their email first' : undefined}>
            Invite
          </Button>
          {page !== null && (
            <ButtonLink size="sm" kind="ghost" href={page} target="_blank" rel="noreferrer">
              Open the group in Google Admin
            </ButtonLink>
          )}
        </span>
        <ErrorLine error={act.error} />
      </form>
    </>
  )
}

/**
 * A one-person install's first workspace (decision 7): the route's `personal`
 * row, the installer's personal workspace, with its grant and its smoke; the
 * first-team and invites steps are not needed, in the route's own words.
 */
function personalRow(state: SetupState, onChanged: () => void): Row {
  const p = state.personal
  const notNeeded = state.steps?.first_team?.reason ?? state.steps?.invites?.reason ?? 'not needed for one person'
  const skipped: Fact = { state: 'done', name: 'First team and invites', text: notNeeded }
  const base = { key: 'team', title: 'First workspace', by: 'swarm-api' }
  if (p === null || p.workspace === null) {
    return {
      ...base,
      state: 'in_progress',
      line: `One-person install: your personal workspace becomes the first workspace; swarm-api files it on this page’s next read${p?.reason ? ` (last read: ${p.reason})` : ''}`,
      body: <Facts items={[skipped]} label="First workspace" />,
    }
  }
  const ws = typeof p.progress.workspace_state === 'string' ? p.progress.workspace_state : null
  const failed = ws !== null && FAILED_WORKSPACE.has(ws)
  const signedIn = signedInCount(state) > 0
  const facts: Fact[] = [
    { state: 'done', name: 'Personal workspace', text: `${p.workspace}${p.owner === null ? '' : ` · ${p.owner}`}` },
    {
      state: ws === null ? 'in_progress' : failed ? 'failed' : ws === WORKSPACE_READY ? 'done' : 'in_progress',
      name: 'Namespace, quota, verify-keys and worker identity',
      text: ws === null ? 'swarm-workspace-apply has not reported yet' : `swarm-workspace-apply: ${ws}`,
    },
    skipped,
    grantFact(p.grant, state.github_org ?? 'your GitHub organisation', 'your personal workspace'),
    smokeFact(p.smoke, signedIn),
  ]
  return {
    ...base,
    state: failed ? 'failed' : ws === WORKSPACE_READY ? smokeHold(p.smoke, signedIn) ?? 'done' : 'in_progress',
    line: ws === WORKSPACE_READY ? `${ONE_PERSON_LINE} ${p.workspace}` : `One-person install: your personal workspace ${p.workspace} is ${ws ?? 'being set up'}`,
    body: (
      <>
        <Facts items={facts} label="First workspace" />
        <SmokeRetry smoke={p.smoke} onChanged={onChanged} />
      </>
    ),
  }
}

function teamRow(state: SetupState, onChanged: () => void, nowMs: number): Row {
  if (isOnePerson(state)) return personalRow(state, onChanged)
  const team = state.teams[0]
  if (team === undefined) {
    return {
      key: 'team',
      title: 'First team',
      state: 'todo',
      by: 'this page',
      line: 'A Workspace group becomes the first workspace: the same call swarm team add makes',
      body: <TeamForm created={state.engine?.workspace_groups?.team ?? null} onChanged={onChanged} />,
    }
  }
  const ws = typeof team.progress.workspace_state === 'string' ? team.progress.workspace_state : null
  const signedIn = signedInCount(state) > 0
  const failed = ws !== null && FAILED_WORKSPACE.has(ws)
  const people = teamPeople(team)
  const joined = people.filter((p) => p.member === true).length
  return {
    key: 'team',
    title: 'First team',
    state: failed ? 'failed' : smokeHold(team.smoke, signedIn) ?? 'done',
    by: 'this page',
    line: `${team.workspace} from ${team.group}${team.first_admin === null ? '' : ` · admin ${team.first_admin}`} · ${joined} of ${people.length} in the group`,
    body: (
      <>
        <Facts items={teamFacts(team, signedIn, nowMs, state.github_org ?? 'your GitHub organisation')} label="First team" />
        <SmokeRetry smoke={team.smoke} onChanged={onChanged} />
        <InviteControl team={team} onChanged={onChanged} />
        {failed && <TeamRetry team={team} onChanged={onChanged} />}
      </>
    ),
  }
}

function TeamRetry({ team, onChanged }: { team: SetupTeam; onChanged: () => void }) {
  const act = useAction(onChanged)
  return (
    <>
      <StepFailure
        cause={`swarm-workspace-apply reported ${String(team.progress.workspace_state)} for ${team.workspace}`}
        fix="the workspace apply job’s log says which part failed; Retry asks for the team again, which re-runs the apply."
        onRetry={() => act.run('retry', () => createSetupTeam({ group: team.group, ...(team.first_admin === null ? {} : { first_admin: team.first_admin }), invites: team.invites }))}
        busy={act.busy === 'retry'}
      />
      <ErrorLine error={act.error} />
    </>
  )
}

function accountFact(a: SetupAccount, signedIn: boolean, nowMs: number, pool: string): Fact {
  if (signedIn) {
    const ago = a.signed_in_at === null ? null : agoWords(a.signed_in_at, nowMs)
    return { state: 'done', name: a.label, text: `${a.owner ?? 'its owner'} · signed in${ago === null ? '' : ` ${ago}`} · in ${pool}` }
  }
  const until = untilWords(a.link_expires, nowMs)
  if (until === 'expired') return { state: 'failed', name: a.label, text: `${a.owner ?? 'its owner'}’s one-time link expired; send a new one` }
  return { state: 'in_progress', name: a.label, text: `waiting for ${a.owner ?? 'its owner'} to open their one-time link and sign in${until === null ? '' : `, ${until}`}` }
}

/**
 * The pool account's one-time link (audit 4d, F5 and F6). The owner is filled
 * in with the installer (one person: always, and only the installer); when the
 * owner is the person signed in here, the page opens their link itself in a
 * tab of its own, named OWN_SIGN_IN_WINDOW, which the link page reads as "open
 * it now", so nobody copies a link to themselves. A link for someone else is
 * made and shown once, to send them. No link is made before the workspace it
 * signs into is ready: before then the account sign-in is refused, and the
 * link would be spent for nothing.
 */
function AccountLinkForm({ onChanged, onePerson, installer, waitingFor }: { onChanged: () => void; onePerson: boolean; installer: string | null; waitingFor: string | null }) {
  const act = useAction(onChanged)
  const [label, setLabel] = useState('')
  const [me, setMe] = useState<string | null>(installer)
  const [owner, setOwner] = useState(installer ?? '')
  const typed = useRef(false)
  const [minted, setMinted] = useState<SetupLink | null>(null)
  const [opened, setOpened] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  useEffect(() => {
    if (installer !== null) return undefined
    let live = true
    void loadMe().then((r) => {
      const email = (r.status === 'ok' || r.status === 'stale') && typeof r.data?.principal?.email === 'string' ? r.data.principal.email : null
      if (!live || email === null || email === '') return
      setMe(email)
      if (!typed.current) setOwner(email)
    })
    return () => {
      live = false
    }
  }, [installer])
  const self = me !== null && owner.trim().toLowerCase() === me.toLowerCase()
  const shown = minted === null ? null : minted.url ?? minted.path
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        setCopied(false)
        setMinted(null)
        setOpened(null)
        const body = { kind: 'pool-account' as const, label: label.trim(), owner: owner.trim() }
        if (!self) {
          act.run('link', () => mintSetupLink(body), setMinted)
          return
        }
        // Opened now, inside the click, so no popup blocker stops it; the link goes into it once it is made.
        const tab = window.open('', OWN_SIGN_IN_WINDOW) ?? null
        if (tab !== null) tab.opener = null
        act.run(
          'link',
          () =>
            mintSetupLink(body).then((r) => {
              if (r.status === 'error') tab?.close()
              return r
            }),
          (link) => {
            const href = link.url ?? link.path
            if (tab !== null) {
              tab.location.href = href
            } else {
              window.name = OWN_SIGN_IN_WINDOW
              window.location.assign(href)
            }
            setOpened(link.label)
          },
        )
      }}
    >
      <label className="c-field">
        <span className="c-lbl">Account label</span>
        <input className="c-inp" value={label} placeholder="lowercase, e.g. ravi" onChange={(e) => setLabel(e.target.value)} />
      </label>
      <label className="c-field">
        <span className="c-lbl">Its owner</span>
        <input
          className="c-inp"
          value={owner}
          placeholder="name@your-domain"
          readOnly={onePerson && installer !== null}
          onChange={(e) => {
            typed.current = true
            setOwner(e.target.value)
          }}
        />
        <p className="c-hint">
          {onePerson ? 'You: a one-person install’s pool takes your own subscription' : 'You, or a member of the first team’s group'}
          {self ? '; your sign-in opens in a new tab' : '; the link opens for them only, once, within 24 h'}
        </p>
      </label>
      {waitingFor !== null && <p className="c-hint">{waitingFor}</p>}
      <span className="ob-acts">
        <Button
          size="sm"
          kind="primary"
          type="submit"
          busy={act.busy === 'link' ? (self ? 'Opening' : 'Making') : false}
          disabledReason={waitingFor !== null ? 'Workspace not ready' : label.trim() === '' || owner.trim() === '' ? 'Name the account and its owner first' : undefined}
        >
          {self ? 'Sign in my Claude account' : 'Make a sign-in link'}
        </Button>
      </span>
      {opened !== null && <p className="c-hint" role="status">{`Your sign-in for ${opened} opened in a new tab: sign in to Claude there, and this page sees the account at its next read.`}</p>}
      {shown !== null && minted !== null && (
        <CodeBlock
          title={`One-time link for ${minted.owner}, shown once: send it to them`}
          lang="url"
          text={shown}
          action={
            <Button
              size="sm"
              kind="ghost"
              onClick={() => {
                void navigator.clipboard?.writeText(shown).then(() => setCopied(true))
              }}
            >
              {copied ? 'Copied' : 'Copy'}
            </Button>
          }
        />
      )}
      <ErrorLine error={act.error} />
    </form>
  )
}

function accountsRow(state: SetupState, onChanged: () => void, nowMs: number): Row {
  const onePerson = isOnePerson(state)
  const pool = onePerson ? 'your personal workspace’s pool' : 'the first team’s pool'
  if (state.teams.length === 0 && !onePerson) {
    return {
      key: 'accounts',
      title: 'Claude accounts',
      state: 'todo',
      by: 'this page',
      line: 'Comes after the first team: each pool account is owned by it, so its owner must be a member of the first team’s group',
    }
  }
  const signed = signedInCount(state)
  const st: StepState = signed > 0 ? 'done' : state.accounts.length > 0 ? 'in_progress' : 'todo'
  // F6: the sign-in is refused until the workspace is `ready`, so no link is made for it before then.
  const first = firstWorkspace(state)
  const ws = first === null ? null : typeof first.progress.workspace_state === 'string' ? first.progress.workspace_state : null
  const waitingFor =
    ws === WORKSPACE_READY
      ? null
      : first === null
        ? 'Waiting for the workspace: a sign-in link is made once swarm-api has filed it and it is ready'
        : `Waiting for the workspace ${first.workspace} (${ws === null ? 'swarm-workspace-apply has not reported yet' : `swarm-workspace-apply: ${ws}`}): a sign-in link is made once it is ready`
  return {
    key: 'accounts',
    title: 'Claude accounts',
    state: st,
    by: 'this page',
    line:
      state.accounts.length === 0
        ? `Each subscription’s owner signs in once, through a one-time link this page makes, into ${pool}`
        : `Pool: ${signed} of ${state.accounts.length} signed in`,
    body: (
      <>
        {state.accounts.length > 0 && <Facts items={state.accounts.map((a) => accountFact(a, accountSignedIn(state, a), nowMs, pool))} label="Pool accounts" />}
        <AccountLinkForm onChanged={onChanged} onePerson={onePerson} installer={onePerson ? state.personal?.owner ?? null : null} waitingFor={waitingFor} />
      </>
    ),
  }
}

function addonsRow(state: SetupState): Row {
  return {
    key: 'addons',
    title: 'Add-ons',
    state: 'todo',
    by: 'optional',
    line: 'Off by default; core runs without them. Enabling one is a terminal step: it needs a Terraform switch',
    body:
      state.addons.length === 0 ? undefined : (
        <Facts items={state.addons.map((a) => ({ state: 'todo', name: a.name, text: <code>{a.enable}</code> }))} label="Add-ons" />
      ),
  }
}

/**
 * Every console step `finish` requires is done, so the hand-over is never
 * offered to a 409: the App installed and checked, a first workspace (the
 * first team; for one person the personal workspace), a pool account signed
 * in to it, and its smoke `succeeded`.
 */
export function consoleDone(state: SetupState): boolean {
  const app = state.github_app
  return app !== null && app.installed_on !== null && app.self_check?.ok === true && firstWorkspace(state) !== null && signedInCount(state) > 0 && smokePassed(state)
}

/** The verify row's words, from the engine's `verify` field and the page's clock. */
export function verifyWords(v: SetupVerify, nowMs: number): { progress: string; elapsed: string | null; expected: string | null; deadline: string | null } {
  const now = nowMs / 1000
  const progress = `${v.check ?? 'starting'} · ${v.done} of ${v.total}`
  const elapsed = v.started === null ? null : `${spanWords(now - v.started)} elapsed`
  const expected =
    v.expected_end === null ? null : v.expected_end >= now ? `about ${spanWords(v.expected_end - now)} left` : `${spanWords(now - v.expected_end)} past the expected end`
  const deadline = v.deadline === null ? null : v.deadline >= now ? `deadline in ${spanWords(v.deadline - now)}` : `${spanWords(now - v.deadline)} past its deadline`
  return { progress, elapsed, expected, deadline }
}

function VerifyFinish({ onChanged }: { onChanged: () => void }) {
  const act = useAction(onChanged)
  return (
    <span className="ob-acts">
      <Button size="sm" kind="primary" busy={act.busy === 'finish' ? 'Handing over' : false} onClick={() => act.run('finish', finishSetup)}>
        Hand over to swarm install
      </Button>
      <ErrorLine error={act.error} />
    </span>
  )
}

type RunStep = 'done' | 'failed' | 'in_progress'

/** A run's state word and mark: a task's TaskState or a workflow's derived state. */
function runStep(word: string | null): RunStep {
  if (word === null) return 'in_progress'
  const w = word.toUpperCase()
  if (w === 'SUCCEEDED') return 'done'
  return w === 'FAILED' || w === 'CANCELLED' || w === 'DEAD_LETTERED' ? 'failed' : 'in_progress'
}

/** The run's state as read now: `{word}`, or the read's own error, or null before the first read answers. */
type RunRead = { word: string } | { error: string } | null

function useRunState(id: string | null, read: (id: string) => Promise<Result<string>>): RunRead {
  // Kept with the id it was read for: a run started again has a fresh id, and
  // the old run's end must not stop the new one being read.
  const [seen, setSeen] = useState<{ id: string | null; got: RunRead }>({ id: null, got: null })
  const got = seen.id === id ? seen.got : null
  const [tick, setTick] = useState(0)
  const ended = got !== null && 'word' in got && runStep(got.word) !== 'in_progress'
  useEffect(() => {
    if (id === null || ended) return undefined
    let alive = true
    void read(id).then((r) => {
      if (!alive) return
      if (r.status === 'ok' || r.status === 'stale') setSeen({ id, got: { word: r.data } })
      else if (r.status === 'error') setSeen({ id, got: { error: r.error.message } })
    })
    const t = window.setTimeout(() => setTick((n) => n + 1), RUN_POLL_MS)
    return () => {
      alive = false
      window.clearTimeout(t)
    }
  }, [id, read, tick, ended])
  return got
}

const readTaskState = (id: string): Promise<Result<string>> =>
  loadTask(id).then((r) => (r.status === 'ok' || r.status === 'stale' ? { ...r, data: String(r.data.state) } : (r as Result<string>)))
const readWorkflowState = (id: string): Promise<Result<string>> =>
  loadWorkflow(id).then((r) => (r.status === 'ok' || r.status === 'stale' ? { ...r, data: String(r.data.workflow?.state ?? '') } : (r as Result<string>)))

function runFact(name: string, id: string, href: string, got: RunRead, repository: string | null): Fact {
  const where = repository === null ? '' : ` · on ${repository}`
  const link = <a href={href}>{id}</a>
  if (got === null) return { state: 'in_progress', name, text: <>{link} · reading its state{where}</> }
  if ('error' in got) return { state: 'todo', name, text: <>{link} · its state could not be read: {got.error}{where}</> }
  return { state: runStep(got.word), name, text: <>{link} · {got.word.toLowerCase().replace(/_/g, ' ')}{where}</> }
}

/**
 * "Run the console checks" (SETUP-API-2's `POST /v1/admin/setup/verify-runs`):
 * the console's task and workflow, as the first team on a repository of its
 * grant. Their ids come from the status route's `verify_runs` (or the post's
 * answer, before the next read), and each run is drawn with its state, linked.
 * swarm install's verify reads the same ids from the record: nobody pastes one.
 */
function ConsoleChecks({ state, onChanged }: { state: SetupState; onChanged: () => void }) {
  const act = useAction(onChanged)
  const [posted, setPosted] = useState<SetupVerifyRuns | null>(null)
  const runs = state.verify_runs ?? posted
  const task = useRunState(runs?.task ?? null, readTaskState)
  const workflow = useRunState(runs?.workflow ?? null, readWorkflowState)
  if (runs === null || (runs.task === null && runs.workflow === null)) {
    const first = firstWorkspace(state)
    const whose = isOnePerson(state) ? 'Your personal workspace' : 'The first team'
    const blocked =
      first === null
        ? isOnePerson(state)
          ? 'Your personal workspace is filed on the next read'
          : 'Create the first team first'
        : first.grant?.state === 'granted' && first.grant.repositories.length > 0
          ? undefined
          : `${whose} needs its repository grant first`
    return (
      <>
        <span className="ob-acts">
          <Button size="sm" busy={act.busy === 'verify-runs' ? 'Starting' : false} disabledReason={blocked} onClick={() => act.run('verify-runs', startSetupVerifyRuns, setPosted)}>
            Run the console checks
          </Button>
        </span>
        <ErrorLine error={act.error} />
      </>
    )
  }
  const items: Fact[] = []
  if (runs.task !== null) {
    const word = task !== null && 'word' in task ? (task.word as TaskState) : ('RUNNING' as TaskState)
    items.push(runFact('Console task', runs.task, agentPath({ id: runs.task, state: word }), task, runs.repository))
  }
  if (runs.workflow !== null) items.push(runFact('Console workflow', runs.workflow, `/workflows/${encodeURIComponent(runs.workflow)}`, workflow, runs.repository))
  // A failed or cancelled run is started again by the same route (audit 3, A8),
  // a fresh start per failed key up to VERIFY_RUN_RETRIES; verify says so too.
  const failedLeft = [
    { got: task, used: runs.retries.task },
    { got: workflow, used: runs.retries.workflow },
  ]
    .filter((r) => r.got !== null && 'word' in r.got && runStep(r.got.word) === 'failed')
    .map((r) => Math.max(0, VERIFY_RUN_RETRIES - r.used))
  if (failedLeft.length === 0) return <Facts items={items} label="Console checks" />
  const left = Math.min(...failedLeft)
  return (
    <>
      <Facts items={items} label="Console checks" />
      <span className="ob-acts">
        <Button
          size="sm"
          busy={act.busy === 'verify-runs' ? 'Starting' : false}
          disabledReason={left === 0 ? `It failed after ${VERIFY_RUN_RETRIES} fresh starts, so none is started again: read that run for why` : undefined}
          onClick={() => act.run('verify-runs', startSetupVerifyRuns, setPosted)}
        >
          Run the console checks again
        </Button>
        <span className="ur-hint">
          {left} of {VERIFY_RUN_RETRIES} fresh starts left
        </span>
      </span>
      <ErrorLine error={act.error} />
    </>
  )
}

function verifyRow(state: SetupState, onChanged: () => void, nowMs: number): Row {
  const v = state.engine?.verify ?? null
  const base = { key: 'verify', title: 'Verify, run by swarm install', by: 'swarm install' }
  const checks = <ConsoleChecks state={state} onChanged={onChanged} />
  if (v === null) {
    const ready = consoleDone(state)
    return {
      ...base,
      state: 'todo',
      line:
        state.finished !== null
          ? state.cli_running
            ? `The console steps are finished; swarm install starts verify on its next read: ${VERIFY_CHECKS}`
            : `The console steps are finished; run swarm install in a terminal to verify: ${VERIFY_CHECKS}`
          : ready
            ? `Every console step is done: hand over, and swarm install checks ${VERIFY_CHECKS}`
            : `Runs once every console step above is done; swarm install checks ${VERIFY_CHECKS}`,
      body: checks,
      act: state.finished === null && ready ? <VerifyFinish onChanged={onChanged} /> : undefined,
    }
  }
  const w = verifyWords(v, nowMs)
  if (v.result === 'passed') {
    return { ...base, state: 'done', line: `Verify passed: ${v.done} of ${v.total} checks${w.elapsed === null ? '' : `, in ${w.elapsed.replace(' elapsed', '')}`}`, body: checks }
  }
  if (v.result === 'failed') {
    return {
      ...base,
      state: 'failed',
      line: `Verify failed at ${w.progress}`,
      body: (
        <>
          <StepFailure cause={`The ${v.check ?? 'current'} check failed`} fix={<>swarm install printed which part failed and why; fix it, then run <code>swarm install</code> again: it re-runs verify from the failed check.</>} />
          {checks}
        </>
      ),
    }
  }
  return {
    ...base,
    state: 'in_progress',
    next: true,
    line: (
      <>
        <b>{w.progress}</b>
        {[w.elapsed, w.expected, w.deadline].filter((x): x is string => x !== null).map((x) => ` · ${x}`)}
      </>
    ),
    body: checks,
  }
}

function sendRow(state: SetupState): Row {
  const passed = state.engine?.verify?.result === 'passed'
  return {
    key: 'send',
    title: 'Send to your team',
    state: passed ? 'done' : 'todo',
    by: 'this page',
    line: passed ? 'The plugin line and the console URL, below' : 'Send to your team is unlocked when verify passes',
  }
}

function Checklist({ rows }: { rows: Row[] }) {
  return (
    <ol className="ob-list" aria-label="Setup steps">
      {rows.map((r) => (
        <li key={r.key} className={`ob-step is-${r.state}${r.next === true ? ' is-next' : ''}`} data-step={r.key}>
          <ToneMark tone={TONE[r.state]} hidden />
          <div className="ob-step-main">
            <p className="ob-step-h">
              <b>{r.title}</b>
              <span className="ob-state">
                {WORD[r.state]} · {r.by}
              </span>
              {r.next === true && <Chip>next</Chip>}
            </p>
            <p className="ur-hint ob-line">{r.line}</p>
            {r.body ?? null}
          </div>
          <div className="ob-step-act">{r.act ?? null}</div>
        </li>
      ))}
    </ol>
  )
}

/** "Send to your team": locked until verify passes; then the one plugin line, the console URL and day two. */
export function SendToTeam({ state }: { state: SetupState }) {
  const passed = state.engine?.verify?.result === 'passed'
  const hostname = state.hostname ?? state.engine?.hostname ?? null
  const [copied, setCopied] = useState(false)
  let body: ReactNode
  if (!passed) {
    body = <p className="ur-hint">Send to your team is unlocked when verify passes: the line below works only on a deployment verify has proven.</p>
  } else if (hostname === null) {
    body = <p className="ur-hint">The deployment’s hostname is not set on swarm-api (INSTALL_HOSTNAME), so there is no URL to send yet.</p>
  } else {
    const line = pluginLine(hostname, state.engine?.release ?? null)
    body = (
      <>
        <p className="ur-hint">One line for a teammate’s terminal; the plugin asks for nothing else. It reads the rest from https://{hostname}/.well-known/swarmcloud.</p>
        <CodeBlock
          title="Plugin install"
          lang="shell"
          text={line}
          action={
            <Button size="sm" kind="ghost" onClick={() => void navigator.clipboard?.writeText(line).then(() => setCopied(true))}>
              {copied ? 'Copied' : 'Copy'}
            </Button>
          }
        />
        <Facts
          items={[
            { state: 'done', name: 'Console', text: `https://${hostname}` },
            { state: 'done', name: 'Then', text: 'sc login, then /sc:submit in Claude Code' },
            { state: 'done', name: 'Day two', text: 'Admin › Setup › Health' },
          ]}
        />
      </>
    )
  }
  return (
    <div role="region" aria-label="Send to your team">
      <Card title="Send to your team" action={passed ? <Chip>verify passed</Chip> : <Chip faint>locked</Chip>}>
        {body}
      </Card>
    </div>
  )
}

const count = (n: number, one: string, many: string): string => `${n} ${n === 1 ? one : many}`

/**
 * The Capacity card: stage 0's `sizing` (one person or a team, and the
 * ceilings the person accepted), read-only. The numbers are swarm install's,
 * written to terraform/infra's `pool_limits` and the services' max instances;
 * nothing here changes them.
 */
export function CapacityCard({ sizing }: { sizing: SetupSizing | null }) {
  let body: ReactNode
  if (sizing === null) {
    body = <p className="ur-hint">The sizing is not recorded yet: swarm install asks for it, one person or a team, in its first stage.</p>
  } else {
    const l = sizing.pool_limits
    const services = Object.entries(sizing.service_max_instances)
    const items: Fact[] = [
      { state: 'done', name: 'For', text: sizing.mode === 'individual' ? 'One person' : sizing.members === null ? 'A team' : `A team of ${count(sizing.members, 'person', 'people')}` },
    ]
    if (l.global !== null) items.push({ state: 'done', name: 'Ceiling', text: `${l.global} agents at once across the deployment` })
    if (l.default_tenant !== null) items.push({ state: 'done', name: 'Per workspace', text: `${l.default_tenant} agents at once per workspace` })
    if (sizing.pool_accounts !== null) items.push({ state: 'done', name: 'Accounts to add', text: count(sizing.pool_accounts, 'Claude subscription account', 'Claude subscription accounts') })
    if (sizing.budget_usd !== null) items.push({ state: 'done', name: 'Budget', text: `$${sizing.budget_usd.toLocaleString('en-US')} a month` })
    if (services.length > 0) items.push({ state: 'done', name: 'Service ceilings', text: services.map(([name, n]) => `${name} ${count(n, 'instance', 'instances')}`).join(' · ') })
    body = <Facts items={items} label="Sizing" />
  }
  return (
    <div role="region" aria-label="Capacity">
      <Card title="Capacity" foot="Chosen in swarm install’s first stage; read-only here.">
        {body}
      </Card>
    </div>
  )
}

/** The installer side card: what swarm install is doing, from the engine document. */
export function InstallerCard({ state, now }: { state: SetupState; now?: number }) {
  const nowMs = useNow(now)
  const e = state.engine
  const beat = agoWords(e?.lease?.heartbeat ?? null, nowMs)
  const ready = e?.ready_at ?? null
  return (
    <Card title="The installer" foot={`Install record: Firestore swarm-install/${state.env || '<env>'}${(e?.release ?? null) === null ? '' : ` · ${e?.release}`}`}>
      <Facts
        items={[
          {
            state: ready !== null ? 'done' : state.cli_running ? 'in_progress' : 'failed',
            name: 'swarm install',
            text: ready !== null ? `finished, verify passed, ${agoWords(ready, nowMs) ?? ''}` : state.cli_running ? `waiting on this page · seen ${beat ?? 'just now'}` : `not running${beat === null ? '' : ` · last seen ${beat}`}`,
          },
          { state: e === null ? 'todo' : 'done', name: 'Release', text: e?.release ?? 'not recorded yet' },
        ]}
      />
    </Card>
  )
}

/** How long the page waits before it runs a self-check that came back still waiting again. */
export const SELF_CHECK_RETRY_MS = 30_000

/** Whether the App's self-check is due: installed, infra pass 3 has run, and no check has passed or failed yet. */
export function selfCheckDue(state: SetupState): boolean {
  const app = state.github_app
  if (app === null || app.id === null || app.installed_on === null || !pass3Ran(state)) return false
  return app.self_check === null || app.self_check.state === 'waiting_for_infra_pass_3'
}

/**
 * Audit 4d, F7: pass 4 and the hand-off wait on a passing self-check, so the
 * page runs it itself as soon as it is due, not on a click. A check that still
 * answers "waiting" (swarm-api's new revision not serving yet) is run again
 * on a later read, at most once per SELF_CHECK_RETRY_MS; a failed one waits
 * for its Retry.
 */
function useAutoSelfCheck(state: SetupState, act: ReturnType<typeof useAction>): void {
  const due = selfCheckDue(state)
  const last = useRef<number | null>(null)
  const { busy, run } = act
  useEffect(() => {
    if (!due || busy !== null) return
    const at = Date.now()
    if (last.current !== null && at - last.current < SELF_CHECK_RETRY_MS) return
    last.current = at
    run('self-check', runGitHubAppSelfCheck)
  }, [due, busy, run, state])
}

/**
 * The console's steps, in order, with the verify row and "Send to your
 * team": the panels SETUP-UI-2's page lays out. `now` fixes the clock (a
 * test, a capture); `onChanged` re-reads the state after any write.
 */
export function AdminSetupSteps({ state, now, onChanged }: { state: SetupState; now?: number; onChanged?: () => void }) {
  const nowMs = useNow(now)
  const changed = useRef(onChanged)
  changed.current = onChanged
  const reread = useCallback(() => changed.current?.(), [])
  const githubAct = useAction(reread)
  useAutoSelfCheck(state, githubAct)
  const rows = [
    cloudRow(state),
    githubRow(state, reread, githubAct),
    teamRow(state, reread, nowMs),
    accountsRow(state, reread, nowMs),
    addonsRow(state),
    verifyRow(state, reread, nowMs),
    sendRow(state),
  ]
  const nextKey = rows.find((r) => r.key !== 'addons' && r.state !== 'done')?.key
  for (const r of rows) if (r.key === nextKey) r.next = true
  const done = rows.filter((r) => r.state === 'done').length
  return (
    <>
      {!state.cli_running && (
        <Banner tone="warn" title="swarm install is not running">
          Run <code>swarm install</code> in a terminal to continue: the App’s switches and verify need it. The steps on this page stay usable meanwhile.
        </Banner>
      )}
      <SendToTeam state={state} />
      <CapacityCard sizing={state.sizing} />
      <Card className="ob-card" title="First run" foot="Each step is checked again on every read; nothing here is marked done by hand.">
        <p className="ob-summary">
          {done} of {rows.length} done · add-ons optional
        </p>
        <Checklist rows={rows} />
      </Card>
    </>
  )
}

// ---------------------------------------------------------------------------
// the GitHub App callback
// ---------------------------------------------------------------------------

/** Codes already posted in this tab: GitHub's code is single-use, so it is never posted twice. */
const POSTED = new Set<string>()

/**
 * `/admin/setup/github-app/callback`: GitHub sends the person here with
 * `code` and `state` after creating the App. The page posts them to the
 * conversion route once, takes them out of the address bar, and says the
 * secrets were stored. It shows the App's slug and id, never a secret: the
 * route answers `{id, slug}` only, and this reads nothing else.
 */
export function GitHubAppCallback({ search }: { search?: string }) {
  const [params] = useState(() => {
    const q = new URLSearchParams(search ?? (typeof window === 'undefined' ? '' : window.location.search))
    return { code: q.get('code'), state: q.get('state') }
  })
  const [result, setResult] = useState<Result<{ id: string | null; slug: string | null }> | null>(null)
  const started = useRef(false)
  useEffect(() => {
    const { code, state } = params
    if (code === null || state === null || started.current || POSTED.has(code)) return
    started.current = true
    POSTED.add(code)
    if (typeof window !== 'undefined') window.history.replaceState(window.history.state, '', window.location.pathname)
    void convertGitHubApp(code, state).then(setResult)
  }, [params])

  const back = (
    <ButtonLink size="sm" href="/admin/setup">
      Back to Admin › Setup
    </ButtonLink>
  )
  if (params.code === null || params.state === null) {
    return (
      <Card title="GitHub App">
        <Banner tone="warn" title="This page needs the code GitHub sends back">
          It opens after GitHub creates the App from Admin › Setup. Create the GitHub App again from there.
        </Banner>
        <span className="ob-acts">
          <ButtonLink size="sm" kind="primary" href="/admin/setup">
            Create the GitHub App again
          </ButtonLink>
        </span>
      </Card>
    )
  }
  if (result === null || result.status === 'loading') {
    return (
      <Card title="GitHub App">
        <p className="ur-hint">Exchanging GitHub’s one-time code and storing the App’s secrets in Secret Manager…</p>
      </Card>
    )
  }
  if (result.status === 'error' || result.status === 'empty') {
    const message = result.status === 'error' ? result.error.message : 'swarm-api answered with nothing'
    return (
      <Card title="GitHub App">
        <Banner tone="bad" title="GitHub’s code could not be exchanged" actions={back}>
          {message}. The code works once and expires within an hour; create the GitHub App again from Admin › Setup.
        </Banner>
      </Card>
    )
  }
  const { id, slug } = result.data
  return (
    <Card title="GitHub App created" action={back}>
      <Facts
        items={[
          { state: 'done', name: 'Created from the manifest', text: `${slug ?? 'the App'}${id === null ? '' : ` (app id ${id})`}` },
          {
            state: 'done',
            name: 'Secrets stored',
            text: 'the private key, the client secret and the webhook secret went from GitHub’s answer straight into Secret Manager; none is shown here or anywhere',
          },
          { state: 'todo', name: 'Next', text: 'install the App on your organisation, then come back to Admin › Setup' },
        ]}
      />
      {slug !== null && (
        <span className="ob-acts">
          <ButtonLink size="sm" kind="primary" href={`https://github.com/apps/${encodeURIComponent(slug)}/installations/new`}>
            Install the App
          </ButtonLink>
        </span>
      )}
    </Card>
  )
}
