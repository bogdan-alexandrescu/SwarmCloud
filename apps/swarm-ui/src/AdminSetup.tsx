/**
 * ADMIN › SETUP, THE PAGE (DAYONE-SETUP-UI-2; part of #2688;
 * docs/design/day-one-plan.md §5.1, D26, frames B05-B09a, B11, B12b, B15).
 *
 * swarm install did the cloud part from a terminal and handed over to this
 * page for the people part. The page is P1-B's: the setup strip (`cfg-*`,
 * the console's own Workspace setup strip), the lead (`ws-lead`, Admin's
 * `AdminLead`) naming the next step, and beside it the checklist and side
 * cards SETUP-UI-1 built (`AdminSetupSteps`, `InstallerCard`). Everything is
 * read from `GET /v1/admin/setup` every 5 s (`useSetupState`); nothing here is
 * marked done by hand.
 *
 * THE ORDER IS GitHub App -> first team -> Claude pool accounts -> verify ->
 * Send to your team (BGD, 2026-10-29): the strip draws the steps in the
 * checklist's order. The P1-B frames drew the accounts before the team.
 *
 * WAITING FOR THE CLI (R8): when no CLI holds the install lease the page says
 * to run swarm install in a terminal to continue (the checklist's banner),
 * and the strip's count says the same; every console step stays usable.
 *
 * DAY TWO (B15): once the first team exists, Add a team is the same team
 * panel as its own flow, with the `swarm team add` line printed beside it
 * (`AdminSetupTeam`).
 *
 * ONE PERSON (decision 7; DAYONE-SETUP-UI-5A, audit finding 12): the strip's
 * first-workspace step is the route's `personal` row, the installer's personal
 * workspace, and the accounts follow it; no step waits on a team one person
 * never makes.
 *
 * The Health page (`/admin/setup/health`) and the GitHub App callback
 * (`/admin/setup/github-app/callback`) are framed here too, so App.tsx routes
 * three screens from one module (SETUP-UI-3).
 */
import { type ReactNode } from 'react'
import { type Result, type SetupState } from './api'
import { AdminLead, type AdminLeadTone } from './AdminSettings'
import { AdminSetupHealth } from './AdminSetupHealth'
import {
  AdminSetupSteps,
  consoleDone,
  firstWorkspace,
  GitHubAppCallback,
  InstallerCard,
  isOnePerson,
  signedInCount,
  smokeHold,
  spanWords,
  useSetupState,
  WAITING_FOR_PASS_3,
  type StepState,
} from './AdminSetupSteps'
import { AdminSetupTeam } from './AdminSetupTeam'
import { Banner, Button, Card } from './components'
import { PageHead } from './Shell'
import './styles/config.css'
import './styles/onboarding.css'
import './styles/workspace-settings.css'
import './styles/admin-setup.css'

/** The cloud stages swarm install finishes before it hands over (§5.1: 0-8, 10, 13), as the checklist's cloud row counts them. */
const CLI_STAGES = [0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 13]

/** The strip's class for a step's state: `cfg-step.is-unknown` is the strip's not-started step. */
const STRIP_CLASS: Record<StepState, string> = { done: 'is-done', in_progress: 'is-in_progress', failed: 'is-failed', todo: 'is-unknown' }

/** The workspace record's states the apply does not recover from on its own (swarm_api/workspaces.py STATES). */
const FAILED_WORKSPACE = new Set(['failed', 'denied'])

interface StripStep {
  key: 'cloud' | 'github' | 'team' | 'accounts' | 'verify' | 'send'
  label: string
  state: StepState
  /** What is true of the step now, in a few words. */
  line: string
}

const workspaceState = (state: SetupState): string | null => {
  const ws = firstWorkspace(state)?.progress.workspace_state
  return typeof ws === 'string' ? ws : null
}

/**
 * The strip's steps, from the same record the checklist reads and by the
 * same rules: a step is done in the strip exactly when its checklist row is.
 */
export function stripSteps(state: SetupState): StripStep[] {
  const stages = state.engine?.stages ?? []
  const cloudDone = CLI_STAGES.filter((n) => stages[n]?.state === 'done').length
  const app = state.github_app
  const org = state.github_org ?? 'your organisation'
  const github: StepState =
    app === null || !app.started ? 'todo' : app.self_check?.state === 'failed' ? 'failed' : app.installed_on !== null && app.self_check?.ok === true ? 'done' : 'in_progress'
  const onePerson = isOnePerson(state)
  const team = firstWorkspace(state)
  const ws = workspaceState(state)
  const signed = signedInCount(state)
  // One person's first workspace is done once its apply says ready, a team's once it exists, and either only
  // once its smoke has succeeded (the checklist's rows; the hand-over waits for the smoke).
  const through = team !== null && !(ws !== null && FAILED_WORKSPACE.has(ws)) && (!onePerson || ws === 'ready')
  const smoke = through ? smokeHold(team.smoke, signed > 0) : null
  const teamState: StepState =
    team === null ? (onePerson ? 'in_progress' : 'todo') : ws !== null && FAILED_WORKSPACE.has(ws) ? 'failed' : !through ? 'in_progress' : smoke ?? 'done'
  const smokeWords = smoke === null ? '' : `, smoke ${team?.smoke?.state ?? 'starting'}`
  const accounts: StepState = team === null && !onePerson ? 'todo' : signed > 0 ? 'done' : state.accounts.length > 0 ? 'in_progress' : 'todo'
  const v = state.engine?.verify ?? null
  const verify: StepState = v === null ? 'todo' : v.result === 'passed' ? 'done' : v.result === 'failed' ? 'failed' : 'in_progress'
  return [
    {
      key: 'cloud',
      label: 'Cloud',
      state: cloudDone === CLI_STAGES.length ? 'done' : 'in_progress',
      line: `by swarm install, ${cloudDone} of ${CLI_STAGES.length} stages`,
    },
    {
      key: 'github',
      label: 'GitHub App',
      state: github,
      line:
        github === 'todo'
          ? 'not yet'
          : github === 'failed'
            ? `self-check failed${app?.self_check?.reason ? `: ${app.self_check.reason}` : ''}`
            : app?.installed_on === null || app?.installed_on === undefined
              ? `created, install it on ${org}`
              : github === 'done'
                ? `installed on ${app.installed_on}`
                : app.self_check?.state === 'waiting_for_infra_pass_3'
                  ? `installed on ${app.installed_on}, ${WAITING_FOR_PASS_3}`
                  : `installed on ${app.installed_on}, self-check next`,
    },
    {
      key: 'team',
      label: onePerson ? 'First workspace' : 'First team',
      state: teamState,
      line: team === null ? (onePerson ? 'your personal workspace, filed on the next read' : 'not yet') : `${team.workspace}${ws === null ? '' : `, ${ws}`}${smokeWords}`,
    },
    {
      key: 'accounts',
      label: 'Claude accounts',
      state: accounts,
      line: team === null && !onePerson ? 'after the first team' : state.accounts.length === 0 ? 'not yet' : `${signed} of ${state.accounts.length} signed in`,
    },
    {
      key: 'verify',
      label: 'Verify',
      state: verify,
      line:
        v === null
          ? state.finished !== null
            ? 'handed over to swarm install'
            : 'after the console steps'
          : v.result === 'passed'
            ? `passed, ${v.done} of ${v.total} checks`
            : v.result === 'failed'
              ? `failed at ${v.check ?? 'a check'}`
              : `${v.check ?? 'starting'}, ${v.done} of ${v.total}`,
    },
    {
      key: 'send',
      label: 'Send to your team',
      state: v?.result === 'passed' ? 'done' : 'todo',
      line: v?.result === 'passed' ? 'the plugin line is ready' : 'unlocked when verify passes',
    },
  ]
}

function StepMark({ state }: { state: StepState }) {
  return (
    <svg className="cfg-mark" viewBox="0 0 20 20" aria-hidden>
      <circle cx={10} cy={10} r={8.5} />
      {state === 'done' && <path d="m6 10.4 2.7 2.7L14.2 7.6" />}
    </svg>
  )
}

/** The strip: the CLI's step, then the console's, the next one grounded; compact on a phone (B11). */
export function SetupStrip({ state }: { state: SetupState }) {
  const steps = stripSteps(state)
  const done = steps.filter((s) => s.state === 'done').length
  const next = steps.find((s) => s.state !== 'done') ?? null
  const ready = state.engine?.ready_at ?? null
  const count =
    next === null
      ? ready === null
        ? 'every step done and verified'
        : `swarm install saw this at ${new Date(ready).toLocaleTimeString()}`
      : state.cli_running
        ? 'the CLI is waiting on this page'
        : 'swarm install is not running: run it in a terminal'
  return (
    <section className={`cfg-strip asm-strip${next === null ? ' is-complete' : ''}`} aria-label="Install setup">
      <div className="cfg-compact">
        <div className="cfg-compact-h">
          <b>
            Setup {done} of {steps.length}
          </b>
        </div>
        <small>{next === null ? 'Every step done' : `Next: ${next.label}, ${next.line}`}</small>
      </div>
      <ol className="cfg-steps" style={{ ['--cfg-n' as string]: steps.length }}>
        {steps.map((s, i) => (
          <li key={s.key} data-step={s.key} className={`cfg-step ${STRIP_CLASS[s.state]}${s === next ? ' is-next' : ''}`}>
            <StepMark state={s.state} />
            <div className="cfg-step-main">
              <b>
                {i + 1}. {s.label}
              </b>
              <small>{s === next ? `Next: ${s.line}` : s.line}</small>
            </div>
          </li>
        ))}
      </ol>
      <div className="cfg-count">
        <b>
          {done} of {steps.length} done
        </b>
        <small>{count}</small>
      </div>
    </section>
  )
}

/** The lead: the next step, in a sentence, with what the person can do about it. */
export function SetupLead({ state, now }: { state: SetupState; now?: number }) {
  const steps = stripSteps(state)
  const i = steps.findIndex((s) => s.state !== 'done')
  const kicker = (n: number) => `Step ${n + 1} of ${steps.length} · next`
  const lead = (tone: AdminLeadTone, title: string, body: ReactNode) => <AdminLead tone={tone} kicker={i === -1 ? 'Setup is complete' : kicker(i)} title={title} body={body} />
  const hostname = state.hostname ?? state.engine?.hostname ?? null
  const cli = state.cli_running ? 'swarm install is waiting and verifies each step as you finish it.' : 'The App’s switches and verify wait for swarm install in a terminal; the steps here stay usable.'
  const step = i === -1 ? null : steps[i]!
  if (step === null) {
    return lead(
      'ok',
      hostname === null ? 'SwarmCloud is ready' : `SwarmCloud is ready at ${hostname}`,
      'Every step done and verified. Send the plugin line to your team; Admin › Setup › Health shows the hourly doctor’s findings from here on, and swarm doctor checks this machine and the deployment from a terminal.',
    )
  }
  const tone: AdminLeadTone = step.state === 'failed' ? 'warn' : 'next'
  switch (step.key) {
    case 'cloud':
      return lead(tone, `swarm install is doing the cloud part: ${step.line.replace('by swarm install, ', '')}`, 'This page finishes the people part once swarm install hands over to it.')
    case 'github':
      return lead(tone, `GitHub App: ${step.state === 'todo' ? 'create it from the manifest' : step.line}`, `swarm install did the cloud part; this page finishes the people part. ${cli}`)
    case 'team': {
      const smoke = firstWorkspace(state)?.smoke ?? null
      const ws = workspaceState(state)
      // The step is held by its smoke once the workspace itself is through (`stripSteps`).
      if (firstWorkspace(state) !== null && (ws === null || !FAILED_WORKSPACE.has(ws)) && (!isOnePerson(state) || ws === 'ready')) {
        return lead(
          tone,
          `${isOnePerson(state) ? 'First workspace' : 'First team'}: ${step.line}`,
          smoke?.state === 'failed'
            ? `Its smoke agent failed: the row below says why and retries it, and the hand-over to swarm install waits for it to succeed. ${cli}`
            : `Its smoke agent runs on the first signed-in Claude account, and the hand-over to swarm install waits for it to succeed. ${cli}`,
        )
      }
      if (isOnePerson(state)) {
        return lead(tone, `First workspace: ${step.line}`, `One-person install: your personal workspace is the first workspace, so no team or invites are needed; the Claude pool accounts come next. ${cli}`)
      }
      return lead(tone, step.state === 'failed' ? `First team: ${step.line}` : 'First team: name the Workspace group that becomes the first workspace', `Its members are the team; the Claude pool accounts come next and are owned by it. ${cli}`)
    }
    case 'accounts':
      return lead(
        tone,
        `Claude accounts: ${step.line}`,
        `Each subscription’s owner signs in once, through the one-time link this page sends; ${isOnePerson(state) ? 'your personal workspace’s' : 'the first team’s'} smoke agent runs on the first one. ${cli}`,
      )
    case 'verify': {
      const v = state.engine?.verify ?? null
      if (v === null && state.finished === null && !consoleDone(state)) {
        // The hand-over button shows only once every console step is done, the first workspace's smoke with them.
        return lead(tone, 'Verify waits for the first workspace’s smoke to succeed', `The hand-over to swarm install is offered once it has. ${cli}`)
      }
      if (v === null) {
        return lead(
          tone,
          state.finished !== null ? 'Verify: handed over to swarm install' : 'Every console step is done: hand over to swarm install',
          state.cli_running ? 'swarm install runs smoke, GKE proof, race, Scheduler targets, digests and four end-to-end runs.' : 'Run swarm install in a terminal: it runs verify.',
        )
      }
      if (v.result === 'failed') return lead('warn', `Verify failed at ${v.check ?? 'a check'}`, 'swarm install printed which part failed and why; fix it, then run swarm install again.')
      const left = v.expected_end === null ? null : v.expected_end - (now ?? Date.now()) / 1000
      return lead(tone, `Verify: ${step.line}`, left !== null && left > 0 ? `About ${spanWords(left)} left. Send to your team unlocks when it passes.` : 'Send to your team unlocks when it passes.')
    }
    case 'send':
      return lead(tone, 'Send to your team', 'Unlocked when verify passes.')
  }
}

function SetupRead({ result, reload, children }: { result: Result<SetupState>; reload: () => void; children: (state: SetupState) => ReactNode }) {
  if (result.status === 'loading') {
    return (
      <Card title="Setup">
        <p className="ur-hint">Reading the install record…</p>
      </Card>
    )
  }
  if (result.status === 'error') {
    const admin = result.error.kind === 'admin_required'
    return (
      <Banner
        tone={admin ? 'warn' : 'bad'}
        title={admin ? 'Admin › Setup is for platform admins' : 'The install record could not be read'}
        actions={
          <Button size="sm" onClick={reload}>
            Read again
          </Button>
        }
      >
        {result.error.message}
      </Banner>
    )
  }
  if (result.status === 'empty') {
    return (
      <Banner tone="info" title="No install record yet">
        swarm install writes it when it starts. Run swarm install in a terminal to begin.
      </Banner>
    )
  }
  return <>{children(result.data)}</>
}

/** The page's body for one read of the record: exported so a test or a capture can draw a given state. */
export function AdminSetupPage({ state, now, onChanged }: { state: SetupState; now?: number; onChanged?: () => void }) {
  return (
    <>
      <SetupStrip state={state} />
      <SetupLead state={state} now={now} />
      <div className="ws-grid is-setup">
        <div className="ws-col">
          <AdminSetupSteps state={state} now={now} onChanged={onChanged} />
        </div>
        <div className="ws-col">
          <InstallerCard state={state} now={now} />
          <Card title="Health" foot="The hourly doctor’s checks, read-only, on the live deployment; swarm doctor checks this machine and the deployment from a terminal.">
            <p className="ur-hint">
              Once setup is done, the scheduled doctor’s findings and each one’s repair are on <a href="/admin/setup/health">Admin › Setup › Health</a>.
            </p>
          </Card>
        </div>
      </div>
      {state.teams.length > 0 && <AdminSetupTeam teams={state.teams} onChanged={onChanged} />}
    </>
  )
}

/**
 * `/admin/setup`, and its GitHub App callback page when `callback` holds the
 * query GitHub sent the person back with. `now` fixes the clock (a test, a
 * capture).
 */
export function AdminSetupScreen({ now, callback }: { now?: number; callback?: string }) {
  if (callback !== undefined) return <AdminSetupCallbackScreen search={callback} />
  return <SetupFirstRunScreen now={now} />
}

function SetupFirstRunScreen({ now }: { now?: number }) {
  // The Setup tab's page: the record read every 5 s, and drawn. (No doc
  // comment above it: test_nav_headings_agree.py follows AdminSetupScreen's
  // last return here only when that return ends its body.)
  const { result, reload } = useSetupState()
  return (
    <div className="ur-page asm-page">
      <PageHead title="Setup" />
      <SetupRead result={result} reload={reload}>
        {(state) => <AdminSetupPage state={state} now={now} onChanged={reload} />}
      </SetupRead>
    </div>
  )
}

/** `/admin/setup/health`: the latest scheduled doctor run (B12b). */
export function AdminSetupHealthScreen({ now }: { now?: number }) {
  return (
    <div className="ur-page asm-page">
      <PageHead title="Health" />
      <p className="ur-sub">
        The hourly doctor’s checks, read-only, on the live deployment: the scheduled run’s findings, each with its repair. From a terminal, swarm doctor checks this machine and the deployment against the manifest. The install steps are on <a href="/admin/setup">Admin › Setup</a>.
      </p>
      <AdminSetupHealth now={now} />
    </div>
  )
}

/** `/admin/setup/github-app/callback`: GitHub sends the person here with `code` and `state` (`search`, read off the address on arrival). */
export function AdminSetupCallbackScreen({ search }: { search: string }) {
  return (
    <div className="ur-page asm-page">
      <PageHead title="GitHub App" />
      <GitHubAppCallback search={search} />
    </div>
  )
}
