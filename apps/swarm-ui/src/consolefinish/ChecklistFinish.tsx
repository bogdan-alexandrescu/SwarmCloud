/**
 * ADMIN › SETUP'S CONSOLE FINISH: A CHECKLIST BESIDE A WORKING PANEL (part of
 * #2688; the owner's pick for the Day One console finish, 2026-11-02;
 * docs/design/day-one-plan.md §5.1).
 *
 * Everything on one screen. Across the top, one status line says what swarm
 * install is doing now, from the install record (its lease, its stages, its
 * verify). On the left, a compact checklist of eight steps: one line per step
 * with its mark, its name, who acts now (you, a teammate, Google, GitHub,
 * swarm install or the console) and since when; the waits on other people
 * stay listed under their step, so the person works on another step
 * meanwhile. Beside it, the selected step's working panel: who it waits on,
 * then the step's own controls, which are today's rows (AdminSetupSteps.tsx)
 * unchanged. The panel follows the next step the person can act on, unless
 * they picked another; one click goes back to following.
 *
 * EIGHT LINES, NOT SEVEN: the smoke agent has its own line between the
 * Claude accounts and verify, with its own mark, actor and Retry, so the
 * first team is done once its workspace is ready. The hand-over is still
 * offered only once the smoke has succeeded (`consoleDone`, the verify row's).
 *
 * NOTHING HERE IS MARKED BY HAND: every mark, actor and time is derived from
 * `GET /v1/admin/setup`, read every 5 s. A time the record does not hold is
 * drawn as "not recorded", never as now or zero; how long a wait usually
 * takes is the route's `typical_s`, and without it the page says so rather
 * than guess.
 */
import { useCallback, useRef, useState, type ReactNode } from 'react'
import {
  CapacityCard,
  ConsoleChecks,
  Facts,
  StepFailure,
  accountSignedIn,
  accountsRow,
  addonsRow,
  consoleDone,
  firstWorkspace,
  githubRow,
  isOnePerson,
  pluginLine,
  signInRow,
  signedInCount,
  spanWords,
  teamRow,
  useAction,
  useAutoSelfCheck,
  useNow,
  verifyRow,
  type Row,
  type StepState,
} from '../AdminSetupSteps'
import { restartSetupSmoke, type SetupState } from '../api'
import { Button, Chip, CodeBlock } from '../components'
import '../styles/admin-setup.css'
import '../styles/console-finish.css'

/** Who acts on a step now. `console` is this deployment's own machinery (a workspace build, the smoke agent). */
export type Actor = 'you' | 'teammate' | 'google' | 'github' | 'install' | 'console'

const ACTOR: Record<Actor, string> = {
  you: 'You',
  teammate: 'A teammate',
  google: 'Google',
  github: 'GitHub',
  install: 'swarm install',
  console: 'The console',
}

/** One thing a step waits on: who, doing what, since when, and how long it usually takes (null: not recorded). */
interface Wait {
  who: Actor
  name?: string
  what: string
  since: number | null
  usual: string | null
}

export type LineKey = 'cloud' | 'sign-in-client' | 'github' | 'team' | 'accounts' | 'smoke' | 'verify' | 'send'

/** One line of the compact checklist. */
export interface Line {
  key: LineKey
  name: string
  state: StepState
  /** Who acts now; null once done, or while the step waits on an earlier one. */
  actor: Actor | null
  /** When the current actor's turn started (ms), or when the step was done; null when the record holds no time. */
  since: number | null
  /** A few words: what is true of it now. */
  now: string
  waits: Wait[]
  /** How long the actor's part usually takes, from the route's `typical_s`. */
  usual?: string
}

const ms = (iso: string | null | undefined): number | null => {
  if (iso === null || iso === undefined) return null
  const t = Date.parse(iso)
  return Number.isNaN(t) ? null : t
}

const clock = (t: number): string => new Date(t).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', timeZone: 'UTC' })

/** "since 12:04 · 6 min", or the honest "since: not recorded". */
function sinceWords(t: number | null, nowMs: number): string {
  if (t === null) return 'since: not recorded'
  return `since ${clock(t)} · ${spanWords((nowMs - t) / 1000)}`
}

/** How long a wait usually takes, from the route's `typical_s`; null when the route serves no such time. */
function typical(state: SetupState, key: string): string | null {
  const s = state.typical_s[key]
  return s === undefined ? null : `usually about ${spanWords(s)}`
}

/** The verify checks swarm install runs, in plain words; an id not listed is shown as it is. */
const CHECK_WORDS: Record<string, string> = {
  smoke: 'the smoke run',
  'gke-proof': 'the GKE proof',
  race: 'the race test',
  'reconciler-jobs': 'the reconciler’s scheduled jobs',
  digests: 'the image digests',
  'plugin-browser': 'the live-browser check',
  'console-task': 'the console task',
  'console-workflow': 'the console workflow',
}
const checkWords = (id: string | null): string => (id === null ? 'starting' : CHECK_WORDS[id] ?? id)

/** The cloud stages swarm install finishes before it hands over (§5.1: 0-8, 10, 13). */
const CLI_STAGES = [0, 1, 2, 3, 4, 5, 6, 7, 8, 10, 13]

/** The record, as the checklist's eight lines. A line is done exactly when the record says so. */
export function finishLines(state: SetupState, nowMs: number): Line[] {
  const e = state.engine
  const handOff = ms(e?.hand_off_at)
  const stages = e?.stages ?? []
  const cloudDone = CLI_STAGES.filter((n) => stages[n]?.state === 'done').length
  const onePerson = isOnePerson(state)
  const out: Line[] = []

  out.push({
    key: 'cloud',
    name: 'Cloud',
    state: cloudDone === CLI_STAGES.length ? 'done' : 'in_progress',
    actor: cloudDone === CLI_STAGES.length ? null : 'install',
    since: handOff,
    now: `${cloudDone} of ${CLI_STAGES.length} stages, by swarm install`,
    waits: [],
  })

  const client = state.sign_in_client
  const signIn: Line = { key: 'sign-in-client', name: 'Sign-in client', state: 'todo', actor: null, since: null, now: 'not read yet', waits: [] }
  if (client !== null) {
    const saved = ms(client.saved_at)
    if (client.state === 'applied') Object.assign(signIn, { state: 'done', since: saved, now: 'on Google’s sign-in proxy' })
    else if (client.state === 'saved') {
      Object.assign(signIn, { state: 'in_progress', actor: 'install', since: saved, now: 'saved; swarm install applies it' })
      signIn.waits.push({ who: 'install', what: 'adds the client to the sign-in proxy (its infra pass)', since: saved, usual: typical(state, 'sign_in_pass') })
    } else Object.assign(signIn, { actor: 'you', since: handOff, now: 'three Google pages, then its id and secret' })
  }
  out.push(signIn)

  const app = state.github_app
  const org = state.github_org ?? 'your organisation'
  const gh: Line = { key: 'github', name: 'GitHub App', state: 'todo', actor: 'you', since: ms(client?.saved_at) ?? handOff, now: 'create it from the manifest', waits: [] }
  if (app !== null && app.started) {
    const written = ms(app.secrets_written_at)
    const check = app.self_check
    if (check?.state === 'failed') Object.assign(gh, { state: 'failed', actor: 'you', since: ms(check.at), now: `self-check failed${check.reason === null ? '' : `: ${check.reason}`}` })
    else if (app.installed_on !== null && check?.ok === true) Object.assign(gh, { state: 'done', actor: null, since: ms(check.at), now: `installed on ${app.installed_on}` })
    else if (app.id === null) Object.assign(gh, { state: 'in_progress', actor: 'github', since: null, now: 'you are on GitHub’s page creating it' })
    else if (app.installed_on === null) {
      Object.assign(gh, { state: 'in_progress', actor: 'you', since: written, now: `created; install it on ${org}` })
      gh.waits.push({ who: 'github', what: `tells this page once the App is installed on ${org}; the page checks every 10 s`, since: written, usual: 'as soon as you press Install' })
    } else {
      Object.assign(gh, { state: 'in_progress', actor: 'install', since: ms(check?.at) ?? written, now: `installed on ${app.installed_on}; self-check waits` })
      gh.waits.push({
        who: 'install',
        what: 'runs its third infrastructure pass, which gives swarm-api the App’s id; this page then runs the self-check by itself',
        since: ms(check?.at) ?? written,
        usual: typical(state, 'pass_3'),
      })
    }
  }
  out.push(gh)

  const team = firstWorkspace(state)
  const ws = team === null ? null : typeof team.progress.workspace_state === 'string' ? team.progress.workspace_state : null
  const built = ms(typeof team?.progress.created_at === 'string' ? team.progress.created_at : null)
  const teamLine: Line = { key: 'team', name: onePerson ? 'Your workspace' : 'First team', state: 'todo', actor: null, since: null, now: 'after the GitHub App', waits: [] }
  if (team === null) {
    if (gh.state === 'done') Object.assign(teamLine, { actor: onePerson ? 'console' : 'you', since: gh.since, now: onePerson ? 'filed on the next read' : 'name the Google group' })
  } else if (ws === 'failed' || ws === 'denied') {
    Object.assign(teamLine, { state: 'failed', actor: 'you', since: built, now: `${team.workspace}: the build ${ws}` })
  } else if (ws !== 'ready') {
    Object.assign(teamLine, { state: 'in_progress', actor: 'console', since: built, now: `${team.workspace}: building` })
    teamLine.waits.push({ who: 'console', what: `builds ${team.workspace}: record, namespace, worker identity, git access`, since: built, usual: typical(state, 'workspace_build') })
  } else {
    Object.assign(teamLine, { state: 'done', since: built, now: `${team.workspace} is ready` })
  }
  // Invites: the console adds no one; Google Groups shows each person once they are added on Google Admin's page.
  const t0 = state.teams[0]
  if (!onePerson && t0 !== undefined) {
    for (const p of t0.members) {
      if (p.member === true) continue
      teamLine.waits.push({
        who: 'google',
        name: p.email,
        what: p.member === null ? `Google Groups did not answer this read for ${p.email}` : `shows ${p.email} in ${t0.group} once they are added on Google Admin’s members page`,
        since: built,
        usual: typical(state, 'groups'),
      })
    }
  }
  out.push(teamLine)

  const signed = signedInCount(state)
  const acc: Line = { key: 'accounts', name: 'Claude accounts', state: 'todo', actor: null, since: null, now: onePerson ? 'after your workspace' : 'after the first team', waits: [] }
  if (team !== null || onePerson) {
    acc.state = signed > 0 ? 'done' : state.accounts.length > 0 ? 'in_progress' : 'todo'
    acc.now = state.accounts.length === 0 ? 'one link per subscription account' : `${signed} of ${state.accounts.length} signed in`
    if (state.accounts.length === 0) Object.assign(acc, { actor: 'you', since: built })
    for (const a of state.accounts) {
      if (accountSignedIn(state, a)) {
        if (acc.since === null) acc.since = ms(a.signed_in_at)
        continue
      }
      const exp = ms(a.link_expires)
      const own = onePerson || a.owner === state.personal?.owner
      const left = exp === null ? null : exp - nowMs
      acc.waits.push({
        who: own ? 'you' : 'teammate',
        name: a.owner ?? a.label,
        what: own ? 'sign in your own account: the link opens itself in a new tab' : left !== null && left <= 0 ? 'the link expired unused: make a new one' : 'opens the one-time link and signs in',
        since: ms(a.link_made_at),
        usual: left === null ? 'link time not recorded' : left > 0 ? `${spanWords(left / 1000)} left on the link` : 'expired',
      })
    }
    if (acc.state !== 'done') {
      const w = acc.waits[0]
      if (w !== undefined) Object.assign(acc, { actor: w.who, since: w.since })
    }
  }
  out.push(acc)

  const smoke = team?.smoke ?? null
  const smokeAt = ms(smoke?.started_at)
  const sm: Line = { key: 'smoke', name: 'Smoke agent', state: 'todo', actor: null, since: null, now: 'runs on the first signed-in account', waits: [] }
  if (smoke !== null) {
    if (smoke.state === 'succeeded') Object.assign(sm, { state: 'done', since: smokeAt, now: smoke.detail ?? 'succeeded' })
    else if (smoke.state === 'failed') Object.assign(sm, { state: 'failed', actor: 'you', since: smokeAt, now: 'failed: retry it' })
    else {
      Object.assign(sm, { state: 'in_progress', actor: 'console', since: smokeAt, now: `${smoke.state} in ${team?.workspace ?? 'the workspace'}` })
      sm.waits.push({ who: 'console', what: 'runs one agent end to end: clone, edit, publish', since: smokeAt, usual: typical(state, 'smoke') })
    }
  } else if (team !== null && signed > 0) Object.assign(sm, { actor: 'console', now: 'starts on the next read' })
  out.push(sm)

  const v = e?.verify ?? null
  const finished = ms(state.finished?.at)
  const ver: Line = { key: 'verify', name: 'Verify', state: 'todo', actor: null, since: null, now: 'after the smoke agent', waits: [] }
  if (v === null) {
    if (state.finished !== null) Object.assign(ver, { actor: 'install', since: finished, now: 'handed over; starting' })
    else if (consoleDone(state)) Object.assign(ver, { actor: 'you', since: smoke?.state === 'succeeded' ? smokeAt : null, now: 'hand over to swarm install' })
  } else {
    const started = v.started === null ? null : v.started * 1000
    if (v.result === 'passed') Object.assign(ver, { state: 'done', since: started, now: `passed, ${v.done} of ${v.total} checks` })
    else if (v.result === 'failed') Object.assign(ver, { state: 'failed', actor: 'you', since: started, now: `failed at check ${v.done + 1} of ${v.total}` })
    else {
      Object.assign(ver, { state: 'in_progress', actor: 'install', since: started, now: `check ${v.done + 1} of ${v.total}` })
      const left = v.expected_end === null ? null : v.expected_end * 1000 - nowMs
      ver.waits.push({
        who: 'install',
        what: `checks ${checkWords(v.check)}`,
        since: started,
        usual: left === null ? 'end not recorded' : left > 0 ? `about ${spanWords(left / 1000)} left` : `${spanWords(-left / 1000)} past the expected end`,
      })
    }
  }
  out.push(ver)

  const passed = v?.result === 'passed'
  out.push({ key: 'send', name: 'Send to your team', state: passed ? 'done' : 'todo', actor: passed ? 'you' : null, since: ms(e?.ready_at), now: passed ? 'the plugin line is ready' : 'locked until verify passes', waits: [] })
  return out
}

/** The line the panel follows by itself: the first one the person can act on, else the first not done, else Send. */
export function nextLineKey(ls: Line[]): LineKey {
  const mine = ls.find((l) => l.state !== 'done' && l.actor === 'you')
  if (mine !== undefined) return mine.key
  return ls.find((l) => l.state !== 'done')?.key ?? 'send'
}

/** A step's mark: a ring, ticked once done. */
function StepMark({ state }: { state: StepState }) {
  return (
    <svg className={`cfg-mark is-${state}`} viewBox="0 0 20 20" aria-hidden>
      <circle cx={10} cy={10} r={8.5} />
      {state === 'done' && <path d="m6 10.4 2.7 2.7L14.2 7.6" />}
    </svg>
  )
}

// ---------------------------------------------------------------------------
// the status line: what swarm install is doing now
// ---------------------------------------------------------------------------

function TerminalLine({ state, ls, nowMs }: { state: SetupState; ls: Line[]; nowMs: number }) {
  const e = state.engine
  const beat = ms(e?.lease?.heartbeat)
  const seen = beat === null ? 'not recorded' : `${spanWords((nowMs - beat) / 1000)} ago`
  const v = e?.verify ?? null
  const ready = ms(e?.ready_at)
  const host = e?.hostname ?? state.hostname
  const done = ls.filter((l) => l.state === 'done').length
  const verifyUsual = typical(state, 'verify')
  let tone: 'ok' | 'live' | 'bad' | 'warn' = 'live'
  let head: ReactNode
  let line: ReactNode
  if (ready !== null && host !== null) {
    tone = 'ok'
    head = 'swarm install finished and printed its ready line'
    line = (
      <>
        <code className="cfc-ready">READY: SwarmCloud answers at https://{host}</code> at {clock(ready)}. It has exited; nothing waits on this page.
      </>
    )
  } else if (!state.cli_running) {
    tone = 'bad'
    head = `swarm install is not running · last seen ${seen}`
    line = (
      <>
        Every step here still works up to the GitHub App’s self-check. To continue, run <code>swarm install</code> again in a terminal: it resumes where it stopped.
      </>
    )
  } else if (v !== null && v.result === 'failed') {
    tone = 'warn'
    head = `swarm install stopped: verify failed at check ${v.done + 1} of ${v.total}, ${checkWords(v.check)}`
    line = (
      <>
        It printed the cause in the terminal{v.detail === null ? '' : ', and it is shown below'}. Run <code>swarm install</code> again once it is fixed: verify starts again from that check.
      </>
    )
  } else if (v !== null) {
    head = `swarm install is running verify · check ${v.done + 1} of ${v.total}, ${checkWords(v.check)}`
    const left = v.expected_end === null ? null : v.expected_end * 1000 - nowMs
    const dl = v.deadline === null ? null : v.deadline * 1000 - nowMs
    line = `${left === null ? 'End not recorded' : left > 0 ? `About ${spanWords(left / 1000)} left` : `${spanWords(-left / 1000)} past the expected end`}${dl === null ? '' : ` · stops at its deadline, in ${spanWords(Math.max(0, dl) / 1000)}`} · seen ${seen}. It prints its ready line when every check passes.`
  } else if (state.finished !== null) {
    head = 'swarm install has the hand-over and is starting verify'
    line = `Seen ${seen}.${verifyUsual === null ? '' : ` Verify ${verifyUsual.replace('usually about', 'usually takes about')}.`}`
  } else {
    const pass = ls.find((l) => l.actor === 'install' && l.state !== 'done')
    head = pass === undefined ? 'swarm install is waiting on this page' : `swarm install is working: ${pass.waits[0]?.what ?? pass.now}`
    line = `Seen ${seen}; it checks in every 30 s and runs its own steps as yours land here. Keep its terminal open.`
  }
  const left = ls.length - done
  const waitingOn = [...new Set(ls.filter((l) => l.state !== 'done' && l.actor !== null).map((l) => ACTOR[l.actor as Actor]))].join(', ')
  const installUsual = typical(state, 'install')
  return (
    <section className={`cfc-status is-${tone}`} aria-label="What swarm install is doing" role="status">
      <span className="cfc-status-mark">
        <StepMark state={tone === 'ok' ? 'done' : tone === 'bad' || tone === 'warn' ? 'failed' : 'in_progress'} />
      </span>
      <div className="cfc-status-main">
        <p className="cfc-status-h">
          <span className="cfc-status-k">Terminal</span> {head}
        </p>
        <p className="cfc-status-l">{line}</p>
      </div>
      <div className="cfc-status-count">
        <b>
          {done} of {ls.length} done
        </b>
        <small>
          {left === 0
            ? 'every step done'
            : state.finished !== null
              ? `${left} left`
              : done <= 1 && installUsual !== null
                ? `${left} left · ${installUsual} in all`
                : `${left} left · waiting on ${waitingOn || 'nobody yet'}`}
        </small>
      </div>
    </section>
  )
}

// ---------------------------------------------------------------------------
// the compact checklist
// ---------------------------------------------------------------------------

function ActorChip({ who, name }: { who: Actor; name?: string }) {
  return <span className={`cfc-who is-${who}`}>{name === undefined ? ACTOR[who] : `${ACTOR[who]} · ${name}`}</span>
}

function CompactList({ ls, selected, following, onPick, nowMs, onePerson }: { ls: Line[]; selected: LineKey; following: boolean; onPick: (k: LineKey | null) => void; nowMs: number; onePerson: boolean }) {
  return (
    <nav className="cfc-list" aria-label="Setup steps">
      <ol>
        {ls.map((l, i) => {
          const others = l.waits.filter((w) => w.who === 'teammate' || w.who === 'google')
          return (
            <li key={l.key} data-step={l.key} className={`cfc-item is-${l.state}${l.key === selected ? ' is-selected' : ''}`}>
              <button type="button" className="cfc-pick" aria-current={l.key === selected ? 'step' : undefined} onClick={() => onPick(l.key)}>
                <StepMark state={l.state} />
                <span className="cfc-item-main">
                  <span className="cfc-item-h">
                    <b>
                      {i + 1}. {l.name}
                    </b>
                    {l.state === 'failed' && <Chip tone="bad">failed</Chip>}
                  </span>
                  {l.state === 'done' ? (
                    <span className="cfc-item-now">
                      <span className="cfc-done">{l.since === null ? 'done' : `done ${clock(l.since)}`}</span> · {l.now}
                    </span>
                  ) : l.actor === null ? (
                    <span className="cfc-item-now cfc-later">{l.now}</span>
                  ) : (
                    <>
                      <span className="cfc-item-now">{l.now}</span>
                      <span className="cfc-item-who">
                        <ActorChip who={l.actor} />
                        <span className="cfc-since">{sinceWords(l.since, nowMs)}</span>
                      </span>
                    </>
                  )}
                </span>
              </button>
              {others.length > 0 && (
                <ul className="cfc-subs" aria-label={`${l.name}: waiting on others`}>
                  {others.map((w, j) => (
                    <li key={j}>
                      <ActorChip who={w.who} name={w.name} />
                      <span className="cfc-since">
                        {w.since === null ? 'since: not recorded · ' : `since ${clock(w.since)} · `}
                        {w.usual ?? 'usual time not recorded'}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </li>
          )
        })}
      </ol>
      <p className="cfc-addons">
        <span className="cfc-addon-mark" aria-hidden>
          +
        </span>
        Add-ons: optional, off. Each is turned on from the terminal later; the panel lists them.
        <button type="button" className="cfc-link" onClick={() => onPick(null)} hidden={following}>
          Follow the next step again
        </button>
      </p>
      {onePerson && <p className="cfc-note">One-person install: no team step and no invites. Your admins group was still made.</p>}
    </nav>
  )
}

// ---------------------------------------------------------------------------
// the working panel
// ---------------------------------------------------------------------------

function WaitTable({ waits, nowMs, closed }: { waits: Wait[]; nowMs: number; closed: boolean }) {
  if (waits.length === 0) return null
  return (
    <div className="cfc-waits" role="region" aria-label="Waiting on">
      <h3 className="cfc-h3">Waiting on</h3>
      <ul>
        {waits.map((w, i) => (
          <li key={i}>
            <ActorChip who={w.who} name={w.name} />
            <span className="cfc-wait-what">
              {w.what}
              {closed && w.who === 'install' ? ' (not running: run swarm install again)' : ''}
            </span>
            <span className="cfc-wait-meta">
              <span>{sinceWords(w.since, nowMs)}</span>
              <span>{w.usual ?? 'usual time not recorded'}</span>
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}

/** The cloud line's panel: what the terminal did before it handed over. */
function CloudBody({ state, nowMs }: { state: SetupState; nowMs: number }) {
  const e = state.engine
  const stages = e?.stages ?? []
  const first = ms(stages[0]?.at)
  const handOff = ms(e?.hand_off_at)
  const cloudDone = CLI_STAGES.filter((n) => stages[n]?.state === 'done').length
  return (
    <Facts
      label="The cloud part"
      items={[
        { state: cloudDone === CLI_STAGES.length ? 'done' : 'in_progress', name: 'Stages', text: `${cloudDone} of ${CLI_STAGES.length}: project, infrastructure, identity, edge and secrets` },
        { state: handOff === null ? 'todo' : 'done', name: 'Handed over', text: handOff === null ? 'not recorded' : `at ${clock(handOff)}, ${spanWords((nowMs - handOff) / 1000)} ago${first === null ? '' : `; its first stage was recorded at ${clock(first)}`}` },
        { state: e?.release == null ? 'todo' : 'done', name: 'Release', text: e?.release ?? 'not recorded' },
      ]}
    />
  )
}

function SmokeBody({ state, onChanged }: { state: SetupState; onChanged: () => void }) {
  const act = useAction(onChanged)
  const team = firstWorkspace(state)
  const smoke = team?.smoke ?? null
  const first = state.accounts.find((a) => accountSignedIn(state, a))
  return (
    <>
      {smoke?.state === 'failed' && (
        <StepFailure
          cause={`The smoke agent failed${smoke.detail === null ? '' : `: ${smoke.detail}`}`}
          fix={<>check that the App is installed on that repository ({state.github_org ?? 'your organisation'}’s settings › GitHub Apps › Repository access), then Retry. Its task page says the rest.</>}
          onRetry={() => act.run('smoke', restartSetupSmoke)}
          busy={act.busy === 'smoke'}
          retryLabel="Retry the smoke agent"
        />
      )}
      {act.error !== null && (
        <p className="cfc-err" role="alert">
          {act.error.message}
        </p>
      )}
      <Facts
        label="Smoke agent"
        items={[
          {
            state: smoke === null ? 'todo' : smoke.state === 'succeeded' ? 'done' : smoke.state === 'failed' ? 'failed' : 'in_progress',
            name: 'State',
            text: smoke === null ? 'not started' : smoke.state === 'succeeded' ? `succeeded${smoke.detail === null ? '' : `, ${smoke.detail}`}` : smoke.state,
          },
          { state: first === undefined ? 'todo' : 'done', name: 'On account', text: first === undefined ? 'none signed in yet' : `${first.label} (${first.owner ?? 'owner not recorded'})` },
          { state: team === null ? 'todo' : 'done', name: 'In', text: team === null ? 'no workspace yet' : `the ${team.workspace} workspace` },
        ]}
      />
      {smoke?.url != null && (
        <p className="ur-hint">
          <a href={smoke.url}>Open the smoke agent’s page</a>
        </p>
      )}
      <p className="ur-hint">The hand-over to swarm install is offered once it succeeds.</p>
    </>
  )
}

function SendBody({ state }: { state: SetupState }) {
  const passed = state.engine?.verify?.result === 'passed'
  const host = state.hostname ?? state.engine?.hostname ?? null
  const [copied, setCopied] = useState(false)
  if (!passed) return <p className="cfc-locked">Locked until verify passes: the line works only on a deployment verify has proven.</p>
  if (host === null) return <p className="ur-hint">The deployment’s address is not recorded yet, so there is nothing to send.</p>
  const line = pluginLine(host, state.engine?.release ?? null)
  return (
    <>
      <p className="ur-hint">One line for each teammate’s terminal. Its only value is this deployment’s address; the plugin reads the rest from it.</p>
      <CodeBlock
        title="Plugin install"
        lang="shell"
        text={line}
        action={
          <Button size="sm" kind="primary" onClick={() => void navigator.clipboard?.writeText(line).then(() => setCopied(true))}>
            {copied ? 'Copied' : 'Copy'}
          </Button>
        }
      />
      <Facts
        items={[
          { state: 'done', name: 'Console', text: `https://${host}` },
          { state: 'done', name: 'Then', text: 'each teammate runs sc login, then /sc:submit in Claude Code' },
          { state: 'done', name: 'Day two', text: 'Admin › Setup › Health shows the hourly checks' },
        ]}
      />
    </>
  )
}

function VerifyBody({ state, onChanged }: { state: SetupState; onChanged: () => void }) {
  const v = state.engine?.verify
  if (v == null) return null
  return (
    <>
      {v.result === 'failed' && (
        <StepFailure
          cause={`Check ${v.done + 1} of ${v.total}, ${checkWords(v.check)}, failed${v.detail === null ? '' : `: ${v.detail}`}`}
          fix={
            <>
              swarm install printed {v.detail === null ? 'the cause' : 'the same'} in its terminal. Once it is fixed, run <code>swarm install</code> again: it starts verify again from this check.
            </>
          }
        />
      )}
      <p className="cfc-row-line">
        {v.done} of {v.total} checks passed{v.result === null ? `; now ${checkWords(v.check)}` : ''}. The console task and workflow below are two of them.
      </p>
      <ConsoleChecks state={state} onChanged={onChanged} />
    </>
  )
}

function PanelHead({ kicker, line, children }: { kicker: string; line: Line; children?: ReactNode }) {
  return (
    <header className="cfc-panel-head">
      <p className="cfc-kicker">{kicker}</p>
      <h2 className={`cfc-panel-h is-${line.state}`}>
        <StepMark state={line.state} />
        {line.name}
      </h2>
      {children}
    </header>
  )
}

function Panel({ line, n, total, state, rows, nowMs, onChanged, following }: { line: Line; n: number; total: number; state: SetupState; rows: Partial<Record<LineKey, Row>>; nowMs: number; onChanged: () => void; following: boolean }) {
  const row = rows[line.key]
  const closed = !state.cli_running && state.engine?.ready_at == null
  let body: ReactNode = null
  if (line.key === 'cloud') body = <CloudBody state={state} nowMs={nowMs} />
  else if (line.key === 'smoke') body = <SmokeBody state={state} onChanged={onChanged} />
  else if (line.key === 'send') body = <SendBody state={state} />
  else if (line.key === 'verify' && state.engine?.verify != null) body = <VerifyBody state={state} onChanged={onChanged} />
  else if (row !== undefined) {
    body = (
      <>
        {line.key === 'verify' && closed && consoleDone(state) && state.finished === null && (
          <p className="cfc-locked">swarm install is not running. You can hand over now: verify starts as soon as swarm install runs again.</p>
        )}
        <p className="cfc-row-line">{row.line}</p>
        {row.body ?? null}
        {row.act !== undefined && <div className="cfc-act">{row.act}</div>}
      </>
    )
  }
  return (
    <section className="cfc-panel" aria-label={`Step ${n}: ${line.name}`}>
      <PanelHead kicker={`Step ${n} of ${total}${following ? ' · the panel follows the next step you can act on' : ' · picked by you'}`} line={line}>
        <p className="cfc-panel-who">
          {line.state === 'done' ? (
            <span className="cfc-done">Done{line.since === null ? '' : ` at ${clock(line.since)}`}, from the install record</span>
          ) : line.actor === null ? (
            <span className="cfc-later">Not yet: it waits on the step above</span>
          ) : (
            <>
              <span>Acts now:</span> <ActorChip who={line.actor} /> <span className="cfc-since">{sinceWords(line.since, nowMs)}</span>
              {line.usual !== undefined && <span className="cfc-usual">{line.usual}</span>}
            </>
          )}
        </p>
      </PanelHead>
      <WaitTable waits={line.waits} nowMs={nowMs} closed={closed} />
      <div className="cfc-panel-body">{body}</div>
    </section>
  )
}

function ThisInstall({ state }: { state: SetupState }) {
  const s = state.sizing
  const addons = addonsRow(state)
  return (
    <section className="cfc-about" aria-label="This install">
      <h3 className="cfc-h3">This install</h3>
      <dl className="cfc-facts">
        <div>
          <dt>Sized for</dt>
          <dd>{s === null ? 'not recorded' : s.mode === 'individual' ? 'one person' : s.members === null ? 'a team' : `a team of ${s.members}`}</dd>
        </div>
        <div>
          <dt>Budget</dt>
          <dd>{s?.budget_usd == null ? 'not recorded' : `$${s.budget_usd.toLocaleString('en-US')} a month`}</dd>
        </div>
        <div>
          <dt>Accounts</dt>
          <dd>{s?.pool_accounts == null ? 'not recorded' : `${s.pool_accounts} to add`}</dd>
        </div>
        <div>
          <dt>Ceiling</dt>
          <dd>{s?.pool_limits.global == null ? 'not recorded' : `${s.pool_limits.global} agents at once`}</dd>
        </div>
        <div>
          <dt>Release</dt>
          <dd>{state.engine?.release ?? 'not recorded'}</dd>
        </div>
        <div>
          <dt>Health</dt>
          <dd>
            <a href="/admin/setup/health">Admin › Setup › Health</a>
          </dd>
        </div>
      </dl>
      <details className="cfc-more">
        <summary>Capacity in full, and the add-ons</summary>
        <CapacityCard sizing={s} />
        <p className="ur-hint">{addons.line}</p>
        {addons.body ?? null}
      </details>
    </section>
  )
}

/**
 * The one screen for one read of the record: the status line, the checklist
 * and the panel. `callback` is GitHub's callback page, drawn as the GitHub
 * step's panel; `now` fixes the clock (a test, the fixture build's moments).
 */
export function ChecklistFinish({ state, now, onChanged, callback }: { state: SetupState; now?: number; onChanged?: () => void; callback?: ReactNode }) {
  const nowMs = useNow(now)
  const changed = useRef(onChanged)
  changed.current = onChanged
  const reread = useCallback(() => changed.current?.(), [])
  const githubAct = useAction(reread)
  useAutoSelfCheck(state, githubAct)
  const [picked, setPicked] = useState<LineKey | null>(null)
  const ls = finishLines(state, nowMs)
  const following = picked === null && callback === undefined
  const selected: LineKey = callback !== undefined ? 'github' : picked ?? nextLineKey(ls)
  const rows: Partial<Record<LineKey, Row>> = {
    'sign-in-client': signInRow(state, reread, nowMs),
    github: githubRow(state, reread, githubAct),
    team: teamRow(state, reread, nowMs),
    accounts: accountsRow(state, reread, nowMs),
    verify: verifyRow(state, reread, nowMs),
  }
  const i = ls.findIndex((l) => l.key === selected)
  const line = ls[i] as Line
  return (
    <section className="cfc-finish" aria-label="Install setup">
      <TerminalLine state={state} ls={ls} nowMs={nowMs} />
      <div className="cfc-grid">
        <div className="cfc-left">
          <CompactList ls={ls} selected={selected} following={following} onPick={setPicked} nowMs={nowMs} onePerson={isOnePerson(state)} />
        </div>
        <div className="cfc-right">
          {callback !== undefined ? (
            <section className="cfc-panel" aria-label="Back from GitHub">
              <PanelHead kicker={`Step ${i + 1} of ${ls.length} · back from GitHub`} line={line} />
              <div className="cfc-panel-body">{callback}</div>
            </section>
          ) : (
            <Panel line={line} n={i + 1} total={ls.length} state={state} rows={rows} nowMs={nowMs} onChanged={reread} following={following} />
          )}
          <ThisInstall state={state} />
          <p className="cfc-prov">Every mark comes from the install record, read every 5 s; nothing here is ticked by hand.</p>
        </div>
      </div>
    </section>
  )
}
