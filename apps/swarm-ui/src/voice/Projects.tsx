/**
 * VOICE IT › PROJECTS (docs/design/voice-it.md §3.7, §3.11; §10 phase four's
 * panes row): the person's saved conversations, each resumable.
 *
 * Everything here is swarm-api's projects routes (routes/voice.py), which
 * read only the caller's own projects in the selected workspace:
 *
 *   * the list, newest first: each project's title and when it last changed,
 *     and its last session and open card count when swarm-api serves them
 *     (a column nothing serves is not drawn, rather than drawn empty);
 *   * Resume opens Talk with the project handed over (`resumeProject`);
 *   * Rename, inline: the title alone, which swarm-api checks again;
 *   * Delete, typed: the route deletes only when `confirm` is the project's
 *     title (owner pick, PR #3019), so the dialog unlocks only on an exact
 *     match and sends what was typed. A delete removes the project's
 *     transcripts, mockups and plans under the workspace's prefix, which is
 *     why it is typed; a refusal (a live session is 409) is said in place.
 *
 * Nothing here starts a session or spends: Resume only hands Talk the
 * project, and Talk's own Start talking is what opens one.
 */
import { useCallback, useEffect, useState, type FormEvent } from 'react'

import { Banner, Button, Dash, EmptyState, LoadingState, Table, TypedConfirm, type Column } from '../components'
import { read, route, write } from '../fetch'
import { formatDateTime } from '../timezone'

/** The person's projects in the selected workspace, newest first. */
export const VOICE_PROJECTS = '/v1/voice/projects'
/** One project: PATCH {title} renames it, DELETE {confirm} deletes it. */
export const VOICE_PROJECT = '/v1/voice/projects/{id}'
/** Talk's page, where a resumed project is picked up. */
export const TALK_PATH = '/voice/talk'
/**
 * The tab's hand-over from Resume to Talk: the project id, taken once. A
 * session-storage entry rather than the address, so a reload of Talk, a
 * bookmark or a shared link never silently reopens an old project.
 */
export const RESUME_KEY = 'swarm.voice.resume'

export interface VoiceProject {
  project_id: string
  title: string
  updated_at: string | null
  /** When its last session started; drawn only when swarm-api serves it. */
  last_session_at?: string | null
  /** How many of its cards are open; drawn only when swarm-api serves it. */
  open_cards?: number | null
}

function asProject(data: unknown): VoiceProject | null {
  const d = data as Partial<VoiceProject> | null
  if (d === null || typeof d !== 'object') return null
  if (typeof d.project_id !== 'string' || !d.project_id.startsWith('vp_') || typeof d.title !== 'string') return null
  const p: VoiceProject = {
    project_id: d.project_id,
    title: d.title,
    updated_at: typeof d.updated_at === 'string' ? d.updated_at : null,
  }
  if ('last_session_at' in d) p.last_session_at = typeof d.last_session_at === 'string' ? d.last_session_at : null
  if ('open_cards' in d) p.open_cards = typeof d.open_cards === 'number' && Number.isFinite(d.open_cards) ? d.open_cards : null
  return p
}

function asProjects(data: unknown): VoiceProject[] | null {
  const d = data as { projects?: unknown } | null
  if (d === null || typeof d !== 'object' || !Array.isArray(d.projects)) return null
  return d.projects.map(asProject).filter((p): p is VoiceProject => p !== null)
}

/** Newest first by when each last changed; a project with no time sorts last. */
function newestFirst(rows: readonly VoiceProject[]): VoiceProject[] {
  return [...rows].sort((a, b) => (b.updated_at === null ? -Infinity : Date.parse(b.updated_at)) - (a.updated_at === null ? -Infinity : Date.parse(a.updated_at)))
}

/**
 * Resume: hand Talk the project and open Talk, the way the console's other
 * out-of-router navigations do (write the path, then `popstate`).
 */
export function resumeProject(projectId: string): void {
  try {
    window.sessionStorage.setItem(RESUME_KEY, projectId)
  } catch {
    // Storage refused (a private window's quota): Talk still opens, on a new project.
  }
  window.history.pushState(null, '', TALK_PATH)
  window.dispatchEvent(new PopStateEvent('popstate'))
}

/** Talk's side of Resume: the handed-over project id, once, or null. */
export function takeResumedProject(): string | null {
  try {
    const id = window.sessionStorage.getItem(RESUME_KEY)
    window.sessionStorage.removeItem(RESUME_KEY)
    return id !== null && id.startsWith('vp_') ? id : null
  } catch {
    return null
  }
}

function When({ at, why }: { at: string | null | undefined; why: string }) {
  return at ? <>{formatDateTime(at)}</> : <Dash why={why} />
}

/** The inline rename: the title field and its two actions, in the row. */
function RenameField({
  project,
  busy,
  onSave,
  onCancel,
}: {
  project: VoiceProject
  busy: boolean
  onSave: (title: string) => void
  onCancel: () => void
}) {
  const [title, setTitle] = useState(project.title)
  const submit = (e: FormEvent) => {
    e.preventDefault()
    const t = title.trim()
    if (t !== '') onSave(t)
  }
  return (
    <form className="ob-acts" onSubmit={submit} aria-label={`Rename ${project.title}`}>
      <input
        className="c-inp"
        aria-label={`New title for ${project.title}`}
        value={title}
        maxLength={200}
        autoComplete="off"
        // The person pressed Rename to type here.
        autoFocus
        onChange={(e) => setTitle(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Escape') onCancel()
        }}
      />
      <Button kind="primary" size="sm" type="submit" busy={busy ? 'Saving…' : false} disabled={title.trim() === ''}>
        Save title
      </Button>
      <Button kind="ghost" size="sm" onClick={onCancel} disabled={busy}>
        Cancel
      </Button>
    </form>
  )
}

/** Voice it › Projects. */
export function VoiceProjects() {
  const [projects, setProjects] = useState<VoiceProject[] | null>(null)
  const [readError, setReadError] = useState<string | null>(null)
  const [renaming, setRenaming] = useState<string | null>(null)
  const [deleting, setDeleting] = useState<VoiceProject | null>(null)
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState<string | null>(null)
  const [deleteError, setDeleteError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    void read<unknown>(route(VOICE_PROJECTS), () => false).then((r) => {
      if (!live) return
      if (r.status === 'ok' || r.status === 'stale') {
        const rows = asProjects(r.data)
        if (rows === null) setReadError("swarm-api's answer carried no list of projects.")
        else setProjects(newestFirst(rows))
      } else if (r.status === 'empty') {
        setProjects([])
      } else if (r.status === 'error') {
        setReadError(r.error.message)
      }
    })
    return () => {
      live = false
    }
  }, [])

  const rename = useCallback(async (p: VoiceProject, title: string) => {
    setBusy(true)
    setFailure(null)
    try {
      const r = await write(route(VOICE_PROJECT, { id: p.project_id }), 'PATCH', { title })
      if (r.status === 'error' || r.status === 'stale') {
        setFailure(r.error.message)
        return
      }
      const renamed = r.status === 'ok' ? asProject(r.data) : null
      setProjects((rows) =>
        rows === null ? rows : newestFirst(rows.map((x) => (x.project_id === p.project_id ? { ...x, ...(renamed ?? { title }) } : x))),
      )
      setRenaming(null)
    } finally {
      setBusy(false)
    }
  }, [])

  const remove = useCallback(async (p: VoiceProject) => {
    setBusy(true)
    setDeleteError(null)
    try {
      const r = await write(route(VOICE_PROJECT, { id: p.project_id }), 'DELETE', { confirm: p.title })
      if (r.status === 'error' || r.status === 'stale') {
        setDeleteError(r.error.message)
        return
      }
      setProjects((rows) => (rows === null ? rows : rows.filter((x) => x.project_id !== p.project_id)))
      setDeleting(null)
    } finally {
      setBusy(false)
    }
  }, [])

  if (readError !== null) {
    return (
      <Banner tone="bad" title="Could not read your Voice it projects" role="alert">
        {readError}
      </Banner>
    )
  }
  if (projects === null) return <LoadingState lines={3} label="Reading your Voice it projects" />
  if (projects.length === 0) {
    return (
      <EmptyState kind="empty" heading="No projects yet">
        Each talk is kept here as a project: start one in Talk, and it appears here to rename, resume or delete.
      </EmptyState>
    )
  }

  const servesLastSession = projects.some((p) => p.last_session_at !== undefined)
  const servesOpenCards = projects.some((p) => p.open_cards !== undefined)
  const columns: Column<VoiceProject>[] = [
    {
      key: 'title',
      head: 'Project',
      cell: (p) =>
        renaming === p.project_id ? (
          <RenameField project={p} busy={busy} onSave={(t) => void rename(p, t)} onCancel={() => setRenaming(null)} />
        ) : (
          <b>{p.title}</b>
        ),
    },
    ...(servesLastSession
      ? [{ key: 'last_session', head: 'Last session', cell: (p: VoiceProject) => <When at={p.last_session_at} why="No session recorded" /> }]
      : []),
    ...(servesOpenCards
      ? [
          {
            key: 'open_cards',
            head: 'Open cards',
            num: true,
            cell: (p: VoiceProject) => (typeof p.open_cards === 'number' ? p.open_cards : <Dash why="Not served for this project" />),
          },
        ]
      : []),
    { key: 'updated', head: 'Last changed', cell: (p) => <When at={p.updated_at} why="swarm-api served no time" /> },
    {
      key: 'actions',
      head: 'Actions',
      cell: (p) => (
        <span className="ob-acts">
          <Button kind="primary" size="sm" onClick={() => resumeProject(p.project_id)}>
            Resume
          </Button>
          <Button
            size="sm"
            disabled={renaming !== null}
            onClick={() => {
              setFailure(null)
              setRenaming(p.project_id)
            }}
          >
            Rename
          </Button>
          <Button
            kind="danger"
            size="sm"
            onClick={() => {
              setDeleteError(null)
              setDeleting(p)
            }}
          >
            Delete
          </Button>
        </span>
      ),
    },
  ]

  return (
    <>
      {failure !== null && (
        <Banner tone="bad" title="The project was not renamed" role="alert">
          {failure}
        </Banner>
      )}
      <Table caption="Your Voice it projects, newest first" columns={columns} rows={projects} rowKey={(p) => p.project_id} />
      {deleting !== null && (
        <TypedConfirm
          title={`Delete ${deleting.title}?`}
          name={deleting.title}
          verb="Delete the project"
          keep="Keep it"
          busy={busy ? 'Deleting…' : false}
          error={deleteError ?? undefined}
          onConfirm={() => void remove(deleting)}
          onClose={() => {
            if (!busy) setDeleting(null)
          }}
        >
          <p>
            This deletes the project, its cards and turns, and its transcripts, mockups and plans. What it started is not stopped:
            agents and workflows it launched keep running.
          </p>
        </TypedConfirm>
      )}
    </>
  )
}
