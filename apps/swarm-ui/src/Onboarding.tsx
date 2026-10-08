import { useState, type ReactNode } from 'react'
import { loadOnboarding } from './api'
import { Button, ButtonLink, Card, Chip, Dash, ToneMark } from './components'
import { ConnectButton, connectionOf } from './GitHubConnect'
import { addressToPath } from './paths'
import { UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { PageHead } from './Shell'
import type { AppInstalledEvidence, OnboardingDoc, OnboardingIssue, OnboardingStep, OnboardingStepName, OnboardingStepState } from './types'
import { timeAgo } from './types'
import './styles/repositories.css'
import './styles/onboarding.css'

/**
 * THE ONBOARDING CHECKLIST (#780, lane OB8; docs/onboarding.md §2.1-§2.3,
 * owner pick D10 "Entry A"). Two renderings of one read, `GET /v1/onboarding`:
 *
 *   * `SetupCard`, on Overview, where a person already lands -- shown while
 *     the checklist is not complete, gone by itself once it is (Entry A);
 *   * `OnboardingScreen`, Work › Setup, the same steps as a page the nav
 *     reaches whether or not the card was hidden.
 *
 * INSTALL THE APP (#780, 2026-10-08) is its own step, between Connect GitHub
 * and the orgs: connecting authorises the App, installing it is a second act
 * at GitHub, and a person who did only the first saw an Access page with
 * nothing on it. Its action is the App's install page itself.
 *
 * DERIVED, NEVER SET (§2.1). Every state here is what the server derived on
 * this read; nothing on this page marks a step done. A step's sub-line is its
 * evidence, its failure is the §2.3 copy the server sent WORD FOR WORD (so the
 * plugin's `/sc:setup` prints the same sentence), and its button is the next
 * action: Connect GitHub here, the rest on Work › Access, where OB4's routes
 * act. Re-check is a fresh read, which is a fresh derivation.
 *
 * HIDE IS THIS BROWSER'S. §3.2 drafts `POST /v1/onboarding/dismiss`, and no
 * lane has built it, so Hide is kept in localStorage per tenant and person:
 * the steps keep their state, Work › Setup still shows them, and another
 * browser still shows the card. Said on the card, not implied.
 */

export const SETUP = 'work/setup'
export const ACCESS = 'work/access'
const SUBMIT_TASK = 'work/new'

/** The steps' names as the mock-up draws them (onboarding.html, Entry A). */
export const STEP_LABEL: Readonly<Record<OnboardingStepName, string>> = {
  signed_in: 'Sign in',
  github_connected: 'Connect GitHub',
  app_installed: 'Install the App',
  orgs_enabled: 'Enable orgs',
  repos_chosen: 'Choose repositories',
  access_verified: 'Verify access',
  ready: 'Ready',
}

const STATE_WORD: Readonly<Record<OnboardingStepState, string>> = {
  done: 'done',
  in_progress: 'in progress',
  failed: 'failed',
  stale: 'stale',
  todo: 'to do',
}

/** A state as a mark. `todo` is the hollow ring: nobody has derived it done. */
const STATE_TONE: Readonly<Record<OnboardingStepState, string>> = {
  done: 'ok',
  in_progress: 'live',
  failed: 'bad',
  stale: 'warn',
  todo: 'unknown',
}

/** The page the step's next action is on, and its button's words. */
const ACCESS_ACTION: Partial<Record<OnboardingStepName, string>> = {
  orgs_enabled: 'Enable orgs',
  repos_chosen: 'Choose repositories',
  access_verified: 'Verify access',
}

function str(v: unknown): string | null {
  return typeof v === 'string' && v !== '' ? v : null
}

function rows(v: unknown): Record<string, unknown>[] {
  return Array.isArray(v) ? v.filter((r): r is Record<string, unknown> => typeof r === 'object' && r !== null) : []
}

export function doneCount(doc: OnboardingDoc): number {
  return doc.steps.filter((s) => s.state === 'done').length
}

/** What the step's evidence says, in one line: its last probe, never a guess. */
function stepLine(step: OnboardingStep, doc: OnboardingDoc): ReactNode {
  const ev = step.evidence ?? {}
  const waiting = str(ev.waiting_for)
  if (step.state === 'todo' && waiting !== null) {
    const on = STEP_LABEL[waiting as OnboardingStepName] ?? waiting
    return `waits for ${on}`
  }
  switch (step.step) {
    case 'signed_in': {
      const email = str(ev.email)
      const tenant = str(ev.tenant_id) ?? doc.tenant_id
      return email === null ? `tenant ${tenant}` : `${email} · tenant ${tenant}`
    }
    case 'github_connected': {
      const view = connectionOf(doc)
      if (view.kind === 'app') return `connected as @${view.login ?? '?'} · GitHub App`
      if (view.kind === 'own-token') return `your own stored token${view.login !== null ? ` (@${view.login})` : ''}`
      if (view.kind === 'tenant') return `tasks act through the tenant token${view.login !== null ? ` (@${view.login})` : ''}, not as you`
      return 'no GitHub account connected'
    }
    case 'app_installed': {
      const inst = ev as Partial<AppInstalledEvidence>
      if (inst.needed === false) return 'not needed: this connection is a token, not the App'
      if (inst.read !== true) return 'installations not read yet'
      const on = Array.isArray(inst.installed) ? inst.installed : []
      if (on.length > 0) return `installed on ${on.join(', ')}`
      const login = str(inst.login)
      return `${login !== null ? `connected as @${login}, but ` : ''}SwarmCloud Saga isn't installed anywhere yet`
    }
    case 'orgs_enabled': {
      const owners = rows(ev.owners)
      if (owners.length === 0) return 'no owner read yet'
      return owners
        .map((o) => {
          const reach = str(o.reach)
          return `${str(o.owner) ?? '?'}${reach !== null && reach !== 'reachable' ? ` (${reach.replace(/_/g, ' ')})` : ''}`
        })
        .join(' · ')
    }
    case 'repos_chosen': {
      const repos = rows(ev.repositories)
      if (repos.length === 0) return 'no repository granted'
      const shown = repos.slice(0, 3).map((r) => `${str(r.repository) ?? '?'} (${str(r.mode) ?? '?'})`)
      return `${repos.length} granted: ${shown.join(', ')}${repos.length > 3 ? `, +${repos.length - 3} more` : ''}`
    }
    case 'access_verified': {
      const repos = rows(ev.repositories)
      if (repos.length === 0) return 'clone, push and pull request checked for every granted repository'
      const n = (r: string) => repos.filter((x) => x.result === r).length
      const parts = [`${n('passed')} passed`]
      if (n('failed') > 0) parts.push(`${n('failed')} failed`)
      if (n('pending') > 0) parts.push(`${n('pending')} not checked yet`)
      // Name the repositories still to verify, so a person knows which
      // grant's Verify to press on Access (#896).
      const open = repos
        .filter((r) => r.result === 'failed' || r.result === 'pending')
        .map((r) => `${str(r.repository) ?? '?'}${r.result === 'failed' ? ' (failed)' : ''}`)
      if (open.length === 0) return parts.join(' · ')
      const shown = open.slice(0, 3).join(', ') + (open.length > 3 ? `, +${open.length - 3} more` : '')
      return (
        <>
          {parts.join(' · ')} · {shown}: press Verify on{' '}
          <a className="c-link" href={addressToPath(ACCESS)}>
            Access
          </a>
        </>
      )
    }
    case 'ready': {
      const first = str(ev.first_repository)
      return first === null ? 'submit a first task in a granted repository' : `submit a first task in ${first}`
    }
  }
  return null
}

/** A §2.3 problem: the server's copy, word for word, and the GitHub page it names. */
function IssueCopy({ code, copy, url }: { code: string; copy: string; url?: string | null }) {
  return (
    <div className="ob-issue" data-code={code}>
      <p className="ur-small">
        <b className="ur-bad">{code}</b> {copy}
      </p>
      {typeof url === 'string' && url.startsWith('https://github.com/') && (
        <a className="c-link" href={url} target="_blank" rel="noreferrer">
          Open on GitHub
        </a>
      )}
    </div>
  )
}

/** The step's one next action, when it has one. */
function StepAction({ step, doc, reload }: { step: OnboardingStep; doc: OnboardingDoc; reload: () => void }) {
  if (step.step === 'signed_in') return null
  if (step.step === 'ready') {
    return step.state === 'done' ? (
      <ButtonLink kind="primary" size="sm" href={addressToPath(SUBMIT_TASK)}>
        Submit a task
      </ButtonLink>
    ) : null
  }
  if (step.state === 'done') return null
  if (step.step === 'github_connected') {
    if (step.state === 'stale') {
      return (
        <Button size="sm" onClick={reload}>
          Re-check
        </Button>
      )
    }
    const view = connectionOf(doc)
    return <ConnectButton label={view.kind === 'app' ? 'Reconnect' : 'Connect GitHub'} />
  }
  if (step.step === 'app_installed') {
    const url = str(step.evidence?.install_url)
    const waiting = step.state === 'todo' && step.evidence?.waiting_for !== undefined
    if (waiting) return null
    return (
      <span className="ob-acts">
        {step.state === 'todo' && url !== null && url.startsWith('https://github.com/') && (
          <ButtonLink kind={doc.next_step === step.step ? 'primary' : 'secondary'} size="sm" href={url} target="_blank" rel="noreferrer">
            Install the App
          </ButtonLink>
        )}
        <Button size="sm" kind="ghost" onClick={reload}>
          Re-check
        </Button>
      </span>
    )
  }
  const label = ACCESS_ACTION[step.step]
  if (label === undefined || (step.state === 'todo' && step.evidence?.waiting_for !== undefined)) return null
  return (
    <span className="ob-acts">
      <ButtonLink kind={doc.next_step === step.step ? 'primary' : 'secondary'} size="sm" href={addressToPath(ACCESS)}>
        {label}
      </ButtonLink>
      {(step.state === 'failed' || step.state === 'stale' || step.state === 'in_progress') && (
        <Button size="sm" kind="ghost" onClick={reload}>
          Re-check
        </Button>
      )}
    </span>
  )
}

/** The six steps' states as one bar: a glance at how far along this person is. */
export function StepBar({ doc }: { doc: OnboardingDoc }) {
  return (
    <div className="ob-bar" role="img" aria-label={`${doneCount(doc)} of ${doc.steps.length} setup steps done`}>
      {doc.steps.map((s) => (
        <i key={s.step} className={`is-${s.state}`} />
      ))}
    </div>
  )
}

/** The checklist itself, shared by the card and the page. */
export function Checklist({ doc, reload }: { doc: OnboardingDoc; reload: () => void }) {
  return (
    <ol className="ob-list" aria-label="Setup steps">
      {doc.steps.map((s) => {
        const issues: OnboardingIssue[] = Array.isArray(s.issues) ? s.issues : []
        const next = doc.next_step === s.step
        return (
          <li key={s.step} className={`ob-step is-${s.state}${next ? ' is-next' : ''}`} data-step={s.step} data-state={s.state} aria-current={next ? 'step' : undefined}>
            <ToneMark tone={STATE_TONE[s.state] ?? 'unknown'} hidden />
            <div className="ob-step-main">
              <p className="ob-step-h">
                <b>{STEP_LABEL[s.step] ?? s.step}</b> <code>{s.step}</code>
                <span className="ob-state">{STATE_WORD[s.state] ?? s.state}</span>
                {next && <Chip>next</Chip>}
              </p>
              <p className="ur-hint ob-line">
                {stepLine(s, doc)}
                {s.checked_at !== null && s.state !== 'todo' && <span className="ob-at"> · checked {timeAgo(s.checked_at)}</span>}
              </p>
              {issues.map((i, n) => (
                <IssueCopy key={`${i.code}:${i.owner ?? ''}:${i.repository ?? ''}:${n}`} code={i.code} copy={i.copy} url={i.url} />
              ))}
              {issues.length === 0 && s.code !== null && s.copy !== null && <IssueCopy code={s.code} copy={s.copy} />}
              {s.step === 'app_installed' && s.state !== 'done' && s.evidence?.waiting_for === undefined && (
                <p className="ur-hint ob-help">
                  Connecting authorised the App to act as you; installing it on your account or an org is what lets it
                  reach that owner's repositories.
                </p>
              )}
            </div>
            <div className="ob-step-act">
              <StepAction step={s} doc={doc} reload={reload} />
            </div>
          </li>
        )
      })}
    </ol>
  )
}

// ---------------------------------------------------------------------------
// Hide, kept in this browser (see the note at the top)
// ---------------------------------------------------------------------------

function hideKey(doc: OnboardingDoc): string {
  return `swarm.setup.hidden:${doc.tenant_id}:${doc.user_hash}`
}

export function setupHidden(doc: OnboardingDoc): boolean {
  try {
    return window.localStorage.getItem(hideKey(doc)) === '1'
  } catch {
    return false
  }
}

function hideSetup(doc: OnboardingDoc): void {
  try {
    window.localStorage.setItem(hideKey(doc), '1')
  } catch {
    // Storage refused (private mode): the card hides for this visit only.
  }
}

/**
 * ENTRY A, THE CARD ON OVERVIEW. It draws nothing until the read lands, and
 * nothing when the read failed or is not served: Overview's own reads are
 * the page, and a checklist that could not be read is not a step to take.
 */
export function SetupCard() {
  const doc = useUrRead(loadOnboarding, 'onboarding')
  const [hidden, setHidden] = useState(false)
  if (doc.state.status !== 'ok' && doc.state.status !== 'stale') return null
  const d = doc.state.data
  if (d.complete || !Array.isArray(d.steps) || hidden || setupHidden(d)) return null
  return (
    <Card
      className="ob-card"
      title="Set up SwarmCloud"
      action={
        <span className="ur-acts">
          <Chip>
            {doneCount(d)} of {d.steps.length} done
          </Chip>
          <Button
            kind="ghost"
            size="sm"
            onClick={() => {
              hideSetup(d)
              setHidden(true)
            }}
          >
            Hide
          </Button>
          <ButtonLink kind="primary" size="sm" href={addressToPath(SETUP)}>
            Open setup
          </ButtonLink>
        </span>
      }
    >
      <StepBar doc={d} />
      <Checklist doc={d} reload={doc.reload} />
      <p className="ur-hint">
        This card leaves Overview by itself once every step is done. Hide keeps every step as it is, in this browser
        only; Work › Setup still shows them.
      </p>
    </Card>
  )
}

/** Work › Setup: the checklist as a page. */
export function OnboardingScreen() {
  const doc = useUrRead(loadOnboarding, 'onboarding')
  return (
    <div className="ur-page ob-page">
      <PageHead title="Setup">
        <UrRefresh reads={[doc]} />
        <span className="ur-acts">
          <ButtonLink size="sm" href={addressToPath(ACCESS)}>
            Access
          </ButtonLink>
        </span>
      </PageHead>
      <p className="ur-sub">
        Connect GitHub as yourself, install the App where your repositories are, enable the orgs SwarmCloud may reach,
        choose the repositories it may read or write, and verify that it can. Each step is checked again on every read; nothing here is marked done by hand.
      </p>
      <UrRegion state={doc.state} route="GET /v1/onboarding" what="Your setup checklist" onRetry={doc.reload} lines={6}>
        {(d) =>
          Array.isArray(d.steps) && d.steps.length > 0 ? (
            <Card
              className="ob-card"
              title={d.complete ? 'Setup is complete' : 'Set up SwarmCloud'}
              action={
                <Chip>
                  {doneCount(d)} of {d.steps.length} done
                </Chip>
              }
              foot={
                <>
                  Derived {d.derived_at ? timeAgo(d.derived_at) : <Dash why="The read carried no time" />} for{' '}
                  {d.user} in tenant {d.tenant_id}.
                </>
              }
            >
              <StepBar doc={d} />
              <Checklist doc={d} reload={doc.reload} />
            </Card>
          ) : (
            <Dash why="The checklist answered with no steps" />
          )
        }
      </UrRegion>
    </div>
  )
}
