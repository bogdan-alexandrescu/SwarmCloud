import { useEffect } from 'react'
import { MarkGlyph } from './marks'
import { PageHead } from './Shell'
import './styles/submit.css'

/**
 * THE SUBMIT CHOOSER (/submit; submit.html M2, the owner's pick 2026-10-01).
 *
 * Three large choices, opened by the spine's Submit button and by N: a card
 * per form, each with its icon, a Start button and its key (T, W, I). The
 * third is "From a GitHub issue" (intake-tenants.html 1A, /submit/issue).
 *
 * RECENT SUBMISSIONS ARE NOT LISTED, and the card says why in place (states.html
 * C, a region state). No route serves "what did I submit, with what settings":
 * `GET /v1/tasks` carries `submitted_by` but not the input a submission was
 * made with, so a list built from it could neither re-open a form nor tell a
 * task from a workflow step. The card stays empty rather than guessing.
 *
 * T, W AND I FOLLOW N's GUARDS (SkyShell in Spine.tsx): never while focus is in a
 * field, never with a modifier held, never for a key another handler already
 * consumed, and never while something sits over the page.
 */

/** The task form's and the workflow form's addresses, as `go` takes them. */
export const TASK_FORM = 'work/new'
export const WORKFLOW_FORM = 'work/new-workflow'
/** The issue form (intake-tenants.html 1A): /submit/issue, key I. */
export const ISSUE_FORM = 'work/new-issue'

const KEYS: Readonly<Record<string, string>> = { t: TASK_FORM, w: WORKFLOW_FORM, i: ISSUE_FORM }

/** What sits over the page and owns the keyboard: the same list as N's. */
const OVER_THE_PAGE = '[role="dialog"], [aria-modal="true"], aside.adm-side'

/** The task card's icon: one agent (submit.html `i-agents`). */
function TaskGlyph() {
  return (
    <svg className="sb-glyph" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <rect x="5" y="7.5" width="14" height="11" rx="3" />
      <path d="M12 4v3.5M9 12.5v1M15 12.5v1M2.5 12v3M21.5 12v3" />
    </svg>
  )
}

/** The workflow card's icon: steps joined (submit.html `i-workflows`). */
function WorkflowGlyph() {
  return (
    <svg className="sb-glyph" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <circle cx="6" cy="6" r="2.2" />
      <circle cx="6" cy="18" r="2.2" />
      <circle cx="18" cy="12" r="2.2" />
      <path d="M8.2 6h3a3 3 0 0 1 3 3v.8M8.2 18h3a3 3 0 0 0 3-3v-.8" />
    </svg>
  )
}

/** The issue card's icon: an issue's ring and dot (intake-tenants.html 1A). */
function IssueGlyph() {
  return (
    <svg className="sb-glyph" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <circle cx="12" cy="12" r="8.5" />
      <circle cx="12" cy="12" r="1.8" />
    </svg>
  )
}

export function SubmitChooser({ go }: { go: (to: string) => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const to = KEYS[e.key.toLowerCase()]
      if (to === undefined) return
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return
      const el = e.target instanceof Element ? e.target : null
      if (el !== null && el.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"])') !== null) return
      if (el instanceof HTMLElement && el.isContentEditable) return
      if (document.querySelector(OVER_THE_PAGE) !== null) return
      e.preventDefault()
      go(to)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [go])

  return (
    <section className="sb-chooser" aria-labelledby="submit-h">
      {/* The page head every page draws (Q2): the title, its `?` and the
          head's "reads nothing" on one row. */}
      <PageHead title="Submit" headingId="submit-h">
        {null}
      </PageHead>
      <div className="sb-choices">
        <div className="sb-card sb-choice">
          <h2><TaskGlyph />Submit a task</h2>
          <p className="sb-note">
            One agent, one runner. It lands READY or PARKED and costs nothing until the scheduler admits it.
          </p>
          <span className="sb-row">
            <button type="button" className="sb-btn is-pri" aria-keyshortcuts="T" onClick={() => go(TASK_FORM)}>
              Start a task
            </button>
            <kbd className="sb-kbd">T</kbd>
          </span>
        </div>
        <div className="sb-card sb-choice">
          <h2><WorkflowGlyph />Submit a workflow</h2>
          <p className="sb-note">Steps in stages that hand work to each other, opening one PR or one per step.</p>
          <span className="sb-row">
            <button type="button" className="sb-btn" aria-keyshortcuts="W" onClick={() => go(WORKFLOW_FORM)}>
              Start a workflow
            </button>
            <kbd className="sb-kbd">W</kbd>
          </span>
        </div>
        <div className="sb-card sb-choice">
          <h2><IssueGlyph />From a GitHub issue</h2>
          <p className="sb-note">
            Name an issue. A planner reads it and writes a plan; once the plan is approved it runs as a workflow.
          </p>
          <span className="sb-row">
            <button type="button" className="sb-btn" aria-keyshortcuts="I" onClick={() => go(ISSUE_FORM)}>
              Start from an issue
            </button>
            <kbd className="sb-kbd">I</kbd>
          </span>
        </div>
      </div>
      <section className="sb-card sb-recent" aria-labelledby="submit-recent-h">
        <h2 className="sb-card-h" id="submit-recent-h">Start from a recent one</h2>
        <p className="sb-empty">
          <span className="sk-st is-neu" data-mark="queued" data-hue="neu">
            <svg viewBox="0 0 12 12" aria-hidden="true" focusable="false">
              <MarkGlyph mark="queued" />
            </svg>
          </span>
          <span>
            <b>No recent submissions to show yet.</b> This list needs the API to keep a short per-person list of
            what you submitted: task or workflow, name, settings, when. It does not today, so the card stays empty
            rather than guessing from the agent list.
          </span>
        </p>
      </section>
      <p className="sb-note sb-foot">
        The spine&rsquo;s Submit button opens this page; <kbd className="sb-kbd">N</kbd> from anywhere does too.
      </p>
    </section>
  )
}
