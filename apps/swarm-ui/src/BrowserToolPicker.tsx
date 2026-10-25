import { useEffect, useId, useState, type MouseEvent } from 'react'
import { loadJudgment, loadProviderKeys, type JevStatus, type ProviderKeys } from './api'
import { routedClick } from './components'
import { addressToPath } from './paths'
import type { RunnerProfile } from './types'

/**
 * WHICH BROWSER TOOL A BROWSER TASK'S AGENT DRIVES (BSTATUS-SUBMIT, owner
 * decision 2026-10-14: "Picker on Submit, Auto by default").
 *
 * DRAWN FROM THE DECLARATION, NEVER FROM A LIST OF OUR OWN. swarm-api refuses
 * any `input` key a profile does not declare with 422 `invalid_input`
 * (contract request 25), so a picker drawn for a profile that does not
 * declare `browser_tool` would invite exactly that refusal. It is read off the
 * served `input_contract.inputs.browser_tool`, and its segments are that
 * declaration's `choices`; with no declaration there is no picker.
 *
 * AUTO IS THE ABSENCE OF THE KEY. "The agent picks per step" is what the
 * runner does with no `browser_tool`, so Auto sends nothing -- a task that
 * names it is the same task as one that does not, and the request body should
 * say so by not carrying it. Auto is drawn first whether or not the
 * declaration lists it.
 *
 * WHETHER JEV CAN RUN IS THE WORKSPACE'S JUDGMENT BACKEND'S QUESTION (lane
 * JEV-OFF, part of #1345). `GET /v1/judgment` says which backend this
 * workspace judges with and whether it can run: an internal backend needs
 * the tailnet route to your internal decision API and no key; TypeSafe needs
 * a key.
 * Measured 2026-10-15: with the route off, this picker offered Jev, and the
 * task silently ran on Playwright. So Jev is disabled, with the API's own
 * reason, whenever the backend cannot run -- and before the read answers, or
 * when it fails, never enabled on a guess.
 *
 * ON THE TYPESAFE BACKEND JEV SENDS PAGE TEXT TO TYPESAFE, under the key `GET
 * /v1/provider-keys/typesafe` says a task here would use. With no key it is
 * disabled with where to add one. Picked, the data notice is printed inline
 * beside the control rather than behind a modal: it is a fact about the
 * pick, read while making it. An internal backend needs no key, so it is
 * never asked for one.
 *
 * THE NETWORK ROW IS READ-ONLY and drawn only from what the API serves on
 * the profile (`egress`: `{mode, country}`, the live session's own shape).
 * Nothing serves it to a console caller yet, so the row is hidden rather
 * than guessed: "Direct" printed over a residential workspace is the claim
 * a person would act on.
 */

/** The task input the pick travels under. */
export const BROWSER_TOOL_KEY = 'browser_tool'
/** The default, which sends nothing. */
export const AUTO = 'auto'
const JEV = 'jev'

const LABEL: Readonly<Record<string, string>> = {
  auto: 'Auto',
  playwright: 'Playwright',
  'dev-browser': 'dev-browser',
  jev: 'Jev',
}

const HINT: Readonly<Record<string, string>> = {
  auto: 'the agent picks per step',
  playwright: 'step-by-step browser tools',
  'dev-browser': 'fast scripted multi-step flows',
  jev: 'goal-driven clicks and reading long pages — sends page text to TypeSafe',
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

/**
 * The tools this profile declares, Auto first, or null when it declares no
 * `browser_tool` (or this API does not serve declared inputs at all).
 */
export function declaredBrowserTools(profile: RunnerProfile | null | undefined): string[] | null {
  const contract: unknown = profile?.input_contract
  const inputs = isRecord(contract) ? contract.inputs : undefined
  const decl = isRecord(inputs) ? inputs[BROWSER_TOOL_KEY] : undefined
  if (!isRecord(decl) || !Array.isArray(decl.choices)) return null
  const choices = decl.choices.filter((c): c is string => typeof c === 'string' && c !== '' && c !== AUTO)
  return [AUTO, ...new Set(choices)]
}

export interface BrowserEgress {
  mode: 'residential' | 'direct'
  country: string | null
}

/** The profile's served egress, or null when none is served or it is not a shape this build knows. */
export function profileEgress(profile: RunnerProfile | null | undefined): BrowserEgress | null {
  const raw: unknown = (profile as { egress?: unknown } | null | undefined)?.egress
  if (!isRecord(raw) || (raw.mode !== 'residential' && raw.mode !== 'direct')) return null
  return { mode: raw.mode, country: typeof raw.country === 'string' && raw.country !== '' ? raw.country : null }
}

/** What `GET /v1/provider-keys/typesafe` said a task here would use. */
export type TypesafeKey =
  | { kind: 'reading' }
  | { kind: 'unread'; detail: string }
  | { kind: 'read'; uses: ProviderKeys['resolution']['uses'] }

/** One read of the TypeSafe key status, made only while `wanted` (a form that draws the picker). */
export function useTypesafeKey(wanted: boolean): TypesafeKey {
  const [key, setKey] = useState<TypesafeKey>({ kind: 'reading' })
  useEffect(() => {
    if (!wanted) return
    let live = true
    Promise.resolve()
      .then(() => loadProviderKeys('typesafe'))
      .then(
        (r) => {
          if (!live) return
          if (r.status === 'ok' || r.status === 'stale') setKey({ kind: 'read', uses: r.data.resolution.uses })
          else if (r.status === 'error') setKey({ kind: 'unread', detail: r.error.message })
          else setKey({ kind: 'read', uses: 'none' })
        },
        (err: unknown) => {
          if (live) setKey({ kind: 'unread', detail: err instanceof Error ? err.message : 'The read did not complete.' })
        },
      )
    return () => {
      live = false
    }
  }, [wanted])
  return key
}

/** What `GET /v1/judgment` said about this workspace's Jev backend. */
export type JevJudgment =
  | { kind: 'reading' }
  | { kind: 'unread'; detail: string }
  | { kind: 'read'; jev: JevStatus }

/** One read of the workspace's judgment backend, made only while `wanted`. */
export function useJevJudgment(wanted: boolean): JevJudgment {
  const [judgment, setJudgment] = useState<JevJudgment>({ kind: 'reading' })
  useEffect(() => {
    if (!wanted) return
    let live = true
    Promise.resolve()
      .then(() => loadJudgment())
      .then(
        (r) => {
          if (!live) return
          if (r.status === 'ok' || r.status === 'stale') setJudgment({ kind: 'read', jev: r.data.jev })
          else if (r.status === 'error') setJudgment({ kind: 'unread', detail: r.error.message })
          else setJudgment({ kind: 'unread', detail: 'The judgment backend read did not answer.' })
        },
        (err: unknown) => {
          if (live) setJudgment({ kind: 'unread', detail: err instanceof Error ? err.message : 'The read did not complete.' })
        },
      )
    return () => {
      live = false
    }
  }, [wanted])
  return judgment
}

function onTypesafe(judgment: JevJudgment): boolean {
  return judgment.kind === 'read' && judgment.jev.backend === 'typesafe'
}

/**
 * Whether Jev may be picked: the backend read answered and it can run -- for
 * TypeSafe, a key resolves; for an internal backend, the API says available.
 */
export function jevAllowed(key: TypesafeKey, judgment: JevJudgment): boolean {
  if (judgment.kind !== 'read') return false
  if (onTypesafe(judgment)) return key.kind === 'read' && key.uses !== 'none'
  return judgment.jev.available
}

/** The task input with the pick in it: nothing added for Auto. */
export function withBrowserTool(input: Record<string, unknown>, tool: string): Record<string, unknown> {
  if (tool === AUTO) return input
  const out: Record<string, unknown> = Object.create(null) as Record<string, unknown>
  Object.assign(out, input)
  out[BROWSER_TOOL_KEY] = tool
  return out
}

const KEYS_ADDRESS = 'config/secrets'

/** Config › Secrets, where the TypeSafe key is set since SSC-UI (Settings › API keys until then), routed in-app like the panel's own link (Spine.tsx). */
function KeysLink() {
  const go = (e: MouseEvent<HTMLAnchorElement>) => {
    if (!routedClick(e)) return
    e.preventDefault()
    window.history.pushState(null, '', addressToPath(KEYS_ADDRESS))
    window.dispatchEvent(new PopStateEvent('popstate'))
  }
  return <a href={addressToPath(KEYS_ADDRESS)} onClick={go}>Config › Secrets</a>
}

function JevOff({ typesafe, judgment }: { typesafe: TypesafeKey; judgment: JevJudgment }) {
  if (judgment.kind === 'reading') return <p className="btp-jev-off">Checking whether Jev can run…</p>
  if (judgment.kind === 'unread') {
    return (
      <p className="btp-jev-off" title={judgment.detail}>
        Which judgment backend this workspace uses could not be read, so Jev is off.
      </p>
    )
  }
  if (!onTypesafe(judgment)) {
    const why = judgment.jev.reason ?? 'the API gave no reason'
    return (
      <p className="btp-jev-off">
        Jev is unavailable: this workspace judges with {judgment.jev.label}, and {why}. An agent that asks for it runs on
        Playwright.
      </p>
    )
  }
  if (typesafe.kind === 'reading') return <p className="btp-jev-off">Checking for a TypeSafe key…</p>
  if (typesafe.kind === 'unread') {
    return (
      <p className="btp-jev-off" title={typesafe.detail}>
        Whether a TypeSafe key exists could not be read, so Jev is off. <KeysLink />
      </p>
    )
  }
  return <p className="btp-jev-off">Add a TypeSafe key in <KeysLink /></p>
}

const JEV_INTERNAL_HINT = 'goal-driven clicks and reading long pages — judged inside your network'

export function BrowserToolPicker({ tools, value, onChange, typesafe, judgment, egress }: {
  tools: readonly string[]
  value: string
  onChange: (tool: string) => void
  typesafe: TypesafeKey
  judgment: JevJudgment
  egress: BrowserEgress | null
}) {
  const id = useId()
  const [pointed, setPointed] = useState<string | null>(null)
  const jevOn = jevAllowed(typesafe, judgment)
  const internal = judgment.kind === 'read' && !onTypesafe(judgment)
  const hint = (t: string) => (t === JEV && internal ? JEV_INTERNAL_HINT : HINT[t])
  const shown = pointed ?? value
  const whose = typesafe.kind === 'read' && typesafe.uses === 'tenant' ? 'the tenant key' : 'your key'
  return (
    <div className="btp">
      <div className="btp-row">
        <span className="btp-h" id={`${id}-h`}>Browser tool</span>
        <div className="btp-seg" role="radiogroup" aria-labelledby={`${id}-h`} onMouseLeave={() => setPointed(null)}>
          {tools.map((t) => {
            const off = t === JEV && !jevOn
            return (
              <label key={t} className={`btp-opt${t === value ? ' is-on' : ''}${off ? ' is-off' : ''}`}
                title={hint(t)} onMouseEnter={() => setPointed(t)}>
                <input type="radio" name={`${id}-tool`} value={t} checked={t === value} disabled={off}
                  aria-describedby={hint(t) === undefined ? undefined : `${id}-${t}`}
                  onChange={() => onChange(t)} onFocus={() => setPointed(t)} onBlur={() => setPointed(null)} />
                <span>{LABEL[t] ?? t}</span>
                {hint(t) !== undefined && <span hidden id={`${id}-${t}`}>{hint(t)}</span>}
              </label>
            )
          })}
        </div>
      </div>
      <p className="btp-hint" aria-hidden="true">{hint(shown) ?? ''}</p>
      {tools.includes(JEV) && !jevOn && <JevOff typesafe={typesafe} judgment={judgment} />}
      {value === JEV && jevOn && judgment.kind === 'read' && (
        internal
          ? <p className="btp-notice">Jev judges pages with {judgment.jev.label}: their text stays inside your network.</p>
          : <p className="btp-notice">Jev sends the text of the pages it reads to TypeSafe, using {whose}.</p>
      )}
      {egress !== null && (
        <p className="btp-net">
          <span className="btp-h">Network</span>{' '}
          <span>{egress.mode === 'residential' ? `Residential${egress.country === null ? '' : ` · ${egress.country.toUpperCase()}`}` : 'Direct'}</span>
        </p>
      )}
    </div>
  )
}
