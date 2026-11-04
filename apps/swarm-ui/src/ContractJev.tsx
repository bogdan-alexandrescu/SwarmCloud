/**
 * JEV IN THE WORK CONTRACT, SHOWN TO THE PERSON WHO DECIDES
 * (docs/design/agent-quality/jev-contract.md section 13, lane 7).
 *
 * Two things the owner chose Jev for, each drawn where its decision is made:
 *
 *   * THE SUBMISSION REFUSAL (section 4.5). A strict contract Jev found
 *     requirements of that no worker could enforce is refused with a 422
 *     `validation_failed` whose `detail.jev.requirements` carries, per
 *     requirement, Jev's class, its reason code and the probability. The task
 *     and the workflow forms draw one line per requirement, in words, labelled
 *     as Jev's review. A requirement's text is never echoed back by the API,
 *     so the line names it by id.
 *   * THE ACCEPTANCE JUDGMENT (sections 5.4 and 8.2). `contract_acceptance`
 *     is the record swarm-api stores on the step (`attemptstate.contract_verdict`,
 *     `contractreask`): each judged requirement's verdict, the item it cites
 *     -- a changed file at the judged commit, or a mechanical result -- its
 *     reason and probability; or `not_run` with why. The approval screen draws
 *     it as a card labelled as Jev's judgment, and says plainly when the
 *     control plane re-asked three times and is now handing it to a person.
 *
 * A MODEL JUDGMENT IS NOT AN AUTHORIZATION (section 1, rule 1). A `met` here
 * is evidence beside the decision; the card says so, and nothing on it
 * approves or rejects. Only closed values are read: a class, a reason or a
 * verdict this file does not know is shown verbatim, never as a known one,
 * and a probability outside [0, 1] is not drawn as a percentage.
 */
import type { ReactNode } from 'react'
import './styles/contract-jev.css'

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}
const str = (v: unknown): string | null => (typeof v === 'string' && v !== '' ? v : null)
const num = (v: unknown): number | null => (typeof v === 'number' && Number.isFinite(v) ? v : null)

/** `p` as a percentage, or null when it is not a probability. */
function percent(p: number | null): string | null {
  return p === null || p < 0 || p > 1 ? null : `${Math.round(p * 100)}%`
}

function short(sha: string): string {
  return /^[0-9a-f]{40}$/.test(sha) ? sha.slice(0, 7) : sha
}

/* ---- the submission refusal (JC1) ------------------------------------------- */

/** `workcontract.ContractNotEnforceable.code`: the refusal's `detail.reason`. */
export const NOT_ENFORCEABLE = 'work_contract_not_enforceable'

/** Jev's classes (`contractjudge.REVIEW_CLASSES`, and `not_judged`), as the person reads them. */
export const REVIEW_CLASS_WORDS: Readonly<Record<string, string>> = {
  mechanical: 'the proof it names can show it',
  judgeable: 'a judge reading the diff must decide it',
  unenforceable: 'nothing observable when the step ends decides it',
  not_judged: 'Jev did not judge it',
}

/** `contractjudge.REVIEW_REASONS`, as the person reads them; `none` says nothing more. */
export const REVIEW_REASON_WORDS: Readonly<Record<string, string>> = {
  vague_outcome: 'the outcome it asks for is vague',
  unobservable_at_step_end: 'nothing at the step’s end shows it',
  outside_the_repository: 'it is about something outside the repository',
  depends_on_a_later_event: 'it depends on something that happens after the step',
  subjective: 'it is a matter of taste',
  proof_kind_mismatch: 'the proof it names cannot show it',
  several_requirements_in_one: 'it is several requirements in one',
}

export interface JevClass {
  id: string
  cls: string
  reason: string | null
  p: number | null
}

export interface ContractRefusal {
  contractId: string | null
  revision: number | null
  backend: string | null
  requirements: JevClass[]
}

/**
 * Section 4.5's refusal out of a 422's `detail`, or null. Only a detail with
 * Jev's part is Jev's: the mechanical compile's refusal of an unenforced
 * component carries the same `reason` and no `jev`, and stays the forms'
 * ordinary refusal.
 */
export function contractRefusal(detail: unknown): ContractRefusal | null {
  if (!isRecord(detail) || detail.reason !== NOT_ENFORCEABLE || !isRecord(detail.jev)) return null
  const rows = Array.isArray(detail.jev.requirements) ? detail.jev.requirements : []
  const requirements: JevClass[] = []
  for (const r of rows) {
    if (!isRecord(r) || str(r.id) === null) continue
    requirements.push({ id: str(r.id)!, cls: str(r.class) ?? 'not_judged', reason: str(r.reason), p: num(r.p) })
  }
  return {
    contractId: str(detail.contract_id),
    revision: num(detail.revision),
    backend: str(detail.jev.backend),
    requirements,
  }
}

/** The `detail` keys the refusal panel says; the workflow form's facts leave them out. */
export const REFUSAL_KEYS: ReadonlySet<string> = new Set(['reason', 'contract_id', 'revision', 'digest', 'unenforced', 'jev'])

function Words({ code, words }: { code: string | null; words: Readonly<Record<string, string>> }) {
  if (code === null || code === 'none') return null
  const said = words[code]
  return said === undefined ? <code>{code}</code> : <>{said}</>
}

/** The refusal, one line per requirement Jev refused, labelled as Jev's review. */
export function ContractRefusalPanel({ refusal }: { refusal: ContractRefusal }) {
  const named = refusal.contractId === null ? 'This work contract' : <>Work contract <code>{refusal.contractId}</code></>
  return (
    <div className="cj-refusal" data-testid="cj-refusal">
      <p className="cj-lead">
        {named}
        {refusal.revision !== null && <> (revision {refusal.revision})</>} is strict, and Jev’s review found{' '}
        {refusal.requirements.length === 1 ? 'a requirement' : 'requirements'} no worker could enforce:
      </p>
      <ul className="cj-list" aria-label="Jev’s review, per requirement">
        {refusal.requirements.map((r) => (
          <li key={r.id} className="cj-row" data-requirement={r.id}>
            <span className="cj-id mono">{r.id}</span>
            <span className="cj-say">
              <span className="cj-by">Jev’s review</span>{' '}
              <Words code={r.cls} words={REVIEW_CLASS_WORDS} />
              {r.reason !== null && r.reason !== 'none' && (
                <>
                  {': '}
                  <Words code={r.reason} words={REVIEW_REASON_WORDS} />
                </>
              )}
              {percent(r.p) !== null && <span className="cj-p"> · confidence {percent(r.p)}</span>}
            </span>
          </li>
        ))}
      </ul>
      <p className="cj-note">
        Rewrite each one as something the step’s end can show, or name the proof that shows it, then submit again.
        {refusal.backend !== null && <> Judged by <code>{refusal.backend}</code>.</>}
      </p>
    </div>
  )
}

/* ---- the acceptance judgment (JC2) ------------------------------------------ */

/** `contractjudge.VERDICTS`, as each row's chip says it. */
export const VERDICT_WORDS: Readonly<Record<string, string>> = {
  met: 'met',
  unmet: 'unmet',
  cannot_judge: 'Jev could not judge it',
}

/** `contractjudge.JUDGMENT_REASONS`, as the person reads them. */
export const JUDGMENT_REASON_WORDS: Readonly<Record<string, string>> = {
  not_in_diff: 'the diff does not do it',
  partially_done: 'it is partly done',
  contradicted_by_tests: 'the tests contradict it',
  wrong_place: 'it is done in the wrong place',
  scope_exceeded: 'the change goes past its scope',
  evidence_missing: 'what deciding it needs is missing',
  satisfied: 'the diff does it',
}

/** The mechanical proof kinds (`workcontract.VERIFICATION_KINDS` less `judgment`). */
const MECHANICAL_WORDS: Readonly<Record<string, string>> = {
  unit_tests: 'unit tests',
  ci: 'CI',
  review: 'the review',
  browser_check: 'the browser check',
  artifact_present: 'the artifact',
}

/** Why a step is the person's now (`contractreask.OWNER_*`). */
export const OWNER_REASON_WORDS: Readonly<Record<string, string>> = {
  send_back_cap: 'Jev judged it short and it was already sent back twice, so it is yours to decide.',
  attempts_exhausted: 'Jev judged it short and no attempt is left for another send-back, so it is yours to decide.',
  reask_exhausted: 'The control plane re-asked Jev three times over 30 minutes without a verdict, so it is yours to decide.',
  unmet_after_attempt: 'A re-ask judged a requirement unmet after the attempt had ended, so it is yours to decide.',
  contract_superseded: 'The contract was amended after this attempt, so this judgment is of a revision no longer current.',
}

export interface AcceptanceVerdict {
  requirementId: string
  verdict: string
  reason: string | null
  item: string | null
  p: number | null
}

export interface Acceptance {
  status: string
  waitingOn: string | null
  ownerReason: string | null
  outcome: string | null
  reason: string | null
  callId: string | null
  commit: string | null
  revision: number | null
  verdicts: AcceptanceVerdict[]
  changed: string[]
  mechanical: { kind: string; passed: boolean | null }[]
  reask: { tries: number; nextAt: string | null; last: { outcome: string | null; reason: string | null } | null } | null
}

/** The stored `contract_acceptance` record, or null when the value is not one. */
export function contractAcceptance(raw: unknown): Acceptance | null {
  if (!isRecord(raw) || str(raw.status) === null || str(raw.commit) === null) return null
  const verdicts: AcceptanceVerdict[] = []
  for (const v of Array.isArray(raw.verdicts) ? raw.verdicts : []) {
    if (!isRecord(v) || str(v.requirement_id) === null) continue
    verdicts.push({ requirementId: str(v.requirement_id)!, verdict: str(v.verdict) ?? 'cannot_judge', reason: str(v.reason), item: str(v.evidence_item), p: num(v.p) })
  }
  const changed = (Array.isArray(raw.changed) ? raw.changed : []).map((c) => (typeof c === 'string' ? c : ''))
  const mechanical = (Array.isArray(raw.mechanical) ? raw.mechanical : []).map((m) =>
    isRecord(m) ? { kind: str(m.kind) ?? '', passed: typeof m.passed === 'boolean' ? m.passed : null } : { kind: '', passed: null },
  )
  const r = isRecord(raw.reask) ? raw.reask : null
  const last = r !== null && isRecord(r.last) ? { outcome: str(r.last.outcome), reason: str(r.last.reason) } : null
  return {
    status: str(raw.status)!,
    waitingOn: str(raw.waiting_on),
    ownerReason: str(raw.owner_reason),
    outcome: str(raw.outcome),
    reason: str(raw.reason),
    callId: str(raw.call_id),
    commit: str(raw.commit),
    revision: num(raw.revision),
    verdicts,
    changed,
    mechanical,
    reask: r === null ? null : { tries: num(r.tries) ?? 0, nextAt: str(r.next_at), last },
  }
}

/** `REASK_TRIES` in contractreask.py. */
const REASK_TRIES = 3

/** True once the control plane has spent its three re-asks. */
function reaskSpent(a: Acceptance): boolean {
  return a.ownerReason === 'reask_exhausted' || (a.reask !== null && a.reask.tries >= REASK_TRIES)
}

/** What a verdict rests on: a changed file at the commit, a mechanical result, or nothing. */
function Cited({ a, item }: { a: Acceptance; item: string | null }) {
  const m = item === null ? null : /^([im])(\d+)$/.exec(item)
  if (m === null) return <span className="cj-cite">cites nothing</span>
  const n = Number(m[2])
  if (m[1] === 'i') {
    const path = a.changed[n]
    if (path === undefined || path === '') return <span className="cj-cite">cites change <code>{item}</code>, which the record does not name</span>
    return (
      <span className="cj-cite">
        cites the hunk of <code>{path}</code>
        {a.commit !== null && <> at <code>{short(a.commit)}</code></>}
      </span>
    )
  }
  const mech = a.mechanical[n]
  if (mech === undefined) return <span className="cj-cite">cites result <code>{item}</code>, which the record does not name</span>
  const kind = MECHANICAL_WORDS[mech.kind] ?? mech.kind
  return <span className="cj-cite">cites {kind} {mech.passed === null ? 'not run' : mech.passed ? 'passed' : 'failed'}</span>
}

const TONE: Readonly<Record<string, string>> = { met: 'is-ok', unmet: 'is-bad', cannot_judge: 'is-warn' }

/** The line at the top of the card: where the step stands, in words. */
function Status({ a }: { a: Acceptance }) {
  const notRun = a.outcome === 'not_run'
  const key = notRun ? 'not_run' : a.status
  let said: ReactNode
  if (a.waitingOn === 'reask') {
    const tries = a.reask?.tries ?? 0
    said = (
      <>
        {notRun ? 'Jev did not judge this step yet' : 'Jev could not judge every requirement yet'}: the control plane has re-asked {tries} of {REASK_TRIES} times
        {a.reask?.nextAt ? <> and asks again at {new Date(a.reask.nextAt).toLocaleTimeString()}</> : null}, over 30 minutes, before it hands the step to its owner.
      </>
    )
  } else if (a.waitingOn === 'owner') {
    said = (
      <>
        {notRun && <>Jev did not judge this step{reaskSpent(a) ? ', after the control plane’s three re-asks' : ''}. </>}
        {a.ownerReason !== null ? OWNER_REASON_WORDS[a.ownerReason] ?? <code>{a.ownerReason}</code> : 'It is yours to decide.'}
      </>
    )
  } else if (a.status === 'met') {
    said = 'Jev judged every requirement it was asked about met.'
  } else if (a.status === 'sent_back') {
    said = 'Jev judged a requirement unmet, and the step was sent back to its agent.'
  } else {
    said = notRun ? 'Jev did not judge this step.' : <code>{a.status}</code>
  }
  return (
    <p className="cj-lead" data-testid="cj-status" data-status={key}>
      {said}
    </p>
  )
}

/** The approval screen's card body: the status, one row per verdict, and what Jev is not. */
export function AcceptanceVerdicts({ acceptance: a }: { acceptance: Acceptance }) {
  const spent = reaskSpent(a)
  return (
    <div className="cj-judgment" data-testid="cj-judgment">
      <Status a={a} />
      {a.outcome === 'not_run' && (
        <p className="cj-note">
          Why it did not run: {a.reason === null ? 'not recorded' : <code>{a.reason}</code>}
          {a.reask?.last && a.reask.last.outcome === 'not_run' && (
            <>
              ; the last re-ask: {a.reask.last.reason === null ? 'not recorded' : <code>{a.reask.last.reason}</code>}
            </>
          )}
          . Missing verification is not a pass: what Jev was to judge is unmet until a person decides.
        </p>
      )}
      {a.verdicts.length > 0 && (
        <ul className="cj-list" aria-label="Jev’s verdict, per requirement">
          {a.verdicts.map((v) => (
            <li key={v.requirementId} className="cj-row" data-requirement={v.requirementId} data-verdict={v.verdict}>
              <span className="cj-id mono">{v.requirementId}</span>
              <span className="cj-say">
                <span className="cj-by">Jev’s judgment</span>{' '}
                <span className={`cj-chip ${TONE[v.verdict] ?? ''}`}>{VERDICT_WORDS[v.verdict] ?? v.verdict}</span>
                {v.verdict === 'cannot_judge' && spent && <span className="cj-p"> after the control plane’s three re-asks</span>}
                {v.reason !== null && (
                  <>
                    {': '}
                    <Words code={v.reason} words={JUDGMENT_REASON_WORDS} />
                  </>
                )}
                {percent(v.p) !== null && v.verdict !== 'cannot_judge' && <span className="cj-p"> · confidence {percent(v.p)}</span>}
                <Cited a={a} item={v.item} />
              </span>
            </li>
          ))}
        </ul>
      )}
      <p className="cj-note">
        Jev’s verdicts are evidence for your decision; they approve nothing.
        {a.commit !== null && <> Judged at commit <code>{short(a.commit)}</code></>}
        {a.revision !== null && <> against revision {a.revision} of the contract</>}
        {a.callId !== null && <>, call <code>{a.callId}</code></>}
        {a.commit !== null || a.revision !== null || a.callId !== null ? '.' : null}
      </p>
    </div>
  )
}
