/**
 * CONFIG › PLUGINS (part of #2688; docs/design/agent-plugins.md). The owner
 * picked the settings table on 4 November 2026 (plugins console mock-ups,
 * direction A): a facts strip, the catalogue as a table whose row opens to
 * its skills per mode, and one Allowed here switch per plugin, because the
 * workspace allows whole plugins and each step picks its own skills. A
 * change is drawn before it is signed: the save bar names it and what it
 * refuses, in `swarm_api.plugins.check_step`'s words, and Save and sign is
 * one `PUT /v1/workspaces/{tenant_id}/plugins` of `{allowed}`.
 *
 * WHAT NO ROUTE SERVES IS SAID AS NOT KNOWN, never invented and never 0:
 * what each skill does, the skills a plugin ships that no step can pick (and
 * why), the changes before the last one, and which steps name a plugin. The
 * route serves the last change (`set_by`, `set_at`) and the plugins in force,
 * which it reads only from a block whose signature verifies: a non-empty list
 * is therefore signed, and an empty one may be either.
 */
import { Fragment, useEffect, useState } from 'react'
import { loadWorkspacePlugins, saveWorkspacePlugins } from './api'
import { Banner, Button, Chip } from './components'
import type { ApiError } from './fetch'
import { UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { PageHead } from './Shell'
import { formatDateTime } from './timezone'
import { PLUGIN_MODES, type CataloguePlugin, type PluginMode, type WorkspacePlugins } from './types'
import './styles/plugins.css'

const ROUTE = 'GET /v1/plugins'

const MODE_WORDS: Readonly<Record<PluginMode, string>> = {
  unattended: 'Unattended',
  planner: 'Planner',
  designer: 'Designer',
}

/** What each mode means for the person who allows the plugin (docs/design/agent-plugins.md, "Modes"). */
const MODE_MEANS: Readonly<Record<PluginMode, string>> = {
  unattended: 'answers what has a recommended answer itself; anything else waits for a person',
  planner: 'interviews a person, then proposes a plan for approval',
  designer: 'interviews a person, then proposes a design for approval',
}

/** `check_step`'s refusal for a plugin the workspace has not allowed, word for word. */
export function notAllowedWords(name: string): string {
  return `this workspace has not allowed ${name}: its owner or admin allows it in the workspace’s plugins setting`
}

function sameSet(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((n) => b.includes(n))
}

function andList(names: readonly string[]): string {
  if (names.length <= 1) return names.join('')
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
}

function when(iso: string): string {
  return formatDateTime(iso)
}

/** The Setting fact: signed is known only for a non-empty served list. */
function settingWords(d: WorkspacePlugins) {
  if (d.allowed.length > 0) {
    return (
      <>
        <span className="plg-signed">
          <svg viewBox="0 0 16 16" aria-hidden><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" strokeWidth="1.8" /></svg>
          Signed
        </span>
        <span className="plg-dim"> · unsigned allows nothing</span>
      </>
    )
  }
  if (d.set_at === null) return <>Not set: the default applies</>
  return (
    <span title="The route reads only a list whose signature verifies, and an empty list and an unsigned one both read as none.">
      Signed: not known<span className="plg-dim"> · unsigned allows nothing</span>
    </span>
  )
}

export function ConfigPluginsScreen() {
  const read = useUrRead(loadWorkspacePlugins, 'plugins')
  return (
    <div className="plg-page">
      <PageHead title="Plugins">
        <UrRefresh reads={[read]} />
      </PageHead>
      <p className="plg-lead">
        Agent steps load plugins from the platform&rsquo;s closed catalogue. Each plugin is built into the agent images at
        one pinned version; a step names a plugin, a mode and skills by name. Nothing here takes a path, a command, a web
        address or a version.
      </p>
      <UrRegion state={read.state} route={ROUTE} what="The workspace's plugins" plural onRetry={read.reload}>
        {(d) => <PluginsSetting data={d} onSaved={read.reload} />}
      </UrRegion>
    </div>
  )
}

type Saved = { kind: 'signed' } | { kind: 'none' } | { kind: 'unsigned'; asked: string[] }

function PluginsSetting({ data: served, onSaved }: { data: WorkspacePlugins; onSaved: () => void }) {
  // The answer in force: the read's, or the PUT's once it answers (it serves the same view).
  const [data, setData] = useState(served)
  useEffect(() => setData(served), [served])
  const [allowed, setAllowed] = useState<string[]>(served.allowed)
  useEffect(() => setAllowed(served.allowed), [served])
  const [openRow, setOpenRow] = useState<string | null>(served.catalogue[0]?.name ?? null)
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<ApiError | null>(null)
  const [saved, setSaved] = useState<Saved | null>(null)

  const switchedOff = data.allowed.filter((n) => !allowed.includes(n))
  const switchedOn = allowed.filter((n) => !data.allowed.includes(n))
  const dirty = switchedOff.length + switchedOn.length

  function flip(name: string, on: boolean) {
    if (!data.can_edit) return
    setSaved(null)
    setFailed(null)
    setAllowed((was) => (on ? [...was.filter((n) => n !== name), name] : was.filter((n) => n !== name)))
  }

  async function save() {
    if (busy || !data.can_edit) return
    setBusy(true)
    setFailed(null)
    // In catalogue order, as the API stores them (sorted, deduplicated).
    const asked = data.catalogue.map((p) => p.name).filter((n) => allowed.includes(n))
    const r = await saveWorkspacePlugins(data.tenant_id, asked)
    setBusy(false)
    if (r.status === 'error') {
      setFailed(r.error)
      return
    }
    if (r.status === 'ok' || r.status === 'stale') {
      setData(r.data)
      setAllowed(r.data.allowed)
      // The answer reads only a signed list: asking for plugins and reading none back means it was stored unsigned.
      // An empty list reads the same signed or not, so it is only said saved.
      setSaved(asked.length === 0 ? { kind: 'none' } : sameSet(r.data.allowed, asked) ? { kind: 'signed' } : { kind: 'unsigned', asked })
      return
    }
    onSaved()
  }

  return (
    <div className="plg-body">
      {saved?.kind === 'signed' && (
        <Banner tone="info" title="Saved and signed">
          This workspace may use {andList(data.allowed)}. Steps naming a plugin it does not allow are refused from the next submission. Recorded as {data.set_by ?? 'you'}
          {data.set_at === null ? '' : `, ${when(data.set_at)}`}.
        </Banner>
      )}
      {saved?.kind === 'none' && (
        <Banner tone="info" title="Saved">
          No plugin is allowed in this workspace now: steps that name one are refused from the next submission.
        </Banner>
      )}
      {saved?.kind === 'unsigned' && (
        <Banner tone="warn" title="Saved, not signed">
          The API stored {andList(saved.asked)} but could not sign the list, and a list without a valid signature allows
          nothing: no step in this workspace can use a plugin until the deployment has its settings key.
        </Banner>
      )}

      <dl className="plg-strip" role="list" aria-label="The setting">
        <div role="listitem">
          <dt>This workspace may use</dt>
          <dd>{data.allowed.length === 0 ? <span>No plugin</span> : data.allowed.map((n) => <Chip key={n}>{n}</Chip>)}</dd>
        </div>
        <div role="listitem">
          <dt>When nobody has chosen</dt>
          <dd>
            {data.default_allowed === null
              ? <span className="plg-dim">not known</span>
              : data.default_allowed.length === 0 ? 'No plugin allowed' : andList(data.default_allowed)}
          </dd>
        </div>
        <div role="listitem">
          <dt>Setting</dt>
          <dd>{settingWords(data)}</dd>
        </div>
        <div role="listitem">
          <dt>Last change</dt>
          <dd>
            {data.set_at === null && data.set_by === null
              ? 'Never changed'
              : <>{data.set_by ?? 'who: not known'}, {data.set_at === null ? 'when: not known' : when(data.set_at)}</>}
          </dd>
        </div>
        <div role="listitem">
          <dt>You</dt>
          <dd>{data.can_edit ? 'Can change it' : <>Read only</>}</dd>
        </div>
      </dl>
      {!data.can_edit && (
        <p className="plg-readonly">
          You can read this setting, not change it: only this workspace&rsquo;s owner or an admin chooses which plugins
          its steps may use. Each step picks its own skills from what the plugin offers.
        </p>
      )}

      {data.catalogue.length === 0 ? (
        <p className="plg-dim">The catalogue this API serves has no plugin.</p>
      ) : (
        <div className="plg-cat" role="table" aria-label="The plugin catalogue">
          <div className="plg-cat-head" role="row">
            <span role="columnheader"><span className="plg-sr">Open</span></span>
            <span role="columnheader">Plugin</span>
            <span role="columnheader">Pinned version</span>
            <span role="columnheader">Runs on these runner profiles</span>
            <span role="columnheader">Skills per mode</span>
            <span role="columnheader" className="plg-end">Allowed here</span>
          </div>
          {data.catalogue.map((p) => (
            <CatalogueRow
              key={p.name}
              plugin={p}
              open={openRow === p.name}
              onToggle={() => setOpenRow(openRow === p.name ? null : p.name)}
              allowed={allowed.includes(p.name)}
              changed={switchedOff.includes(p.name) || switchedOn.includes(p.name)}
              canEdit={data.can_edit}
              onFlip={(on) => flip(p.name, on)}
              data={data}
            />
          ))}
        </div>
      )}

      {data.can_edit && dirty > 0 && (
        <div className="plg-savebar" role="region" aria-label="Changes not saved">
          <div className="plg-savebar-t">
            <b>{dirty} {dirty === 1 ? 'change' : 'changes'} not saved:</b>
            <span className="plg-savebar-list">
              {switchedOff.map((n) => <span key={n}>{n}: allowed here <span aria-hidden>→</span><span className="plg-sr"> to</span> not allowed</span>)}
              {switchedOn.map((n) => <span key={n}>{n}: not allowed <span aria-hidden>→</span><span className="plg-sr"> to</span> allowed here</span>)}
            </span>
            {switchedOff.length > 0 && (
              <span className="plg-savebar-refuse">
                From the next submission, steps that name {andList(switchedOff)} are refused, each with{' '}
                {switchedOff.map((n, i) => (
                  <Fragment key={n}>{i > 0 && ' and '}&ldquo;{notAllowedWords(n)}&rdquo;</Fragment>
                ))}
                . Which steps name {switchedOff.length === 1 ? 'it' : 'them'} is not known here: no route serves it.
              </span>
            )}
            <span className="plg-dim plg-small">
              Saving signs the setting and records you and the time. Steps already running keep what they loaded.
            </span>
            {failed !== null && (
              <span className="plg-failed" role="alert">Not saved: {failed.message}</span>
            )}
          </div>
          <span className="plg-savebar-b">
            <Button kind="ghost" disabled={busy} onClick={() => { setAllowed(data.allowed); setFailed(null) }}>Discard</Button>
            <Button kind="primary" busy={busy} onClick={() => void save()}>Save and sign</Button>
          </span>
        </div>
      )}
    </div>
  )
}

function CatalogueRow({ plugin: p, open, onToggle, allowed, changed, canEdit, onFlip, data }: {
  plugin: CataloguePlugin
  open: boolean
  onToggle: () => void
  allowed: boolean
  changed: boolean
  canEdit: boolean
  onFlip: (on: boolean) => void
  data: WorkspacePlugins
}) {
  const label = `Allow ${p.name} in this workspace`
  return (
    <div className={`plg-cat-group${open ? ' is-open' : ''}`} role="rowgroup">
      <div className="plg-cat-row" role="row">
        <span role="cell">
          <button type="button" className="plg-caret" aria-expanded={open} aria-label={`${open ? 'Close' : 'Open'} ${p.name}`} onClick={onToggle}>
            <svg viewBox="0 0 16 16" aria-hidden><path d={open ? 'M4 6l4 4 4-4' : 'M6 4l4 4-4 4'} fill="none" stroke="currentColor" strokeWidth="1.6" /></svg>
          </button>
        </span>
        <span role="cell" className="plg-cat-name">{p.name}</span>
        <span role="cell" className="plg-cat-ver">
          <code className="plg-ver" title={p.version}>{p.version === '' ? 'not known' : p.version}</code>
          <span className="plg-dim plg-sub">built into the images</span>
        </span>
        <span role="cell" className="plg-chips">
          {p.runner_profiles.length === 0 ? <span className="plg-dim">not known</span> : p.runner_profiles.map((r) => <Chip key={r}>{r}</Chip>)}
        </span>
        <span role="cell" className="plg-modecounts">
          {PLUGIN_MODES.map((m) => {
            const mode = p.modes[m]
            return (
              <span key={m} className={`plg-modecount${mode?.available === true ? '' : ' is-not-yet'}`}>
                {MODE_WORDS[m]} {mode === undefined ? <span className="plg-dim">none</span> : <b>{mode.skills.length}</b>}
                {mode !== undefined && !mode.available && <span className="plg-dim"> · not yet</span>}
              </span>
            )
          })}
        </span>
        <span role="cell" className="plg-end">
          <label className={`plg-switch${changed ? ' is-changed' : ''}${canEdit ? '' : ' is-inert'}`} title={label}>
            <input type="checkbox" role="switch" checked={allowed} disabled={!canEdit} aria-label={label}
              onChange={(e) => onFlip(e.target.checked)} />
            <span className="plg-switch-track" aria-hidden><span /></span>
            <span className="plg-switch-word">{allowed ? 'On' : 'Off'}</span>
          </label>
        </span>
      </div>

      {open && (
        <div className="plg-cat-body" role="row">
          <div role="cell" className="plg-cat-cell">
            {!allowed && (
              <p className="plg-refuse">
                Not allowed in this workspace: steps that name {p.name} are refused. The skills below are what it would
                offer once allowed.
              </p>
            )}
            <div className="plg-modes">
              {PLUGIN_MODES.map((m) => <ModeColumn key={m} plugin={p} mode={m} />)}
              <section className="plg-never" aria-label={`${p.name}, never offered`}>
                <h3>Never offered</h3>
                <p className="plg-note">
                  Which skills {p.name} ships that no step can pick, and why: not known here. The catalogue route serves
                  only the skills a step may name.
                </p>
              </section>
            </div>
            <p className="plg-audit">
              <span className="plg-k">Changes</span>
              {data.set_at === null && data.set_by === null ? (
                <span>Never changed: the default applies</span>
              ) : (
                <span>
                  <b>{data.set_at === null ? 'when: not known' : when(data.set_at)}</b> {data.set_by ?? 'who: not known'}: set this workspace&rsquo;s plugins
                </span>
              )}
              <span className="plg-dim"> · earlier changes: not served</span>
            </p>
          </div>
        </div>
      )}
    </div>
  )
}

function ModeColumn({ plugin: p, mode: m }: { plugin: CataloguePlugin; mode: PluginMode }) {
  const mode = p.modes[m]
  return (
    <section className={`plg-modecol is-${m}`} aria-label={`${p.name}, ${MODE_WORDS[m]} mode`}>
      <header>
        <h3>{MODE_WORDS[m]}</h3>
        {mode !== undefined && (mode.available
          ? <span className="plg-runs">Runs today</span>
          : <span className="plg-runs is-not">Not yet</span>)}
        {mode !== undefined && <span className="plg-dim plg-n">{mode.skills.length} {mode.skills.length === 1 ? 'skill' : 'skills'}</span>}
      </header>
      <p className="plg-means">{MODE_MEANS[m]}</p>
      {mode === undefined ? (
        <p className="plg-note">Offers no skill in this mode.</p>
      ) : (
        <>
          {!mode.available && mode.reason !== null && <p className="plg-refuse">{p.name} {mode.reason}.</p>}
          {!mode.available && mode.reason === null && <p className="plg-refuse">{p.name} does not run in this mode yet; why: not known.</p>}
          <ul className="plg-skills">
            {mode.skills.map((s) => <li key={s}><code className="plg-skill">{s}</code></li>)}
          </ul>
          <p className="plg-note">What each skill does: not known here. The catalogue route serves skills by name only.</p>
        </>
      )}
    </section>
  )
}
