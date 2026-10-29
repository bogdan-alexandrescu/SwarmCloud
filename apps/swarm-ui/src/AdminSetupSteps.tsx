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
  loadSetup,
  mintSetupLink,
  runGitHubAppSelfCheck,
  startGitHubApp,
  type ApiError,
  type Result,
  type SetupAccount,
  type SetupLink,
  type SetupState,
  type SetupTeam,
  type SetupVerify,
} from './api'
import { Banner, Button, ButtonLink, Card, Chip, CodeBlock, ToneMark, type MarkTone } from './components'
import './styles/onboarding.css'

/** The marketplace the plugin is installed from: the release bucket (DX3 B), as `swarm install` prints it. */
export const PLUGIN_MARKETPLACE = 'https://storage.googleapis.com/swarmcloud/marketplace.json'

/** How often the page re-reads while it is open: the verify row's refresh (§5.1). */
export const SETUP_REFRESH_MS = 5_000

/** How often the GitHub step asks whether the App is on the org yet. */
const INSTALL_POLL_MS = 10_000

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

/** The one line a teammate runs; the deployment URL is its only value. */
export function pluginLine(hostname: string): string {
  return `claude plugin marketplace add ${PLUGIN_MARKETPLACE} && claude plugin install sc@swarmcloud --config deployment_url=https://${hostname}`
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
export function StepFailure({ cause, fix, onRetry, busy }: { cause: string; fix: ReactNode; onRetry?: () => void; busy?: boolean }) {
  return (
    <div className="ob-issue" role="alert">
      <b>{cause}</b>
      <span className="ur-hint">Fix: {fix}</span>
      {onRetry !== undefined && (
        <span className="ob-acts">
          <Button size="sm" onClick={onRetry} busy={busy === true ? 'Retrying' : false}>
            Retry
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
    const switched = state.engine?.stages[9]?.state === 'switches_on' || state.engine?.stages[9]?.state === 'done'
    facts.push({
      state: app.self_check === null ? (switched ? 'todo' : 'in_progress') : app.self_check.ok ? 'done' : 'failed',
      name: 'Self-check',
      text:
        app.self_check !== null
          ? app.self_check.ok
            ? 'the App’s key signs and GitHub answers as the App'
            : app.self_check.reason ?? 'GitHub did not answer as the App'
          : switched
            ? 'swarm install switched the App on; run the self-check'
            : 'waits on swarm install’s infra pass 3, which switches the App on',
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
  const selfCheck = (label: string, kind: 'ghost' | 'secondary' = 'secondary') => (
    <Button size="sm" kind={kind === 'ghost' ? 'ghost' : undefined} busy={act.busy === 'self-check' ? 'Checking' : false} onClick={() => act.run('self-check', runGitHubAppSelfCheck)}>
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
  if (app.self_check !== null && !app.self_check.ok) {
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
  if (app.self_check === null) {
    return {
      key: 'github',
      title: 'GitHub App',
      state: 'in_progress',
      by: 'this page',
      line: `Installed on ${app.installed_on}; the self-check is next`,
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

function TeamForm({ onChanged }: { onChanged: () => void }) {
  const act = useAction(onChanged)
  const [group, setGroup] = useState('')
  const [name, setName] = useState('')
  const [admin, setAdmin] = useState('')
  const [invites, setInvites] = useState('')
  const field = (label: string, value: string, set: (v: string) => void, hint: string, placeholder: string) => (
    <label className="c-field">
      <span className="c-lbl">{label}</span>
      <input className="c-inp" value={value} placeholder={placeholder} onChange={(e) => set(e.target.value)} />
      <p className="c-hint">{hint}</p>
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
    )
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        submit()
      }}
    >
      {field('Workspace group', group, setGroup, 'A Google Workspace group; its members are the team', 'engineering@your-domain')}
      {field('Workspace name', name, setName, 'Shown in the console; the group’s name when left empty', 'engineering')}
      {field('First workspace admin', admin, setAdmin, 'Administers the team’s workspace', 'name@your-domain')}
      {field('Invite', invites, setInvites, 'Each gets a sign-in link; nobody pastes anything', 'one or more emails')}
      <span className="ob-acts">
        <Button size="sm" kind="primary" type="submit" busy={act.busy === 'team' ? 'Creating' : false} disabledReason={group.trim().length < 3 ? 'Name the Workspace group first' : undefined}>
          Create the team
        </Button>
      </span>
      <ErrorLine error={act.error} />
    </form>
  )
}

function teamFacts(team: SetupTeam, signedIn: boolean, nowMs: number): Fact[] {
  const ws = typeof team.progress.workspace_state === 'string' ? team.progress.workspace_state : null
  const git = typeof team.progress.git_link === 'object' && team.progress.git_link !== null ? (team.progress.git_link as Record<string, unknown>) : null
  const smoke = team.smoke
  return [
    { state: 'done', name: 'Tenant record', text: `${team.workspace} · from ${team.group}, checked by membership per registered group` },
    {
      state: ws === null ? 'in_progress' : FAILED_WORKSPACE.has(ws) ? 'failed' : ws === WORKSPACE_READY ? 'done' : 'in_progress',
      name: 'Namespace, quota, verify-keys and worker identity',
      text: ws === null ? 'swarm-workspace-apply has not reported yet' : `swarm-workspace-apply: ${ws}`,
    },
    { state: team.first_admin === null ? 'todo' : 'done', name: 'First admin', text: team.first_admin ?? 'not named' },
    { state: team.invites.length === 0 ? 'todo' : 'done', name: 'Invited', text: team.invites.length === 0 ? 'nobody yet' : team.invites.join(', ') },
    {
      state: git === null ? 'todo' : 'in_progress',
      name: 'Team git slot',
      text:
        git === null
          ? 'served by the GitHub App; a one-time link lets its owner enter a token instead'
          : `one-time link for ${String(git.owner ?? 'its owner')}, ${untilWords(typeof git.expires_at === 'string' ? git.expires_at : null, nowMs) ?? 'open'}`,
    },
    {
      state: smoke === null ? 'todo' : smoke.state === 'passed' ? 'done' : smoke.state === 'failed' ? 'failed' : 'in_progress',
      name: 'Smoke agent',
      text:
        smoke === null
          ? signedIn
            ? 'starts now that a pool account is signed in'
            : 'waits for a pool account (the next step): it needs one to run'
          : `${smoke.state}${smoke.detail === null ? '' : ` · ${smoke.detail}`}`,
    },
  ]
}

function teamRow(state: SetupState, onChanged: () => void, nowMs: number): Row {
  const team = state.teams[0]
  if (team === undefined) {
    return {
      key: 'team',
      title: 'First team',
      state: 'todo',
      by: 'this page',
      line: 'A Workspace group becomes the first workspace: the same call swarm team add makes',
      body: <TeamForm onChanged={onChanged} />,
    }
  }
  const ws = typeof team.progress.workspace_state === 'string' ? team.progress.workspace_state : null
  const signedIn = state.accounts.some((a) => a.signed_in_at !== null)
  const failed = ws !== null && FAILED_WORKSPACE.has(ws)
  return {
    key: 'team',
    title: 'First team',
    state: failed ? 'failed' : 'done',
    by: 'this page',
    line: `${team.workspace} from ${team.group}${team.first_admin === null ? '' : ` · admin ${team.first_admin}`} · ${team.invites.length} invited`,
    body: (
      <>
        <Facts items={teamFacts(team, signedIn, nowMs)} label="First team" />
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

function accountFact(a: SetupAccount, nowMs: number): Fact {
  if (a.signed_in_at !== null) {
    return { state: 'done', name: a.label, text: `${a.owner ?? 'its owner'} · signed in ${agoWords(a.signed_in_at, nowMs) ?? ''} · in the first team’s pool` }
  }
  const until = untilWords(a.link_expires, nowMs)
  if (until === 'expired') return { state: 'failed', name: a.label, text: `${a.owner ?? 'its owner'}’s one-time link expired; send a new one` }
  return { state: 'in_progress', name: a.label, text: `waiting for ${a.owner ?? 'its owner'} to open their one-time link${until === null ? '' : `, ${until}`} · or they run sc account add --label ${a.label}` }
}

function AccountLinkForm({ onChanged }: { onChanged: () => void }) {
  const act = useAction(onChanged)
  const [label, setLabel] = useState('')
  const [owner, setOwner] = useState('')
  const [minted, setMinted] = useState<SetupLink | null>(null)
  const [copied, setCopied] = useState(false)
  const shown = minted === null ? null : minted.url ?? minted.path
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        setCopied(false)
        act.run('link', () => mintSetupLink({ kind: 'pool-account', label: label.trim(), owner: owner.trim() }), setMinted)
      }}
    >
      <label className="c-field">
        <span className="c-lbl">Account label</span>
        <input className="c-inp" value={label} placeholder="lowercase, e.g. ravi" onChange={(e) => setLabel(e.target.value)} />
      </label>
      <label className="c-field">
        <span className="c-lbl">Its owner</span>
        <input className="c-inp" value={owner} placeholder="name@your-domain" onChange={(e) => setOwner(e.target.value)} />
        <p className="c-hint">A member of the first team’s group; the link opens for them only, once, within 24 h</p>
      </label>
      <span className="ob-acts">
        <Button size="sm" kind="primary" type="submit" busy={act.busy === 'link' ? 'Sending' : false} disabledReason={label.trim() === '' || owner.trim() === '' ? 'Name the account and its owner first' : undefined}>
          Send a sign-in link
        </Button>
      </span>
      {shown !== null && minted !== null && (
        <CodeBlock
          title={`One-time link for ${minted.owner}, shown once`}
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
  if (state.teams.length === 0) {
    return {
      key: 'accounts',
      title: 'Claude accounts',
      state: 'todo',
      by: 'this page',
      line: 'Comes after the first team: each pool account is owned by it, so its owner must be a member of the first team’s group',
    }
  }
  const signed = state.accounts.filter((a) => a.signed_in_at !== null).length
  const st: StepState = signed > 0 ? 'done' : state.accounts.length > 0 ? 'in_progress' : 'todo'
  return {
    key: 'accounts',
    title: 'Claude accounts',
    state: st,
    by: 'this page',
    line:
      state.accounts.length === 0
        ? 'Each subscription’s owner signs in once, through a one-time link or sc account add, into the first team’s pool'
        : `Pool: ${signed} of ${state.accounts.length} signed in`,
    body: (
      <>
        {state.accounts.length > 0 && <Facts items={state.accounts.map((a) => accountFact(a, nowMs))} label="Pool accounts" />}
        <AccountLinkForm onChanged={onChanged} />
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

/** Every required console step is done: the App installed and checked, a first team, a pool account signed in. */
function consoleDone(state: SetupState): boolean {
  const app = state.github_app
  return app !== null && app.installed_on !== null && app.self_check?.ok === true && state.teams.length > 0 && state.accounts.some((a) => a.signed_in_at !== null)
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

function verifyRow(state: SetupState, onChanged: () => void, nowMs: number): Row {
  const v = state.engine?.verify ?? null
  const base = { key: 'verify', title: 'Verify, run by swarm install', by: 'swarm install' }
  if (v === null) {
    const ready = consoleDone(state)
    return {
      ...base,
      state: 'todo',
      line:
        state.finished !== null
          ? state.cli_running
            ? 'The console steps are finished; swarm install starts verify on its next read'
            : 'The console steps are finished; run swarm install in a terminal to verify'
          : ready
            ? 'Every console step is done: hand over, and swarm install runs smoke, GKE proof, race, Scheduler targets, digests and four end-to-end runs'
            : 'Runs once every console step above is done',
      act: state.finished === null && ready ? <VerifyFinish onChanged={onChanged} /> : undefined,
    }
  }
  const w = verifyWords(v, nowMs)
  if (v.result === 'passed') {
    return { ...base, state: 'done', line: `Verify passed: ${v.done} of ${v.total} checks${w.elapsed === null ? '' : `, in ${w.elapsed.replace(' elapsed', '')}`}` }
  }
  if (v.result === 'failed') {
    return {
      ...base,
      state: 'failed',
      line: `Verify failed at ${w.progress}`,
      body: <StepFailure cause={`The ${v.check ?? 'current'} check failed`} fix={<>swarm install printed which part failed and why; fix it, then run <code>swarm install</code> again: it re-runs verify from the failed check.</>} />,
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
    const line = pluginLine(hostname)
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
            { state: 'done', name: 'Day two', text: 'swarm doctor · Admin › Setup › Health' },
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
