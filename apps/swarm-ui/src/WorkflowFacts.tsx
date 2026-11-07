// THE WORKFLOW'S FACTS CARD (the picked workflow-clarity variant, the owner,
// 2026-11-05): under the page's state row, what the workflow is meant to
// deliver on the left and a column of labelled facts on the right
// (Repository, Feature, Started by, From, When), stacked under the
// description on a phone; and the list's narrow Repository cell.
//
// WHAT IT READS IS WHAT SWARM-API SERVES (`routes/workflows.py`
// `workflow_about`), on every workflow read, the full and the summary view
// alike, at the workflow's root: `repository_url`, `title`, `description`,
// `description_source`, `about` (`{purpose, goals, output}`, the description
// in three labelled parts, owner 2026-11-06), `feature` (`{title, url?}`) and
// `started_from`. The
// frozen `Workflow` (types.ts) does not name them, so they are read here, each
// checked for its type. The keys are present only when a step task was read:
// ABSENT means "not read" (`not recorded`), null means "none" (`none`).
import { useId, useLayoutEffect, useRef, useState } from 'react'
import { PersonName } from './PersonName'
import { workflowLabel } from './stepviews'
import { timeAgo, type Task, type Workflow } from './types'
import './styles/workflow-facts.css'

/** `STARTED_FROM` in swarm-api's `routes/workflows.py`, in its words. */
export const STARTED_FROM_WORDS: Readonly<Record<string, string>> = {
  schedule: 'Schedule',
  quality_check: 'Quality check',
  issue_run: 'Issue run',
  claude_code: 'Claude Code',
  console: 'Console',
}

/** The description's three labelled parts, each optional (`schemas.check_about`). */
export interface AboutParts {
  readonly purpose: string | null
  readonly goals: readonly string[]
  readonly output: string | null
}

/** What `workflow_about` serves, as read: `undefined` when the key is absent (not read), `null` when it is none. */
export interface WorkflowAbout {
  readonly repositoryUrl: string | null | undefined
  readonly title: string | null | undefined
  readonly description: string | null | undefined
  /** `about`, else a bare `description` as its purpose (a server before `about`), else null / undefined. */
  readonly parts: AboutParts | null | undefined
  readonly generated: boolean
  readonly feature: { readonly title: string; readonly url: string | null } | null | undefined
  readonly startedFrom: string | null | undefined
}

function textOrNone(raw: Record<string, unknown>, key: string): string | null | undefined {
  if (!(key in raw)) return undefined
  const v = raw[key]
  return typeof v === 'string' && v.trim() !== '' ? v.trim() : null
}

/** `about` as served, each part checked for its type; null when no part holds text. */
function partsOf(raw: Record<string, unknown>, description: string | null | undefined): AboutParts | null | undefined {
  const a = raw.about
  if (a !== null && typeof a === 'object') {
    const r = a as Record<string, unknown>
    const goals = Array.isArray(r.goals) ? r.goals.filter((g): g is string => typeof g === 'string' && g.trim() !== '').map((g) => g.trim()).slice(0, 3) : []
    const parts = { purpose: textOrNone(r, 'purpose') ?? null, goals, output: textOrNone(r, 'output') ?? null }
    if (parts.purpose !== null || goals.length > 0 || parts.output !== null) return parts
  }
  if (typeof description === 'string') return { purpose: description, goals: [], output: null }
  return 'about' in raw || description === null ? null : undefined
}

/** The served about-fields of one workflow row, each checked for its type. */
export function aboutOf(workflow: Workflow): WorkflowAbout {
  const raw = workflow as unknown as Record<string, unknown>
  const description = textOrNone(raw, 'description')
  const parts = partsOf(raw, description)
  let feature: WorkflowAbout['feature'] = 'feature' in raw ? null : undefined
  const f = raw.feature
  if (f !== null && typeof f === 'object') {
    const title = textOrNone(f as Record<string, unknown>, 'title')
    if (typeof title === 'string') feature = { title, url: textOrNone(f as Record<string, unknown>, 'url') ?? null }
  }
  return {
    repositoryUrl: textOrNone(raw, 'repository_url'),
    title: textOrNone(raw, 'title'),
    description,
    parts,
    // A generated description is marked only beside one (the route serves the source only then).
    generated: parts != null && raw.description_source === 'generated',
    feature,
    startedFrom: textOrNone(raw, 'started_from'),
  }
}

/**
 * THE NAME EVERY SURFACE GIVES A WORKFLOW: its served `title`, else its label
 * off the step tasks (`workflowLabel`: `unit`, then `title`), else null. The
 * page heading, the breadcrumb, the list rows and the sidebar's Recent all
 * read it, so one workflow has one name everywhere.
 */
export function workflowName(workflow: Workflow, taskById: ReadonlyMap<string, Task> | null): string | null {
  return aboutOf(workflow).title ?? workflowLabel(workflow, taskById)
}

/**
 * `owner/repo` off a repository URL (`https://host/owner/repo(.git)`, or the
 * scp form `git@host:owner/repo.git`); null when it names no two segments.
 */
export function repoOwnerName(url: string | null | undefined): { owner: string; name: string } | null {
  if (typeof url !== 'string') return null
  const path = url
    .trim()
    .replace(/^[a-z+]+:\/\/[^/]+/i, '')
    .replace(/^[^/@\s]+@[^:/\s]+:/, '')
  const parts = path.split('/').filter((p) => p !== '')
  if (parts.length < 2) return null
  const owner = parts[parts.length - 2]
  const name = (parts[parts.length - 1] ?? '').replace(/\.git$/i, '')
  return owner && name ? { owner, name } : null
}

/** An http(s) URL, else null: a served string becomes an href only when it is one. */
function safeHref(url: string | null | undefined): string | null {
  return typeof url === 'string' && /^https?:\/\//i.test(url) ? url : null
}

/** An (i) whose explanation is a tooltip: the card's one note, never a line of prose. */
function Info({ about, tip }: { about: string; tip: string }) {
  const id = useId()
  const [open, setOpen] = useState(false)
  return (
    <span className="c-tip-host wff-info" onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
      <button
        type="button"
        className="wff-info-btn"
        aria-label={`About ${about}`}
        aria-describedby={id}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onClick={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === 'Escape') setOpen(false)
        }}
      >
        <svg className="c-ic" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <circle cx="12" cy="12" r="8.5" />
          <path d="M12 11v5.5M12 7.75v.01" />
        </svg>
      </button>
      <span role="tooltip" id={id} className="c-tip" hidden={!open}>
        {tip}
      </span>
    </span>
  )
}

/** Why a description is marked generated, behind its (i). */
export const GENERATED_TIP = "No description was submitted: this one was written from the steps' briefs."

/** A value that was not read: its slot and label stay, and the word says which kind of nothing. */
function NotRecorded() {
  return <span className="ctl-mark is-absent">not recorded</span>
}

/** Asked, and there is none: a measured nothing, not a missing read. */
function None({ children }: { children: string }) {
  return <span className="ctl-mark is-zero">{children}</span>
}

/** `owner/repo`, breaking only after the slash: a repository name never splits mid-word. */
function RepoWords({ owner, name }: { owner: string; name: string }) {
  return (
    <>
      <span className="wff-nowrap">{owner}/</span>
      <wbr />
      <span className="wff-nowrap">{name}</span>
    </>
  )
}

/**
 * The reference beside From's chip, off the step tasks' own marks (masked
 * metadata, as everything on a task): an issue run's issue (`issue_run.issue`,
 * else its run id) and a schedule's id (`schedule.schedule_id`). Null for the
 * other kinds, and when no joined step carries the mark.
 */
export function startedFromRef(
  workflow: Workflow,
  kind: string | null | undefined,
  taskById: ReadonlyMap<string, Task> | null,
): string | null {
  if (taskById === null || (kind !== 'issue_run' && kind !== 'schedule')) return null
  for (const s of workflow.steps) {
    const meta = s.task_id ? taskById.get(s.task_id)?.metadata : undefined
    const mark = meta?.[kind]
    if (kind === 'issue_run') {
      if (typeof mark === 'string' && mark.trim() !== '') return mark.trim()
      if (mark !== null && typeof mark === 'object') {
        const m = mark as Record<string, unknown>
        for (const key of ['issue', 'run_id']) {
          const v = m[key]
          if (typeof v === 'string' && v.trim() !== '') return v.trim()
        }
      }
    } else if (mark !== null && typeof mark === 'object') {
      const id = (mark as Record<string, unknown>).schedule_id
      if (typeof id === 'string' && id.trim() !== '') return id.trim()
    }
  }
  return null
}

/**
 * One workflow's facts card: the description and the five labelled facts.
 * `started` is the submission instant as the page writes it (`utcStamp`, in
 * Workflows.tsx), null when none is recorded; it is handed in so this module
 * imports nothing of the page's and the sidebar can read `workflowName`.
 */
export function WorkflowFacts({
  workflow,
  taskById,
  started,
  now,
}: {
  workflow: Workflow
  taskById: ReadonlyMap<string, Task> | null
  started: string | null
  now: number
}) {
  const about = aboutOf(workflow)
  const parts = about.parts ?? null
  const partsKey = parts === null ? '' : JSON.stringify(parts)
  const textRef = useRef<HTMLDivElement>(null)
  const [open, setOpen] = useState(false)
  const [clipped, setClipped] = useState(false)
  // SHOW ALL IS DRAWN ONLY WHEN THE CLAMP CUT TEXT: measured, not guessed
  // from a character count, so it follows the width the card has. Each part
  // is clamped on its own, so any one of them cut draws More.
  useLayoutEffect(() => {
    const el = textRef.current
    if (el === null || open) return
    const measure = () => setClipped([...el.querySelectorAll<HTMLElement>('.wff-clip')].some((c) => c.scrollHeight > c.clientHeight + 1))
    measure()
    const ro = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure)
    ro?.observe(el)
    return () => ro?.disconnect()
  }, [partsKey, open])

  const repo = repoOwnerName(about.repositoryUrl)
  const repoHref = safeHref(about.repositoryUrl)
  const featureHref = safeHref(about.feature?.url)
  const ref = startedFromRef(workflow, about.startedFrom, taskById)
  const ago = Number.isFinite(Date.parse(workflow.created_at)) ? timeAgo(workflow.created_at, now) : null
  const textId = `wff-d-${workflow.workflow_id}`

  return (
    <section className={`wff-card${about.parts === null ? ' is-facts-only' : ''}`} aria-label="About this workflow">
      {about.parts === undefined ? (
        <div className="wff-desc">
          <p className="wff-label">Description</p>
          <p className="wff-text">
            <NotRecorded />
          </p>
        </div>
      ) : parts === null ? null : (
        // NONE SERVED DRAWS NOTHING: the page's title stands alone, nothing invented.
        <div className="wff-desc">
          <p className="wff-label">
            Description
            {about.generated && (
              <>
                <span className="c-chip wff-gen">generated</span>
                <Info about="the generated description" tip={GENERATED_TIP} />
              </>
            )}
          </p>
          <div ref={textRef} id={textId} className={`wff-parts${open ? '' : ' is-clamped'}${about.generated ? ' is-generated' : ''}`}>
            {parts.purpose !== null && (
              <p className="wff-part">
                <span className="wff-part-label">Purpose</span>
                <span className="wff-text wff-clip">{parts.purpose}</span>
              </p>
            )}
            {parts.goals.length > 0 && (
              <div className="wff-part">
                <span className="wff-part-label">Goals</span>
                <ul className="wff-goals">
                  {parts.goals.map((g, i) => (
                    <li key={i}>
                      <span className="wff-text wff-clip">{g}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {parts.output !== null && (
              <p className="wff-part">
                <span className="wff-part-label">Expected output</span>
                <span className="wff-text wff-clip">{parts.output}</span>
              </p>
            )}
          </div>
          {(clipped || open) && (
            <button type="button" className="ctl-link wff-more" aria-expanded={open} aria-controls={textId} onClick={() => setOpen(!open)}>
              {open ? 'Less' : 'More'}
            </button>
          )}
        </div>
      )}
      <dl className="wff-facts">
        <div>
          <dt>Repository</dt>
          <dd>
            {repo === null ? (
              about.repositoryUrl === null ? <None>none</None> : <NotRecorded />
            ) : repoHref === null ? (
              <span className="wff-mono">
                <RepoWords {...repo} />
              </span>
            ) : (
              <a className="ctl-link wff-mono" href={repoHref} target="_blank" rel="noreferrer" title={repoHref}>
                <RepoWords {...repo} />
              </a>
            )}
          </dd>
        </div>
        <div>
          <dt>Feature</dt>
          <dd>
            {about.feature === undefined ? (
              <NotRecorded />
            ) : about.feature === null ? (
              <None>none</None>
            ) : featureHref === null ? (
              about.feature.title
            ) : (
              <a className="ctl-link" href={featureHref} target="_blank" rel="noreferrer" title={featureHref}>
                {about.feature.title}
              </a>
            )}
          </dd>
        </div>
        <div>
          <dt>Started by</dt>
          <dd>
            {workflow.submitted_by === null || workflow.submitted_by === '' ? (
              <NotRecorded />
            ) : (
              <PersonName email={workflow.submitted_by} name={workflow.submitted_by_name} variant="full" />
            )}
          </dd>
        </div>
        <div>
          <dt>From</dt>
          <dd className="wff-from">
            {about.startedFrom == null ? (
              <NotRecorded />
            ) : (
              <>
                <span className="c-chip">{STARTED_FROM_WORDS[about.startedFrom] ?? about.startedFrom}</span>
                {ref !== null && <span className="wff-mono">{ref}</span>}
              </>
            )}
          </dd>
        </div>
        <div>
          <dt>When</dt>
          <dd>
            {started === null ? (
              <NotRecorded />
            ) : (
              // THE AGE IS THE CHIPS ROW'S ("16d 11h old"); the card gives the instant.
              <span className="wff-mono" title={ago === null ? workflow.created_at : `${workflow.created_at} · ${ago}`}>
                {started}
              </span>
            )}
          </dd>
        </div>
      </dl>
    </section>
  )
}

/** The list's Repository cell: the repository's name, its owner under it in the faint ink; the whole URL is its title. */
export function WorkflowRepoCell({ workflow }: { workflow: Workflow }) {
  const url = aboutOf(workflow).repositoryUrl
  const repo = repoOwnerName(url)
  return (
    <td className="wff-repo" data-col="repo" title={url ?? 'no repository recorded'}>
      {repo === null ? (
        <span className="wff-dash">—</span>
      ) : (
        <>
          <span className="wff-mono wff-repo-name">{repo.name}</span>
          <span className="wff-mono wff-repo-by">{repo.owner}</span>
        </>
      )}
    </td>
  )
}

/** Where the Repository column gives way (a phone, a narrow pane): the whole `owner/repo` under the name. */
export function WorkflowRepoUnder({ workflow }: { workflow: Workflow }) {
  const repo = repoOwnerName(aboutOf(workflow).repositoryUrl)
  return repo === null ? null : (
    <span className="wff-repo-under wff-mono">
      <RepoWords {...repo} />
    </span>
  )
}
