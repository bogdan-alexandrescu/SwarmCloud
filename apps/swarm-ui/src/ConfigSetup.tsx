import { createContext, useContext, type MouseEvent } from 'react'
import { routedClick } from './components'
import type { Result } from './fetch'
import { WORKSPACE_SETUP_ADDRESS, addressToPath } from './paths'
import type { OnboardingDoc, OnboardingStepState } from './types'
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
 * state the server derived. Secrets set and People invited have no read yet --
 * proposal E's "needed, not set" count and an invited-people count are not
 * served by any route -- so they are UNKNOWN, drawn as such, and never count
 * as done. A step the read did not carry is unknown the same way.
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

function str(v: unknown): string | null {
  return typeof v === 'string' && v !== '' ? v : null
}

/** The steps, from the onboarding document (null: not read). Pure, for the tests and both drawings. */
export function setupSteps(doc: OnboardingDoc | null, personal: boolean): SetupStep[] {
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
      ...measured('github_connected', (e) => {
        const login = str(e.login) ?? str(e.github_login)
        return login === null ? null : `@${login}`
      }),
    },
    {
      key: 'repositories',
      label: 'Repositories chosen',
      to: 'config/repositories',
      ...measured('repos_chosen', (e) => (Array.isArray(e.repositories) ? `${e.repositories.length} chosen` : null)),
    },
    // No route serves what a workspace still needs set, so this is never measured here.
    { key: 'secrets', label: 'Secrets set', to: 'config/secrets', state: 'unknown', line: 'not measured yet' },
  ]
  if (!personal) steps.push({ key: 'people', label: 'People invited', to: 'config/people', state: 'unknown', line: 'not measured yet' })
  return steps
}

/** The first measured step not done: the one the strip calls next. */
function nextOf(steps: readonly SetupStep[]): string | null {
  return steps.find((s) => s.state !== 'done' && s.state !== 'unknown')?.key ?? null
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

/** The strip across the top of a Config page. */
export function SetupStrip({ go }: { go: (to: string) => void }) {
  const steps = useSetupSteps()
  const next = nextOf(steps)
  const { done, of, unknown } = setupCount(steps)
  const link = (to: string) => ({
    href: addressToPath(to),
    onClick: (e: MouseEvent) => {
      if (!routedClick(e)) return
      e.preventDefault()
      go(to)
    },
  })
  const nextStep = steps.find((s) => s.key === next) ?? null
  return (
    <section className="cfg-strip" aria-label="Workspace setup">
      {/* THE PHONE'S STRIP (S2d): one line of count, the meter's segments and
          the next step, where four stacked steps took a third of the screen.
          CSS draws this or the list below, never both. */}
      <div className="cfg-compact">
        <div className="cfg-compact-h">
          <b>
            Setup {done} of {of}
          </b>
          <a className="ctl-link" {...link(nextStep === null ? WORKSPACE_SETUP_ADDRESS : nextStep.to)}>
            {nextStep === null ? 'Workspace setup' : nextStep.label} ›
          </a>
        </div>
        <div className="cfg-meter-bar" aria-hidden="true">
          {steps.map((s) => (
            <i key={s.key} className={`is-${s.state}${s.key === next ? ' is-next' : ''}`} />
          ))}
        </div>
        <small>
          {nextStep !== null ? `Next: ${nextStep.label}, ${nextStep.line}` : unknown > 0 ? `${unknown} not measured` : 'Every step done'}
        </small>
      </div>
      <ol className="cfg-steps" aria-label="Workspace setup steps" style={{ ['--cfg-n' as string]: steps.length }}>
        {steps.map((s, i) => (
          <li key={s.key} className={`cfg-step is-${s.state}${s.key === next ? ' is-next' : ''}`}>
            <StepMark state={s.state} />
            <a className="cfg-step-main" {...link(s.to)}>
              <b>
                {i + 1}. {s.label}
              </b>
              <small>{s.key === next ? `Next: ${s.line}` : s.line}</small>
            </a>
          </li>
        ))}
      </ol>
      <div className="cfg-count">
        <b>
          {done} of {of} done
        </b>
        {unknown > 0 && <small>{unknown} not measured</small>}
        <a className="ctl-link" {...link(WORKSPACE_SETUP_ADDRESS)}>
          Workspace setup
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
        <b>
          {done} of {of}
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
