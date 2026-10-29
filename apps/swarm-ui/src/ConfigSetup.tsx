import { createContext, useContext, useState, type MouseEvent } from 'react'
import { Dash, routedClick } from './components'
import type { Result } from './fetch'
import { configSetupOf, itemName, secretsUnreadReason, type ConfigSetup } from './configSetupApi'
import { STEP_LABEL, countLabel } from './Onboarding'
import { WORKSPACE_SETUP_ADDRESS, addressToPath } from './paths'
import type { OnboardingDoc, OnboardingStepName, OnboardingStepState } from './types'
import { ageSpan } from './types'
import { useNow } from './useNow'
import './styles/config.css'

/**
 * THE SETUP STRIP (CONFIG-NAV; part of #2157, BGD's pick of 2026-10-24,
 * proposal E "Guided setup", docs/web-ui/mockups/config-e.html S1-S6).
 *
 * Proposal E is led by what is missing: across the top of every Config page,
 * GitHub connected -> Repositories chosen -> Secrets set -> People invited
 * (three steps in a personal workspace, which has nobody to invite, S1e), and
 * the Config panel carries the same count as a meter. ONE component, mounted
 * once by App's `ConfigPage`, so no Config page can be without it.
 *
 * EVERY STEP IS A MEASUREMENT OR SAYS IT IS NOT ONE. The strip is fed by the
 * read the console already makes for Workspace setup, `GET /v1/onboarding`
 * (Onboarding.tsx), which the shell makes once per workspace and again on
 * each Config page (Spine.tsx), and hands down through `SetupContext` so the
 * strip and the panel's meter never disagree. GitHub connected is its
 * `github_connected` step and Repositories chosen its `repos_chosen`, in the
 * state the server derived. Secrets set and People invited are its
 * `config_setup` field (CONFIG-SETUP-READ, read through configSetupApi.ts;
 * CONFIG-PANEL-E): Secrets set is done when nothing registered is needed and
 * not stored, and otherwise names the count and the first item; People
 * invited is done when someone other than the reader holds a role. A source
 * the server could not read is UNKNOWN, drawn with the server's reason, and
 * never counts as done; a step the read did not carry is unknown the same way.
 *
 * THE PANEL'S ROW BADGES (S1a) are derived from these same steps
 * (`configBadges`), so a row can never say what the strip does not.
 *
 * A CHECKLIST STEP THAT FAILED IS NEVER HIDDEN BY THE STRIP (QA1-FIX-10,
 * config-08, chrome-C20). The strip has three or four steps and Workspace
 * setup's checklist seven, so their counts differ by design; the strip names
 * the checklist's count beside its own link rather than pretending they are
 * one. But GitHub connected read done from `github_connected` alone while
 * the checklist's Enable orgs had failed, so the strip, the panel's meter and
 * GitHub access's lead all called GitHub finished over a failure. The
 * checklist steps done on the GitHub access page (`GITHUB_SIDE`) now hold
 * the strip's step 1 at their own failed or stale state, named.
 */

/** A strip step's state: the onboarding derivation's, or unknown where nothing measured it. */
export type SetupState = OnboardingStepState | 'unknown'

export interface SetupStep {
  key: 'github' | 'repositories' | 'secrets' | 'people'
  label: string
  state: SetupState
  /** The step's sub-line: its evidence, or why there is none. */
  line: string
  /** The Config page the step is done on (a router address). */
  to: string
}

/** What the strip and the panel's meter read: the shell's onboarding read and the workspace's kind. */
export interface SetupRead {
  doc: Result<OnboardingDoc>
  /** True in the person's own workspace, false in a group one, null while unread. */
  personal: boolean | null
}

export const SetupContext = createContext<SetupRead>({ doc: { status: 'loading', since: 0 }, personal: null })

const STATE_LINE: Readonly<Record<OnboardingStepState, string>> = {
  done: 'done',
  in_progress: 'in progress',
  failed: 'failed',
  stale: 'needs a re-check',
  todo: 'not yet',
}

/** The checklist steps whose fix is on GitHub access, after Connect GitHub: a failed or stale one holds strip step 1. */
const GITHUB_SIDE: readonly OnboardingStepName[] = ['app_installed', 'orgs_enabled']

/**
 * A done strip step held back by the checklist: the first of `names` that
 * failed, else the first that is stale, named as the checklist names it
 * ("Enable orgs failed"). Anything else leaves the step as measured: a step
 * still to do or in progress there is the checklist's to show, not a fault.
 */
function heldBy(doc: OnboardingDoc | null, measured: Pick<SetupStep, 'state' | 'line'>, names: readonly OnboardingStepName[]): Pick<SetupStep, 'state' | 'line'> {
  if (measured.state !== 'done' || doc === null || !Array.isArray(doc.steps)) return measured
  const side = doc.steps.filter((s) => names.includes(s.step))
  const held = side.find((s) => s.state === 'failed') ?? side.find((s) => s.state === 'stale')
  if (held === undefined) return measured
  return { state: held.state, line: `${STEP_LABEL[held.step] ?? held.step} ${STATE_LINE[held.state]}` }
}

function str(v: unknown): string | null {
  return typeof v === 'string' && v !== '' ? v : null
}

/** The steps, from the onboarding document (null: not read). Pure, for the tests and both drawings. */
export function setupSteps(doc: OnboardingDoc | null, personal: boolean): SetupStep[] {
  const setup = configSetupOf(doc)
  const derived = (name: 'github_connected' | 'repos_chosen') => (doc !== null && Array.isArray(doc.steps) ? doc.steps.find((s) => s.step === name) ?? null : null)
  const measured = (name: 'github_connected' | 'repos_chosen', evidence: (e: Record<string, unknown>) => string | null): Pick<SetupStep, 'state' | 'line'> => {
    const s = derived(name)
    if (doc === null) return { state: 'unknown', line: 'not read' }
    if (s === null) return { state: 'unknown', line: 'not in the setup read' }
    const ev = s.state === 'done' ? evidence(s.evidence ?? {}) : null
    return { state: s.state, line: ev ?? STATE_LINE[s.state] ?? s.state }
  }
  const steps: SetupStep[] = [
    {
      key: 'github',
      label: 'GitHub connected',
      to: 'config/github',
      ...heldBy(
        doc,
        measured('github_connected', (e) => {
          // `forge_login` is the name onboarding.py's github_connected evidence serves it under.
          const login = str(e.forge_login) ?? str(e.login) ?? str(e.github_login)
          return login === null ? null : `@${login}`
        }),
        GITHUB_SIDE,
      ),
    },
    {
      key: 'repositories',
      label: 'Repositories chosen',
      to: 'config/repositories',
      ...measured('repos_chosen', (e) => (Array.isArray(e.repositories) ? `${e.repositories.length} chosen` : null)),
    },
    { key: 'secrets', label: 'Secrets set', to: 'config/secrets', ...secretsState(doc, setup) },
  ]
  if (!personal) steps.push({ key: 'people', label: 'People invited', to: 'config/people', ...peopleState(doc, setup) })
  return steps
}

/** "3 to set: linear and 2 more": the count, and the first item by name. */
function toSet(n: number, first: string | null): string {
  if (first === null) return `${n} to set`
  return n > 1 ? `${n} to set: ${first} and ${n - 1} more` : `1 to set: ${first}`
}

/** Step 3 from `config_setup.secrets`: done only when the server read every source and found nothing missing. */
function secretsState(doc: OnboardingDoc | null, setup: ConfigSetup | null): Pick<SetupStep, 'state' | 'line'> {
  if (doc === null) return { state: 'unknown', line: 'not read' }
  const s = setup?.secrets ?? null
  if (s === null) return { state: 'unknown', line: 'not in the setup read' }
  if (s.needed === null) return { state: 'unknown', line: secretsUnreadReason(s) }
  if (s.needed === 0) return { state: 'done', line: 'nothing missing' }
  const first = s.items[0]
  return { state: 'todo', line: toSet(s.needed, first === undefined ? null : itemName(first)) }
}

/** Step 4 from `config_setup.people`: done when someone besides the reader holds a role. */
function peopleState(doc: OnboardingDoc | null, setup: ConfigSetup | null): Pick<SetupStep, 'state' | 'line'> {
  if (doc === null) return { state: 'unknown', line: 'not read' }
  const p = setup?.people ?? null
  if (p === null) return { state: 'unknown', line: 'not in the setup read' }
  // The server judged this workspace personal while `me` said group: say so rather than guess.
  if (!p.applies) return { state: 'unknown', line: p.reason ?? 'does not apply here' }
  if (p.done === null) return { state: 'unknown', line: p.reason ?? 'the roles could not be read' }
  if (!p.done) return { state: 'todo', line: 'only you so far' }
  const n = p.holders ?? 0
  return { state: 'done', line: n === 1 ? '1 other person holds a role' : `${n} other people hold a role` }
}

/**
 * A Config panel row's badge (S1a): done, a count, what is missing, or
 * unknown. `text` is what the row draws; `why` is its title and its name to
 * a screen reader, so a tick or a bare number is never the whole message.
 */
export interface ConfigBadge {
  /** `note` is something left that is nobody's warning: a lone admin's "only you" (S1a draws it dim). */
  tone: 'done' | 'count' | 'missing' | 'note' | 'unknown'
  text: string
  why: string
}

const MISSING: Readonly<Record<OnboardingStepState, string>> = {
  done: 'done',
  in_progress: 'in progress',
  failed: 'failed',
  stale: 're-check',
  todo: 'not yet',
}

/** A badge for a strip step, in the step's own state. */
function stepBadge(step: SetupStep | undefined, todo: string, done: ConfigBadge | null = null): ConfigBadge | null {
  if (step === undefined) return null
  if (step.state === 'unknown') return { tone: 'unknown', text: '—', why: `${step.label}: ${step.line}` }
  if (step.state === 'done') return done ?? { tone: 'done', text: '✓', why: `${step.label}: ${step.line}` }
  return { tone: 'missing', text: step.state === 'todo' ? todo : MISSING[step.state], why: `${step.label}: ${step.line}` }
}

/**
 * Each Config page's badge, keyed by its panel row (Spine.tsx PANEL_PAGES
 * config), from the SAME steps the strip draws and the same read. Workspace
 * Settings carries none: its setup part is the strip itself (S1a draws none).
 */
export function configBadges(doc: OnboardingDoc | null, personal: boolean): Readonly<Record<string, ConfigBadge>> {
  const steps = setupSteps(doc, personal)
  const by = (k: SetupStep['key']) => steps.find((s) => s.key === k)
  const setup = configSetupOf(doc)
  const out: Record<string, ConfigBadge> = {}
  const put = (key: string, b: ConfigBadge | null) => {
    if (b !== null) out[key] = b
  }
  put('github', stepBadge(by('github'), 'not connected'))
  const repos = by('repositories')
  const chosen = doc?.steps?.find((s) => s.step === 'repos_chosen')?.evidence?.repositories
  put('repositories', stepBadge(repos, 'none chosen', Array.isArray(chosen) ? { tone: 'count', text: String(chosen.length), why: `Repositories chosen: ${chosen.length}` } : null))
  const secrets = by('secrets')
  const needed = setup?.secrets?.needed ?? null
  put('secrets', stepBadge(secrets, needed === null ? 'to set' : `${needed} to set`))
  // MCP servers: the secrets step's MCP source alone, so its count is the
  // part of Secrets' count that is set from that page. NOTHING TO SET DRAWS
  // NO MARK (VQ-046): the setup read counts missing credentials, not
  // registrations, so a ✓ there read as "configured" over a workspace with no
  // MCP server at all. Registering one is optional; the page's lead says
  // whether any is registered.
  const mcp = setup?.secrets?.sources.mcp_servers
  if (doc !== null && setup?.secrets != null && mcp !== undefined) {
    const n = setup.secrets.items.filter((i) => i.kind === 'mcp_server').length
    put(
      'mcp-servers',
      mcp.needed === null
        ? { tone: 'unknown', text: '—', why: `MCP servers: ${mcp.reason ?? 'not read'}` }
        : n > 0
          ? { tone: 'missing', text: `${n} to set`, why: `MCP servers: ${n} need a secret nobody has stored` }
          : null,
    )
  } else put('mcp-servers', { tone: 'unknown', text: '—', why: `MCP servers: ${doc === null ? 'not read' : 'not in the setup read'}` })
  const people = by('people')
  const holders = setup?.people?.holders ?? null
  const peopleBadge = stepBadge(people, 'only you', holders === null ? null : { tone: 'count', text: String(holders), why: `People invited: ${people?.line ?? ''}` })
  put('people', peopleBadge !== null && people?.state === 'todo' ? { ...peopleBadge, tone: 'note' } : peopleBadge)
  return out
}

/** The badges for the shell's read, as the panel draws them. */
export function useConfigBadges(): Readonly<Record<string, ConfigBadge>> {
  const { doc, personal } = useContext(SetupContext)
  return configBadges(doc.status === 'ok' || doc.status === 'stale' ? doc.data : null, personal === true)
}

/** A Config panel row's badge. */
export function ConfigBadgeMark({ badge }: { badge: ConfigBadge | undefined }) {
  if (badge === undefined) return null
  return (
    <span className={`cfg-badge is-${badge.tone}`} title={badge.why} aria-label={badge.why} role="img">
      {badge.text}
    </span>
  )
}

/**
 * A STEP STILL TO DO IS NAMED AS A TASK (visual QA pass 2 admin2-03,
 * config2-04, 2026-10-29). The label is the done state ("GitHub connected"),
 * so "Next: GitHub connected, Enable orgs failed" read as if GitHub were
 * connected over the failure. A "Next:" line names what to do instead, and a
 * failed or stale step 1 (held by GitHub access's checklist steps) is a fix.
 */
export function stepAction(step: Pick<SetupStep, 'key' | 'state'>): string {
  switch (step.key) {
    case 'github':
      return step.state === 'failed' || step.state === 'stale' ? 'Fix GitHub access' : 'Connect GitHub'
    case 'repositories':
      return 'Choose repositories'
    case 'secrets':
      return 'Set secrets'
    case 'people':
      return 'Invite people'
  }
}

/** The first measured step not done: the one the strip calls next, for a page's lead to name. */
export function setupNext(steps: readonly SetupStep[]): SetupStep | null {
  return steps.find((s) => s.state !== 'done' && s.state !== 'unknown') ?? null
}

function nextOf(steps: readonly SetupStep[]): string | null {
  return setupNext(steps)?.key ?? null
}

/** The strip's count, in words: measured done of all, and how many nothing measured. */
export function setupCount(steps: readonly SetupStep[]): { done: number; of: number; unknown: number } {
  return {
    done: steps.filter((s) => s.state === 'done').length,
    of: steps.length,
    unknown: steps.filter((s) => s.state === 'unknown').length,
  }
}

/** The steps the shell's read gives, for the strip and the meter alike. */
export function useSetupSteps(): SetupStep[] {
  const { doc, personal } = useContext(SetupContext)
  const data = doc.status === 'ok' || doc.status === 'stale' ? doc.data : null
  return setupSteps(data, personal === true)
}

/** Whether the read says something is left to do: the spine's dot on Config. */
export function setupUnfinished(steps: readonly SetupStep[]): boolean {
  return steps.some((s) => s.state !== 'done' && s.state !== 'unknown')
}

function StepMark({ state }: { state: SetupState }) {
  // A tick for done, a filled ring for in hand, a bare ring otherwise: never a
  // tick for a step nothing measured.
  return (
    <svg className="cfg-mark" viewBox="0 0 20 20" aria-hidden="true" focusable="false">
      <circle cx="10" cy="10" r="8.5" />
      {state === 'done' && <path d="m6 10.4 2.7 2.7L14.2 7.6" />}
      {state === 'unknown' && <path d="M7 10h6" />}
    </svg>
  )
}

/**
 * THE AGE OF THE READING A STEP CAME FROM (CONFIG-STRIP-DONE): the onboarding
 * read's `derived_at`, as "checked 5m ago". Every step is one read's, so they
 * share it. `ageSpan`'s buckets, so under five seconds is still "2s", never
 * "just now": a read that carried no time, or one that does not parse, has no
 * age to show and says why, rather than borrowing the render's clock and
 * reading as fresh.
 */
export type ReadingAge = { age: string; why: null } | { age: null; why: string }

export function readingAge(derivedAt: unknown, now: number): ReadingAge {
  const at = str(derivedAt)
  if (at === null) return { age: null, why: 'the setup read carried no time' }
  const t = Date.parse(at)
  if (!Number.isFinite(t)) return { age: null, why: "the setup read's time could not be read" }
  return { age: `checked ${ageSpan(now - t)} ago`, why: null }
}

function Checked({ age }: { age: ReadingAge }) {
  return age.age ?? (
    <>
      checked <Dash why={age.why} />
    </>
  )
}

/**
 * SHOW STEPS ON A FINISHED STRIP, REMEMBERED PER DEVICE. Storage can refuse
 * (a private window, a blocked origin): the choice then lasts the page.
 */
const SHOW_STEPS_KEY = 'swarm.config.setupSteps'

function readShowSteps(): boolean {
  try {
    return window.localStorage.getItem(SHOW_STEPS_KEY) === 'shown'
  } catch {
    return false
  }
}

function writeShowSteps(shown: boolean) {
  try {
    if (shown) window.localStorage.setItem(SHOW_STEPS_KEY, 'shown')
    else window.localStorage.removeItem(SHOW_STEPS_KEY)
  } catch {
    // Not remembered; this page still draws what was asked.
  }
}

/**
 * THE CHECKLIST'S OWN COUNT, NAMED (config-08, chrome-C20): "Setup
 * checklist: 4 of 7 done", Workspace setup's checklist count in its own words
 * (Onboarding's `countLabel`), on the strip's link to it. The strip counts
 * its three or four steps, and says "steps"; without this the two counts on
 * one screen read as one count that disagrees with itself. It said
 * "Workspace setup: 4 of 7 done" beside "1 of 3 done", which named neither
 * list (pass 2 config2-03, admin-shell-10). Null while the read has no steps.
 */
export function checklistCount(doc: OnboardingDoc | null): string | null {
  if (doc === null || !Array.isArray(doc.steps) || doc.steps.length === 0) return null
  return `Setup checklist: ${countLabel(doc)}`
}

/**
 * The strip across the top of a Config page.
 *
 * FINISHED, IT FOLDS (config-e.html `stripD`): with every step done, measured,
 * the strip is one line, "Setup complete · N of N · checked <age> · Show
 * steps", on a phone as on a desk, so a finished workspace stops paying four
 * cells of height on every Config page. An unknown step is not done, so a
 * strip with one never folds. Show steps unfolds the steps and the compact
 * strip as before, with Hide steps to fold again.
 */
export function SetupStrip({ go }: { go: (to: string) => void }) {
  const { doc } = useContext(SetupContext)
  const data = doc.status === 'ok' || doc.status === 'stale' ? doc.data : null
  const steps = useSetupSteps()
  const now = useNow()
  const [shown, setShown] = useState(readShowSteps)
  const next = nextOf(steps)
  const { done, of, unknown } = setupCount(steps)
  // Only a step drawn from a read has a reading to age; "not read" has none,
  // and a step the read did not measure draws no age on its line either.
  const age = data === null ? null : readingAge(data.derived_at, now)
  const complete = of > 0 && done === of
  const checklist = checklistCount(data)
  const show = (v: boolean) => {
    setShown(v)
    writeShowSteps(v)
  }
  const link = (to: string) => ({
    href: addressToPath(to),
    onClick: (e: MouseEvent) => {
      if (!routedClick(e)) return
      e.preventDefault()
      go(to)
    },
  })
  if (complete && !shown) {
    return (
      <section className="cfg-strip is-complete" aria-label="Workspace setup">
        <div className="cfg-done">
          <StepMark state="done" />
          <b>Setup complete</b>
          <small>
            {done} of {of}
            {age !== null && (
              <>
                {' · '}
                <Checked age={age} />
              </>
            )}
          </small>
          <button type="button" className="ctl-link" aria-expanded={false} onClick={() => show(true)}>
            Show steps
          </button>
          {/* The strip's steps are done but the checklist's are not: say whose count is behind. */}
          {checklist !== null && data?.complete !== true && (
            <a className="ctl-link" {...link(WORKSPACE_SETUP_ADDRESS)}>
              {checklist}
            </a>
          )}
        </div>
      </section>
    )
  }
  const hide = complete ? (
    <button type="button" className="ctl-link" aria-expanded={true} onClick={() => show(false)}>
      Hide steps
    </button>
  ) : null
  const nextStep = steps.find((s) => s.key === next) ?? null
  return (
    <section className="cfg-strip" aria-label="Workspace setup">
      {/* THE PHONE'S STRIP (S2d): one line of count, the meter's segments and
          the next step, where four stacked steps took a third of the screen.
          CSS draws this or the list below, never both. */}
      <div className="cfg-compact">
        <div className="cfg-compact-h">
          <b>
            Setup {done} of {of} steps
          </b>
          {hide ?? (
            <a className="ctl-link" {...link(nextStep === null ? WORKSPACE_SETUP_ADDRESS : nextStep.to)}>
              {nextStep === null ? 'Workspace setup' : stepAction(nextStep)} ›
            </a>
          )}
        </div>
        <div className="cfg-meter-bar" aria-hidden="true">
          {steps.map((s) => (
            <i key={s.key} className={`is-${s.state}${s.key === next ? ' is-next' : ''}`} />
          ))}
        </div>
        <small>
          {nextStep !== null ? `Next: ${stepAction(nextStep)} (${nextStep.line})` : unknown > 0 ? `${unknown} not measured` : 'Every step done'}
          {age !== null && (
            <>
              {' · '}
              <Checked age={age} />
            </>
          )}
        </small>
      </div>
      <ol className="cfg-steps" aria-label="Workspace setup steps" style={{ ['--cfg-n' as string]: steps.length }}>
        {steps.map((s, i) => (
          <li key={s.key} className={`cfg-step is-${s.state}${s.key === next ? ' is-next' : ''}`}>
            <StepMark state={s.state} />
            <a className="cfg-step-main" {...link(s.to)}>
              {/* A FAILED STEP IS TITLED AS ITS TASK (review of QA2-FIX-15):
                  "1. GitHub connected" in red over its failure read as done. */}
              <b>
                {i + 1}. {s.state === 'failed' || s.state === 'stale' ? stepAction(s) : s.label}
              </b>
              <small>{s.key === next ? `Next: ${s.line}` : s.line}</small>
              {/* Its own line, so the step's line stays word for word what the panel's badge names.
                  Only a measured step has a reading to age: an unknown one's line already says why. */}
              {age !== null && s.state !== 'unknown' && (
                <small className="cfg-step-age">
                  <Checked age={age} />
                </small>
              )}
            </a>
          </li>
        ))}
      </ol>
      <div className="cfg-count">
        <b>
          {done} of {of} steps done
        </b>
        {unknown > 0 && <small>{unknown} not measured</small>}
        {hide}
        <a className="ctl-link" {...link(WORKSPACE_SETUP_ADDRESS)}>
          {checklist ?? 'Workspace setup'}
        </a>
      </div>
    </section>
  )
}

/** The Config panel's meter (S1a): the same count, one segment per step. */
export function SetupMeter() {
  const steps = useSetupSteps()
  const next = nextOf(steps)
  const { done, of, unknown } = setupCount(steps)
  const why = unknown > 0 ? `${done} of ${of} measured done; ${unknown} not measured yet` : `${done} of ${of} done`
  return (
    <div className="sk-meter cfg-meter" title={why}>
      <div className="cfg-meter-h">
        <span>Setup</span>
        {/* THE UNIT, AS THE STRIP NAMES IT (review of QA2-FIX-15). */}
        <b>
          {done} of {of} steps
        </b>
      </div>
      <div className="cfg-meter-bar" aria-hidden="true">
        {steps.map((s) => (
          <i key={s.key} className={`is-${s.state}${s.key === next ? ' is-next' : ''}`} />
        ))}
      </div>
    </div>
  )
}

/**
 * ONE WORKSPACE'S PROGRESS, FOR ADMIN › WORKSPACES YOU ADMIN (S10a): the
 * strip's count, its segments and its next step, from that workspace's own
 * read, so an admin of several sees which one is behind. The same steps the
 * strip draws (`setupSteps`), never a second derivation. `onNext` opens the
 * next step's page in that workspace; the page decides how (it selects the
 * workspace first).
 */
export function SetupProgress({ name, steps, onNext }: { name: string; steps: readonly SetupStep[]; onNext?: (step: SetupStep, e: MouseEvent) => void }) {
  const next = nextOf(steps)
  const nextStep = steps.find((s) => s.key === next) ?? null
  const { done, of, unknown } = setupCount(steps)
  return (
    <div className="cfg-progress" role="group" aria-label={`${name} setup`}>
      <div className="cfg-progress-h">
        <b className={`cfg-progress-n${done === of ? ' is-done' : ''}`}>
          Setup {done} of {of} steps
        </b>
        {unknown > 0 && <small>{unknown} not measured</small>}
      </div>
      <div className="cfg-meter-bar">
        {steps.map((s) => (
          <i key={s.key} className={`is-${s.state}${s.key === next ? ' is-next' : ''}`} title={`${s.label}: ${s.line}`} />
        ))}
      </div>
      <small className="cfg-progress-next">
        {nextStep !== null ? (
          <>
            Next:{' '}
            <a href={addressToPath(nextStep.to)} onClick={(e) => onNext?.(nextStep, e)}>
              {stepAction(nextStep)}
            </a>{' '}
            ({nextStep.line})
          </>
        ) : done === of ? (
          'Setup complete'
        ) : (
          `${unknown} not measured: ${steps
            .filter((s) => s.state === 'unknown')
            .map((s) => `${s.label}, ${s.line}`)
            .join('; ')}`
        )}
      </small>
    </div>
  )
}

/**
 * The workspace furthest behind, by steps done, among `read` -- null unless
 * at least two were read and one has strictly fewer done than another, so
 * the page never calls a tie "behind".
 */
export function behindOf<T extends { steps: readonly SetupStep[] }>(read: readonly T[]): T | null {
  if (read.length < 2) return null
  const done = read.map((r) => setupCount(r.steps).done)
  const min = Math.min(...done)
  if (min === Math.max(...done)) return null
  return read[done.indexOf(min)] ?? null
}
