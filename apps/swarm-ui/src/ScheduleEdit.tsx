/**
 * CREATE AND EDIT A SCHEDULE (docs/schedules.md §6.2 variant A, one page from
 * a type template; §6.3 cron in words and the next five).
 *
 * Every field has a default from the type, so most schedules are a type, a
 * repository and a time. The cron field has presets and a raw field; under it
 * the expression in words and the next five firings in the schedule's zone
 * and the viewer's, all from `POST /v1/schedules:preview`, which answers from
 * the tick's own parser -- so this form and the tick cannot disagree.
 *
 * A TYPE BY NAME, NEVER AN IMAGE OR A COMMAND (invariant 10): the body this
 * builds carries a type name, a scope, a cron, a zone, parameters and a gate.
 * `merge: auto` is not offered: it is the SD3 switch on the gate card, which
 * has its own audited route, and the edit route refuses it (§7.1).
 */
import { useEffect, useState } from 'react'
import {
  createSchedule,
  editSchedule,
  loadRepositories,
  loadSchedule,
  loadScheduleTypes,
  previewSchedule,
  type Schedule,
  type ScheduleBody,
  type SchedulePreview,
  type ScheduleType,
} from './api'
import { Banner, Button, Card } from './components'
import type { ApiError } from './fetch'
import type { RepoRecord } from './RepositoriesData'
import { RoutedLink, SCHEDULES, scheduleAddress, whenIn } from './Schedules'
import { PageHead } from './Shell'

/** §6.3's presets. The raw field takes anything the parser does. */
export const CRON_PRESETS: readonly { label: string; cron: string }[] = [
  { label: 'Every hour', cron: '0 * * * *' },
  { label: 'Weekdays at 09:00', cron: '0 9 * * 1-5' },
  { label: 'Nightly', cron: '0 2 * * *' },
  { label: 'Weekly on Monday', cron: '0 9 * * 1' },
]

/** The viewer's own zone, for the second column of the next five. */
function viewerZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'
  } catch {
    return 'UTC'
  }
}

interface Draft {
  name: string
  type: string
  mode: 'repos' | 'all'
  repoIds: string[]
  cron: string
  timezone: string
  run: 'auto' | 'approve' | ''
  plan: 'auto' | 'approve' | ''
  merge: 'off' | 'approve' | ''
  params: string
}

function draftOf(s: Schedule | null): Draft {
  if (s === null) {
    return { name: '', type: '', mode: 'repos', repoIds: [], cron: '0 9 * * 1-5', timezone: viewerZone(), run: '', plan: '', merge: '', params: '' }
  }
  return {
    name: s.name,
    type: s.type,
    mode: s.scope.mode === 'all' ? 'all' : 'repos',
    repoIds: s.scope.repo_ids ?? [],
    cron: s.cron,
    timezone: s.timezone,
    run: s.gate.run,
    plan: s.gate.plan,
    // `auto` is the switch's to set; the form leaves it as it is.
    merge: s.gate.merge === 'auto' ? '' : s.gate.merge,
    params: JSON.stringify(s.params ?? {}, null, 2),
  }
}

/** The body: only what the person chose, so a type's default fills the rest. */
export function bodyOf(d: Draft, editing: Schedule | null): { body: ScheduleBody } | { problem: string } {
  let params: Record<string, unknown> | undefined
  if (d.params.trim() !== '') {
    try {
      const v: unknown = JSON.parse(d.params)
      if (typeof v !== 'object' || v === null || Array.isArray(v)) return { problem: 'Parameters must be a JSON object.' }
      params = v as Record<string, unknown>
    } catch {
      return { problem: 'Parameters are not valid JSON.' }
    }
  }
  if (d.mode === 'repos' && d.repoIds.length === 0) return { problem: 'Choose at least one repository, or all of them.' }
  const gate: NonNullable<ScheduleBody['gate']> = {}
  if (d.run !== '') gate.run = d.run
  if (d.plan !== '') gate.plan = d.plan
  if (d.merge !== '') gate.merge = d.merge
  const body: ScheduleBody = {
    name: d.name.trim(),
    scope: d.mode === 'all' ? { mode: 'all' } : { mode: 'repos', repo_ids: d.repoIds },
    cron: d.cron.trim(),
    timezone: d.timezone.trim() || 'UTC',
    ...(params === undefined ? {} : { params }),
    ...(Object.keys(gate).length === 0 ? {} : { gate }),
  }
  if (editing === null) return { body: { ...body, type: d.type } }
  return { body: { ...body, revision: editing.revision } }
}

export function ScheduleEditScreen({ id, go }: { id: string | null; go: (to: string) => void }) {
  const [types, setTypes] = useState<ScheduleType[] | null>(null)
  const [repos, setRepos] = useState<RepoRecord[] | null>(null)
  const [editing, setEditing] = useState<Schedule | null>(null)
  const [readError, setReadError] = useState<ApiError | null>(null)
  const [draft, setDraft] = useState<Draft>(draftOf(null))

  useEffect(() => {
    let live = true
    void loadScheduleTypes().then((r) => {
      if (!live) return
      if (r.status === 'ok' || r.status === 'stale') setTypes(r.data.types)
      else if (r.status === 'error') setReadError(r.error)
      else if (r.status === 'empty') setTypes([])
    })
    void loadRepositories().then((r) => {
      if (live) setRepos(r.status === 'ok' || r.status === 'stale' ? r.data : [])
    })
    if (id !== null) {
      void loadSchedule(id).then((r) => {
        if (!live) return
        if (r.status === 'ok' || r.status === 'stale') {
          setEditing(r.data.schedule)
          setDraft(draftOf(r.data.schedule))
        } else if (r.status === 'error') setReadError(r.error)
      })
    }
    return () => {
      live = false
    }
  }, [id])

  const title = id === null ? 'New schedule' : `Edit ${editing?.name ?? id}`
  const back = id === null ? SCHEDULES : scheduleAddress(id)
  if (readError !== null) {
    return (
      <div className="au-edit">
        <PageHead title={title} />
        <Banner tone="bad" title="This form could not be read">
          {readError.message}
        </Banner>
      </div>
    )
  }
  if (types === null || (id !== null && editing === null)) {
    return (
      <div className="au-edit">
        <PageHead title={title} />
        <p className="au-dim">Reading the schedule types…</p>
      </div>
    )
  }
  return (
    <div className="au-edit">
      <RoutedLink to={back} go={go} className="rn-back">
        ‹ {id === null ? 'All schedules' : 'Back to the schedule'}
      </RoutedLink>
      <PageHead title={title} />
      <EditForm types={types} repos={repos} editing={editing} draft={draft} setDraft={setDraft} go={go} />
    </div>
  )
}

function EditForm({
  types,
  repos,
  editing,
  draft,
  setDraft,
  go,
}: {
  types: ScheduleType[]
  repos: RepoRecord[] | null
  editing: Schedule | null
  draft: Draft
  setDraft: (f: (d: Draft) => Draft) => void
  go: (to: string) => void
}) {
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setDraft((d) => ({ ...d, [k]: v }))
  const entry = types.find((t) => t.name === draft.type) ?? null
  const creatable = types.filter((t) => t.available && t.creatable_scopes.some((m) => m === 'repos' || m === 'all'))

  async function submit() {
    setProblem(null)
    if (editing === null && draft.type === '') {
      setProblem('Choose a type.')
      return
    }
    const built = bodyOf(draft, editing)
    if ('problem' in built) {
      setProblem(built.problem)
      return
    }
    setBusy(true)
    const r = editing === null ? await createSchedule({ ...built.body, client_request_id: crypto.randomUUID() }) : await editSchedule(editing.schedule_id, built.body)
    setBusy(false)
    if (r.status === 'error') {
      setProblem(r.error.code === 'schedule_changed' ? 'Someone changed this schedule since you opened it. Go back and open it again.' : r.error.message)
      return
    }
    if (r.status === 'ok' || r.status === 'stale') go(scheduleAddress(r.data.schedule.schedule_id))
  }

  return (
    <form
      className="au-form"
      onSubmit={(e) => {
        e.preventDefault()
        void submit()
      }}
    >
      <Card title="What">
        <label className="c-field">
          <span className="c-lbl">Name</span>
          <input className="c-inp" value={draft.name} maxLength={80} required onChange={(e) => set('name', e.target.value)} />
        </label>
        <label className="c-field">
          <span className="c-lbl">Type</span>
          <select className="c-inp" value={draft.type} disabled={editing !== null} onChange={(e) => set('type', e.target.value)}>
            <option value="">Choose a type</option>
            {(editing === null ? creatable : types).map((t) => (
              <option key={t.name} value={t.name}>
                {t.name}
              </option>
            ))}
          </select>
          {entry !== null && <span className="c-hint">{entry.description}</span>}
          {editing === null && types.some((t) => !t.available) && (
            <span className="c-hint">
              Not built yet: {types.filter((t) => !t.available).map((t) => t.name).join(', ')}.
            </span>
          )}
        </label>
        <fieldset className="au-scope">
          <legend className="c-lbl">Repositories</legend>
          <label>
            <input type="radio" checked={draft.mode === 'all'} onChange={() => set('mode', 'all')} /> All, as registered when it fires
          </label>
          <label>
            <input type="radio" checked={draft.mode === 'repos'} onChange={() => set('mode', 'repos')} /> These
          </label>
          {draft.mode === 'repos' &&
            (repos === null ? (
              <span className="c-hint">Reading the repositories…</span>
            ) : repos.length === 0 ? (
              <span className="c-hint">No repository is registered. Register one under Work › Repositories first.</span>
            ) : (
              <div className="au-repos">
                {repos.map((r) => (
                  <label key={r.repo_id}>
                    <input
                      type="checkbox"
                      checked={draft.repoIds.includes(r.repo_id)}
                      onChange={(e) =>
                        setDraft((d) => ({ ...d, repoIds: e.target.checked ? [...d.repoIds, r.repo_id] : d.repoIds.filter((x) => x !== r.repo_id) }))
                      }
                    />{' '}
                    <span className="mono">
                      {r.owner}/{r.repo}
                    </span>
                  </label>
                ))}
              </div>
            ))}
        </fieldset>
      </Card>
      <Card title="When">
        <div className="au-presets" role="group" aria-label="Presets">
          {CRON_PRESETS.map((p) => (
            <Button key={p.cron} size="sm" kind={draft.cron === p.cron ? 'primary' : 'secondary'} onClick={() => set('cron', p.cron)}>
              {p.label}
            </Button>
          ))}
        </div>
        <div className="au-when">
          <label className="c-field">
            <span className="c-lbl">Cron</span>
            <input className="c-inp is-mono" value={draft.cron} onChange={(e) => set('cron', e.target.value)} />
          </label>
          <label className="c-field">
            <span className="c-lbl">Time zone</span>
            <input className="c-inp" value={draft.timezone} onChange={(e) => set('timezone', e.target.value)} />
          </label>
        </div>
        <CronPreview cron={draft.cron} timezone={draft.timezone} type={draft.type} />
      </Card>
      <Card title="Gate">
        <p className="c-hint">Left at the type’s default unless you choose. Below the type’s floor is refused; below its default needs a platform admin.</p>
        <div className="au-gate">
          <GateSelect label="Each run" value={draft.run} options={['auto', 'approve']} dflt={entry?.default_gate.run} onChange={(v) => set('run', v as Draft['run'])} />
          <GateSelect label="Each plan" value={draft.plan} options={['auto', 'approve']} dflt={entry?.default_gate.plan} onChange={(v) => set('plan', v as Draft['plan'])} />
          <GateSelect label="Merge" value={draft.merge} options={['off', 'approve']} dflt={entry?.default_gate.merge} onChange={(v) => set('merge', v as Draft['merge'])} />
        </div>
        <p className="c-hint">Fully automatic merging is a switch on the schedule’s Gate tab, not a field here: it is audited and needs a second person.</p>
      </Card>
      <Card title="Parameters">
        <label className="c-field">
          <span className="c-lbl">JSON, optional: the type’s defaults fill anything left out</span>
          <textarea className="c-inp is-mono au-json" rows={5} value={draft.params} onChange={(e) => set('params', e.target.value)} />
        </label>
      </Card>
      {problem !== null && (
        <Banner tone="bad" title="Not saved">
          {problem}
        </Banner>
      )}
      <div className="au-acts">
        <Button type="submit" kind="primary" busy={busy}>
          {editing === null ? 'Create schedule' : 'Save'}
        </Button>
      </div>
    </form>
  )
}

function GateSelect({ label, value, options, dflt, onChange }: { label: string; value: string; options: readonly string[]; dflt: string | undefined; onChange: (v: string) => void }) {
  return (
    <label className="c-field">
      <span className="c-lbl">{label}</span>
      <select className="c-inp" value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">The type’s default{dflt === undefined ? '' : ` (${dflt})`}</option>
        {options.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </label>
  )
}

/** The words and the next five, re-asked a moment after the field stops changing. */
export function CronPreview({ cron, timezone, type }: { cron: string; timezone: string; type: string }) {
  const [state, setState] = useState<{ preview: SchedulePreview } | { error: string } | null>(null)
  useEffect(() => {
    let live = true
    const timer = setTimeout(() => {
      void previewSchedule(cron.trim(), timezone.trim() || 'UTC', type === '' ? null : type).then((r) => {
        if (!live) return
        if (r.status === 'ok' || r.status === 'stale') setState({ preview: r.data.preview })
        else if (r.status === 'error') setState({ error: r.error.message })
      })
    }, 300)
    return () => {
      live = false
      clearTimeout(timer)
    }
  }, [cron, timezone, type])
  if (state === null) return <p className="c-hint">Reading the expression…</p>
  if ('error' in state) return <p className="c-hint au-bad" role="alert">{state.error}</p>
  const p = state.preview
  const viewer = viewerZone()
  return (
    <div className="au-preview" aria-live="polite">
      <p>
        <b>{p.words}</b>
      </p>
      {p.refusal !== null && (
        <p className="au-bad" role="alert">
          {p.refusal.reason ?? p.refusal.message ?? p.refusal.code}
        </p>
      )}
      <table className="au-next">
        <thead>
          <tr>
            <th scope="col">Next five, {timezone || 'UTC'}</th>
            <th scope="col">Yours, {viewer}</th>
          </tr>
        </thead>
        <tbody>
          {p.next.map((n) => (
            <tr key={n}>
              <td>{whenIn(n, timezone || 'UTC')}</td>
              <td>{whenIn(n, viewer)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
