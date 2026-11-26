/**
 * THE PLAN EDITOR (docs/design/plans-pipeline.md §2.3 `D:P3b`, §11 lane U5
 * PLAN-EDITOR-MOVE), moved out of Runs.tsx so the decision page
 * (PlanDocument.tsx) and the run page (Runs.tsx) open the same one.
 *
 * WHAT IT EDITS: the summary, the estimate and the requirements, and each
 * step's title, prompt, rationale (§3.6), files, tests and estimate. It removes
 * a step -- never the last one, because `PlanSpec.steps` keeps at least one --
 * and removes a diagram (§6.2).
 *
 * EVERY FIELD IT DOES NOT SHOW GOES BACK AS IT CAME. The saved plan is the
 * opened plan spread, with only what was edited replaced: the mode, the
 * overlaps, the plan's rationale, `no_diagram`, each step's `done_when` and
 * `requirements`, and any key a later server adds. An optional field left
 * untouched keeps its exact value (absent, null or a list), so a save changes
 * nothing the reader did not change.
 *
 * SAVE CARRIES THE DIGEST IT REPLACES. The editor does not send; its host
 * records the digest the editor opened on and sends that one (`plan:edit`'s
 * `plan_digest`), so an edit made by someone else since is refused rather
 * than overwritten.
 */
import { useEffect, useId, useRef, useState, useSyncExternalStore } from 'react'
import { editPlan } from './api'
import { Button } from './components'
import { errorHeading, type ApiError } from './fetch'
import { InfoTip } from './InfoTip'
import { useRoleGate } from './roleGate'
import type { PlanStepDoc, RunPlan } from './types'
import './styles/intake.css'
import './styles/plan-document.css'

type Json = Record<string, unknown>

/** A diagram as the plan carries it (§6.2); the editor reads its id and title only. */
export interface EditorDiagram {
  diagram_id: string
  title?: string | null
  kind?: string | null
  [key: string]: unknown
}

/** A step with the fields the frozen-in-time `PlanStepDoc` does not name yet. */
type EditorStep = PlanStepDoc & { rationale?: string | null }

/** A plan with the fields served since `RunPlan` was written: the rationale and the diagrams. */
type EditorPlan = RunPlan & { rationale?: string | null; diagrams?: EditorDiagram[] | null; no_diagram?: string | null }

type StepDraft = { step: EditorStep; title: string; prompt: string; rationale: string; files: string; tests: string; estimate: string }

/** What a person edits a plan in from on a run held with no plan: one empty step to fill. */
export const EMPTY_PLAN: RunPlan = { summary: '', steps: [{ step_id: 'step-1', title: '', prompt: '' }] }

/** Why the last step cannot be removed. */
export const LAST_STEP_WHY = 'A plan keeps at least one step.'

/** A list as the editor shows it: one entry per line. */
function linesOf(list: string[] | null | undefined): string {
  return (list ?? []).join('\n')
}

/** The entries a textarea holds: one per non-blank line, trimmed. */
function entries(text: string): string[] {
  return text.split('\n').map((l) => l.trim()).filter((l) => l !== '')
}

/**
 * An optional plan field as edited. Untouched, it is the value the plan had --
 * absent, null or a list -- so a save changes nothing the reader did not
 * change; emptied, it is dropped (the API takes no empty estimate); otherwise
 * it is what was typed.
 */
function editedList(original: string[] | null | undefined, text: string): string[] | null | undefined {
  if (text === linesOf(original)) return original
  const l = entries(text)
  return l.length === 0 ? undefined : l
}

function editedText(original: string | null | undefined, text: string): string | null | undefined {
  if (text === (original ?? '')) return original
  return text.trim() === '' ? undefined : text.trim()
}

/** `target[key] = value`, or no key at all when the value is undefined. */
function put<T extends object, K extends keyof T>(target: T, key: K, value: T[K] | undefined): void {
  if (value === undefined) delete target[key]
  else target[key] = value
}

/** What a disabled Save names: the blank fields that block it. */
const notSaved = (blanks: string[]) => `Not saved: ${blanks.join(', ')} ${blanks.length === 1 ? 'is' : 'are'} empty.`

const draftOf = (s: EditorStep): StepDraft => ({
  step: s,
  title: s.title,
  prompt: s.prompt,
  rationale: s.rationale ?? '',
  files: linesOf(s.files),
  tests: linesOf(s.tests),
  estimate: s.estimate ?? '',
})

const diagramsOf = (plan: EditorPlan): EditorDiagram[] | null =>
  Array.isArray(plan.diagrams) ? (plan.diagrams as unknown[]).filter((d): d is EditorDiagram => typeof d === 'object' && d !== null && typeof (d as Json).diagram_id === 'string') : null

export function PlanEditor({ plan: opened, busy, onCancel, onSave }: Readonly<{
  plan: RunPlan; busy: boolean; onCancel: () => void; onSave: (plan: RunPlan) => void
}>) {
  const plan = opened as EditorPlan
  const [summary, setSummary] = useState(plan.summary)
  const [estimate, setEstimate] = useState(plan.estimate ?? '')
  const [requirements, setRequirements] = useState(linesOf(plan.requirements))
  const [steps, setSteps] = useState<StepDraft[]>((plan.steps as EditorStep[]).map(draftOf))
  const servedDiagrams = diagramsOf(plan)
  const [diagrams, setDiagrams] = useState<EditorDiagram[] | null>(servedDiagrams)
  // WHAT BLOCKS SAVING IS NAMED (QA1-FIX-24, runs-submit-09, part of archived issue 2596):
  // each blank required field is `aria-invalid` with its own hint, and the
  // disabled Save says which fields block it (`disabledReason`).
  const blanks = [
    ...(summary.trim() === '' ? ['the summary'] : []),
    ...steps.flatMap((s, i) => [
      ...(s.title.trim() === '' ? [`step ${i + 1}'s title`] : []),
      ...(s.prompt.trim() === '' ? [`step ${i + 1}'s prompt`] : []),
    ]),
  ]
  const blank = blanks.length > 0
  // The editor opens on its first field, not on <body> (runs-submit-08).
  const first = useRef<HTMLTextAreaElement>(null)
  useEffect(() => first.current?.focus(), [])
  const hintId = useId()
  const set = (i: number, key: 'title' | 'prompt' | 'rationale' | 'files' | 'tests' | 'estimate', value: string) =>
    setSteps((all) => all.map((s, j) => (j === i ? { ...s, [key]: value } : s)))
  const removeStep = (i: number) => setSteps((all) => (all.length <= 1 ? all : all.filter((_, j) => j !== i)))

  function edited(): RunPlan {
    const kept = new Set(steps.map((d) => d.step.step_id))
    const out: EditorPlan = {
      ...plan,
      summary,
      steps: steps.map((d) => {
        const step: EditorStep = { ...d.step, title: d.title, prompt: d.prompt }
        put(step, 'rationale', editedText(d.step.rationale, d.rationale))
        put(step, 'files', editedList(d.step.files, d.files))
        put(step, 'tests', editedList(d.step.tests, d.tests))
        put(step, 'estimate', editedText(d.step.estimate, d.estimate))
        // A removed step is no longer something to wait for.
        if (Array.isArray(d.step.depends_on) && d.step.depends_on.some((id) => !kept.has(id))) {
          step.depends_on = d.step.depends_on.filter((id) => kept.has(id))
        }
        return step
      }),
    }
    put(out, 'estimate', editedText(plan.estimate, estimate))
    put(out, 'requirements', editedList(plan.requirements, requirements))
    // The diagrams as served unless one was removed; none left drops the key.
    if (servedDiagrams !== null && diagrams !== null && diagrams.length !== servedDiagrams.length) {
      put(out, 'diagrams', diagrams.length === 0 ? undefined : diagrams)
    }
    return out
  }

  return (
    <form className="rn-edit" aria-label="Edit the plan" onSubmit={(e) => {
      e.preventDefault()
      if (!blank) onSave(edited())
    }}>
      <label className="rn-field">
        <b>Summary</b>
        <textarea ref={first} value={summary} rows={3} onChange={(e) => setSummary(e.target.value)}
          aria-invalid={summary.trim() === '' || undefined}
          aria-describedby={summary.trim() === '' ? `${hintId}-summary` : undefined} />
      </label>
      {summary.trim() === '' && <span className="sbf-bad rn-field-why" id={`${hintId}-summary`}>Required: the plan needs a summary.</span>}
      <label className="rn-field">
        <b>Estimate</b>
        <input value={estimate} maxLength={200} onChange={(e) => setEstimate(e.target.value)}
          title="Free text, as the planner wrote it: a time or a size, such as “about 2 hours”. Optional."
          aria-describedby={`${hintId}-estimate`} />
      </label>
      <span id={`${hintId}-estimate`} hidden>Free text, as the planner wrote it: a time or a size, such as “about 2 hours”. Optional.</span>
      <label className="rn-field">
        <b>Requirements, one per line</b>
        <textarea value={requirements} rows={4} onChange={(e) => setRequirements(e.target.value)} />
      </label>
      {steps.map((s, i) => {
        const last = steps.length <= 1
        return (
          <fieldset key={s.step.step_id} className="rn-field-set">
            <legend className="mono">{i + 1} · {s.step.step_id}</legend>
            <label className="rn-field">
              <b>Title</b>
              <input value={s.title} onChange={(e) => set(i, 'title', e.target.value)}
                aria-invalid={s.title.trim() === '' || undefined}
                aria-describedby={s.title.trim() === '' ? `${hintId}-t${i}` : undefined} />
            </label>
            {s.title.trim() === '' && <span className="sbf-bad rn-field-why" id={`${hintId}-t${i}`}>Required: a step needs a title.</span>}
            <label className="rn-field">
              <b>Prompt</b>
              <textarea value={s.prompt} rows={4} onChange={(e) => set(i, 'prompt', e.target.value)}
                aria-invalid={s.prompt.trim() === '' || undefined}
                aria-describedby={s.prompt.trim() === '' ? `${hintId}-p${i}` : undefined} />
            </label>
            {s.prompt.trim() === '' && <span className="sbf-bad rn-field-why" id={`${hintId}-p${i}`}>Required: a step needs a prompt.</span>}
            <label className="rn-field">
              <b>Rationale</b>
              <textarea value={s.rationale} rows={2} onChange={(e) => set(i, 'rationale', e.target.value)} />
            </label>
            <label className="rn-field">
              <b>Files, one per line</b>
              <textarea className="mono" value={s.files} rows={2} onChange={(e) => set(i, 'files', e.target.value)} />
            </label>
            <label className="rn-field">
              <b>Tests, one per line</b>
              <textarea value={s.tests} rows={2} onChange={(e) => set(i, 'tests', e.target.value)} />
            </label>
            <label className="rn-field">
              <b>Step estimate</b>
              <input value={s.estimate} maxLength={100} onChange={(e) => set(i, 'estimate', e.target.value)} />
            </label>
            <span className="rn-actions" title={last ? LAST_STEP_WHY : undefined}>
              <Button size="sm" kind="ghost" disabled={busy || last} aria-label={`Remove step ${i + 1}`}
                aria-describedby={last ? `${hintId}-last` : undefined} onClick={() => removeStep(i)}>Remove step</Button>
              {last && <span id={`${hintId}-last`} hidden>{LAST_STEP_WHY}</span>}
            </span>
          </fieldset>
        )
      })}
      {diagrams !== null && diagrams.length > 0 && (
        <fieldset className="rn-field-set">
          <legend>Diagrams</legend>
          <ul className="rn-edit" style={{ listStyle: 'none', margin: 0, padding: 0 }} aria-label="Diagrams">
            {diagrams.map((d) => {
              const name = d.title?.trim() || d.diagram_id
              return (
                <li key={d.diagram_id} className="rn-actions">
                  <span title={d.diagram_id}>{name}</span>
                  {typeof d.kind === 'string' && <span className="sb-note">{d.kind}</span>}
                  <Button size="sm" kind="ghost" disabled={busy} aria-label={`Remove diagram ${name}`}
                    onClick={() => setDiagrams((all) => (all ?? []).filter((x) => x.diagram_id !== d.diagram_id))}>Remove</Button>
                </li>
              )
            })}
          </ul>
        </fieldset>
      )}
      <div className="rn-actions">
        <Button type="submit" kind="primary" disabled={busy}
          disabledReason={blank ? notSaved(blanks) : undefined}>Save the plan</Button>
        <Button disabled={busy} onClick={onCancel}>Cancel</Button>
        <InfoTip about="saving the plan" tip="Saving makes a new revision with its own digest and sends the digest of the plan you opened: an edit made by someone else since is refused. The requirements are what the review checks, and what decides Closes against part of. The mode and the overlaps are kept as the planner found them." end />
      </div>
    </form>
  )
}

// ---------------------------------------------------------------------------
// On the decision page (D:P3b, D:P3c)
// ---------------------------------------------------------------------------

/** The revision a save on this page made over the digest it replaced. */
export interface SavedRevision {
  revision: number | null
  digest: string
  by: string | null
}

const savedOver = new Map<string, SavedRevision>()
const savedListeners = new Set<() => void>()
const subscribeSaved = (f: () => void) => {
  savedListeners.add(f)
  return () => {
    savedListeners.delete(f)
  }
}

/**
 * The revision saved over `digest` on this page, else null. The decision bar
 * reads it so Approve leaves the replaced revision the moment the save
 * answers (`D:P3c`: Approve is offered for the new revision alone), rather
 * than at the next poll.
 */
export function useSavedOver(digest: string | null): SavedRevision | null {
  return useSyncExternalStore(subscribeSaved, () => (digest === null ? null : (savedOver.get(digest) ?? null)))
}

/**
 * THE DECISION BAR'S EDIT OPENS THE EDITOR IN PLACE (owner's pick on PR 464,
 * "Edit opens the editor in place"): the bar (ApprovalDecision.tsx's Decide)
 * and the document's editor (PlanEditorMount) are drawn apart, so the mount
 * offers an opener for the digest it would edit, and the bar's Edit calls it.
 * No mount, or a reader who may not edit, offers none, and the bar draws no
 * Edit: the bar never offers a click the editor would not take.
 */
const openers = new Map<string, () => void>()
const openerListeners = new Set<() => void>()
const subscribeOpeners = (f: () => void) => {
  openerListeners.add(f)
  return () => {
    openerListeners.delete(f)
  }
}
const openersChanged = () => openerListeners.forEach((f) => f())

/** The function that opens this page's editor over `digest`, else null. */
export function usePlanEditorOpener(digest: string | null): (() => void) | null {
  return useSyncExternalStore(subscribeOpeners, () => (digest === null ? null : (openers.get(digest) ?? null)))
}

const obj = (v: unknown): Json | null => (v !== null && typeof v === 'object' && !Array.isArray(v) ? (v as Json) : null)

/**
 * Whether the reader may edit by hand: the run read's `may_decide.edit` when it
 * serves one (§3.9), else today's rule, a member or above (an older server,
 * `D:P6d`). The server is the gate; this only stops offering a refused click.
 */
function useMayEdit(run: Json | null): boolean {
  const gate = useRoleGate('member', 'edit')
  if (run === null || run.state !== 'PLANNED') return false
  const md = obj(run.may_decide)
  if (md !== null) return md.edit === true
  return gate === null
}

const short = (d: string) => {
  const hex = d.replace(/^sha256:/, '')
  return hex.length > 12 ? hex.slice(0, 12) : hex
}

/**
 * EDIT BY HAND ON THE DECISION PAGE (`D:P3b`): the editor over the revision
 * shown, opened in place by the decision bar's Edit. Save sends `plan:edit` with the digest the editor
 * opened on -- the revision it replaces -- and, once saved, names the new
 * revision, its digest and its author (`D:P3c`).
 */
export function PlanEditorMount({ runId, run, plan, digest }: Readonly<{ runId: string | null; run: Json | null; plan: Json | null; digest: string | null }>) {
  const mayEdit = useMayEdit(run)
  const [open, setOpen] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const saved = useSavedOver(digest)
  const where = useRef<HTMLDivElement>(null)
  const offered = runId !== null && plan !== null && digest !== null && saved === null && mayEdit
  useEffect(() => {
    if (!offered || typeof digest !== 'string') return
    const openHere = () => {
      setRefused(null)
      setOpen(digest)
      // The editor opens where the plan is: brought into view, its first field focused.
      requestAnimationFrame(() => {
        where.current?.scrollIntoView?.({ block: 'start' })
        where.current?.querySelector<HTMLElement>('input, textarea')?.focus()
      })
    }
    openers.set(digest, openHere)
    openersChanged()
    return () => {
      if (Object.is(openers.get(digest), openHere)) openers.delete(digest)
      openersChanged()
    }
  }, [offered, digest])
  if (runId === null || plan === null || digest === null) return null
  if (saved !== null)
    return (
      <p className="rn-actions" role="status" data-testid="pe-saved" title="Saved as a new revision: Approve is offered for it alone.">
        <b>{saved.revision === null ? 'New revision' : `Revision ${saved.revision}`}</b>
        <code className="pd-digest" title={saved.digest} data-digest={saved.digest}>{short(saved.digest)}</code>
        {saved.by !== null && <span className="pd-dim">{saved.by}</span>}
      </p>
    )
  if (!mayEdit) return null
  // Closed, it draws nothing: the decision bar's Edit opens it (usePlanEditorOpener).
  if (open === null) return null
  const save = async (edited: RunPlan) => {
    setBusy(true)
    setRefused(null)
    const replaced = open
    const r = await editPlan(runId, replaced, edited)
    setBusy(false)
    if (r.status === 'ok' || r.status === 'stale') {
      const next = r.data.run
      if (typeof next.plan_digest === 'string' && next.plan_digest !== replaced) {
        savedOver.set(replaced, { revision: typeof next.plan_revision === 'number' ? next.plan_revision : null, digest: next.plan_digest, by: next.plan_edited_by ?? null })
        savedListeners.forEach((f) => f())
      }
      setOpen(null)
      return
    }
    if (r.status === 'error') setRefused(r.error)
  }
  return (
    <div ref={where} data-testid="pe-in-place">
      {refused !== null && (
        <p className="sbf-bad" role="alert" data-testid="pe-refused" title={refused.message}>
          Not saved: {errorHeading(refused)}
        </p>
      )}
      <PlanEditor key={open} plan={plan as unknown as RunPlan} busy={busy} onCancel={() => setOpen(null)} onSave={(p) => void save(p)} />
    </div>
  )
}
