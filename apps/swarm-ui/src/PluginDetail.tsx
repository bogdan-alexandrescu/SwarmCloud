/**
 * A MANAGED PLUGIN'S PAGE (part of #2688; docs/design/managed-plugins.md
 * section 9, lane 10b). The owner picked the cards direction on 6 November
 * 2026: each plugin has its own page with Overview, Settings, Versions and
 * Usage tabs.
 *
 * Overview is the installed version as `GET .../installs/{install_id}/
 * versions` serves it: its skills, hooks, commands, agents and MCP servers,
 * and beside them where the pinned copy came from and what it needs. Settings
 * is the workspace's values for it (`GET .../plugin-config/{install_id}`):
 * never a secret's value, only whether it is stored, by whom and when.
 * Versions lists every pinned version; picking one other than the current
 * one reads the diff from the current to it, and one admin applies it,
 * quoting the diff's digest so what applies is exactly what they read, with
 * the plugin's name typed when the diff is marked. Usage is who uses it, and
 * Remove lists the same before it asks for the name.
 *
 * The page is exported for the shared wiring lane, which routes to it.
 */
import { useState, type ReactNode } from 'react'
import {
  applyPluginVersion,
  checkPluginUpdate,
  loadPluginDiff,
  loadPluginInWorkspace,
  loadPluginIntake,
  loadPluginSettings,
  loadPluginUsage,
  loadPluginVersions,
  pluginScopeOf,
  removePlugin,
  type ManagedInstall,
  type ManagedVersion,
  type PluginDiffAnswer,
  type PluginInWorkspace,
  type PluginIntake,
  type PluginPlace,
  type PluginSettingRow,
  type PluginSettings,
  type PluginUsage,
  type PluginVersions,
} from './api'
import { Banner, Breadcrumb, Button, Chip, EmptyState, Tabs, TypedConfirm } from './components'
import type { ApiError, Result } from './fetch'
import { InfoTip } from './People'
import { HookLine, PluginDiff, bytesWords, shortSha } from './PluginDiff'
import { UrRefresh, UrRegion, useUrRead, type UrRead } from './RepositoriesParts'
import { formatDateTime } from './timezone'
import './styles/plugin-detail.css'

export type PluginTab = 'overview' | 'settings' | 'versions' | 'usage'

const TAB_WORDS: Readonly<Record<PluginTab, string>> = {
  overview: 'Overview',
  settings: 'Settings',
  versions: 'Versions',
  usage: 'Usage',
}

const VERSIONS_ROUTE = 'GET {base}/installs/{install_id}/versions'
const SETTINGS_ROUTE = 'GET /v1/workspaces/{tenant_id}/plugin-config/{install_id}'
const USAGE_ROUTE = 'GET {base}/installs/{install_id}/usage'
const DIFF_ROUTE = 'GET {base}/installs/{install_id}/diff'

const TIPS = {
  hooks: 'Hooks are code the agent runs itself on the event they name, on every matching turn, under the step’s identity.',
  pinned: 'A step runs this exact copy: its digest is signed with the step.',
  apply: 'Steps already submitted keep the version they were signed with; steps submitted after the apply run the new one.',
  remove: 'Steps that name it are refused at their next submission. Steps already queued or running keep their pinned copy. Its settings and secrets are deleted.',
  writeOnly: 'Stored write-only. No route returns it.',
  upload: 'An uploaded plugin’s new version is uploaded from Add plugin.',
  settings: 'Each workspace sets its own values; a step may override a non-secret one.',
} as const

function when(iso: string | null): string {
  return iso === null ? '—' : formatDateTime(iso)
}

function sourceWords(s: Record<string, string>): string {
  if (s.kind === 'marketplace') return [s.marketplace, s.plugin].filter(Boolean).join(' › ') || 'Marketplace'
  if (s.kind === 'upload') return s.filename ?? 'Upload'
  return [s.repository, s.subdir].filter(Boolean).join('/') || '—'
}

function errorWords(e: ApiError): string {
  return e.message
}

/** Read the page: the install and its versions, and, in a workspace, its own facts about the plugin. */
export function PluginDetailScreen({
  installId,
  tenantId,
  backHref = null,
  initialTab = 'overview',
  onRemoved,
}: {
  installId: string
  /** The workspace the page is read in; null on Admin › Plugins. */
  tenantId: string | null
  /** The catalogue's address, for the breadcrumb; the wiring lane passes it. */
  backHref?: string | null
  initialTab?: PluginTab
  onRemoved?: (install: ManagedInstall) => void
}) {
  const place: PluginPlace = { installId, tenantId }
  const versions = useUrRead(() => loadPluginVersions(place), `plugin-versions:${tenantId ?? ''}:${installId}`)
  const inWorkspace = useUrRead<PluginInWorkspace | null>(
    () => (tenantId === null ? Promise.resolve({ status: 'ok', fetchedAt: Date.now(), data: null }) : loadPluginInWorkspace(tenantId, installId)),
    `plugin-in-workspace:${tenantId ?? ''}:${installId}`,
  )
  const [tab, setTab] = useState<PluginTab>(initialTab)
  const [removed, setRemoved] = useState<ManagedInstall | null>(null)

  const v = okData(versions.state)
  const w = okData(inWorkspace.state)
  const install: ManagedInstall | null = removed ?? v?.install ?? w?.entry ?? null
  const scope = pluginScopeOf(installId)
  // A platform install's routes are a platform admin's: reading its versions is the proof. A workspace's need its owner or admin.
  const canChange = removed === null && (scope === 'platform' ? v !== null : w?.can_install === true)
  const tabs: PluginTab[] = tenantId === null ? ['overview', 'versions', 'usage'] : ['overview', 'settings', 'versions', 'usage']
  const shown = tabs.includes(tab) ? tab : 'overview'
  const name = install?.name ?? installId

  return (
    <div className="pd-page">
      {backHref !== null && <Breadcrumb crumbs={[{ key: 'plugins', label: 'Plugins', href: backHref }, { key: 'plugin', label: name, href: null }]} />}
      <Head
        install={install}
        installId={installId}
        workspace={w}
        reads={[versions, inWorkspace]}
        place={place}
        canRemove={canChange && install !== null && install.removable}
        onRemoved={(r) => {
          setRemoved(r)
          onRemoved?.(r)
        }}
      />
      {removed !== null && (
        <Banner tone="info" title={`${removed.name} removed`}>
          <InfoTip about="removing" tip={TIPS.remove} />
        </Banner>
      )}
      <Tabs
        className="pd-tabs"
        label={name}
        current={shown}
        onSelect={(k) => setTab(k as PluginTab)}
        tabs={tabs.map((t) => ({ key: t, label: TAB_WORDS[t] }))}
      />
      <div role="tabpanel" aria-label={TAB_WORDS[shown]} className="pd-panel">
        {shown === 'overview' && <Overview versions={versions} fallback={w?.entry ?? null} />}
        {shown === 'settings' && tenantId !== null && <SettingsTab tenantId={tenantId} installId={installId} />}
        {shown === 'versions' && <VersionsTab versions={versions} place={place} canChange={canChange} />}
        {shown === 'usage' && <UsageTab place={place} workspace={w} />}
      </div>
    </div>
  )
}

function okData<T>(r: Result<T>): T | null {
  return r.status === 'ok' || r.status === 'stale' ? r.data : null
}

function Head({
  install,
  installId,
  workspace,
  reads,
  place,
  canRemove,
  onRemoved,
}: {
  install: ManagedInstall | null
  installId: string
  workspace: PluginInWorkspace | null
  reads: readonly UrRead<unknown>[]
  place: PluginPlace
  canRemove: boolean
  onRemoved: (r: ManagedInstall) => void
}) {
  const [removing, setRemoving] = useState(false)
  const scope = install?.scope ?? pluginScopeOf(installId)
  return (
    <header className="pd-head">
      <div className="pd-head-t">
        <h1>
          {install?.display_name ?? install?.name ?? installId}
          {install?.version != null && <code className="pd-ver">{install.version}</code>}
        </h1>
        <span className="pd-head-m">
          <span className={`pd-scope is-${scope}`}>{scope === 'platform' ? 'Platform' : 'Workspace'}</span>
          {workspace !== null && scope === 'platform' && workspace.entry?.allowed != null && (
            <span className={`pd-fact${workspace.entry.allowed ? ' is-on' : ''}`}>{workspace.entry.allowed ? 'Allowed' : 'Not allowed'}</span>
          )}
          {workspace !== null && workspace.every_step && <span className="pd-fact is-on">Every step</span>}
          {install !== null && install.state !== 'active' && <span className="pd-fact is-off">Removed</span>}
        </span>
      </div>
      <div className="pd-acts">
        <UrRefresh reads={reads} />
        {canRemove && (
          <Button kind="danger" onClick={() => setRemoving(true)}>
            Remove
          </Button>
        )}
      </div>
      {removing && install !== null && (
        <RemoveDialog install={install} place={place} workspace={workspace} onClose={() => setRemoving(false)} onRemoved={(r) => { setRemoving(false); onRemoved(r) }} />
      )}
    </header>
  )
}

/* ------------------------------------------------------------------ overview */

function Overview({ versions, fallback }: { versions: UrRead<PluginVersions>; fallback: ManagedInstall | null }) {
  // A workspace member reads a platform plugin through its workspace's catalogue: its counts, not its parts.
  if (versions.state.status === 'error' && versions.state.error.httpStatus === 403 && fallback !== null) return <OverviewCounts install={fallback} />
  return (
    <UrRegion state={versions.state} route={VERSIONS_ROUTE} what="The plugin" onRetry={versions.reload}>
      {(d) => {
        const current = d.versions.find((x) => x.current) ?? null
        return current === null ? <OverviewCounts install={d.install} /> : <OverviewOf install={d.install} v={current} />
      }}
    </UrRegion>
  )
}

function Facts({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="pd-facts">
      {rows.map(([k, val]) => (
        <div key={k}>
          <dt>{k}</dt>
          <dd>{val}</dd>
        </div>
      ))}
    </dl>
  )
}

function Panel({ title, count, tip, children }: { title: string; count?: number; tip?: string; children: ReactNode }) {
  return (
    <section className="pd-box" aria-label={title}>
      <h2 className="pd-box-h">
        {title}
        {count !== undefined && <em>{count}</em>}
        {tip !== undefined && <InfoTip about={title.toLowerCase()} tip={tip} />}
      </h2>
      {children}
    </section>
  )
}

function OverviewOf({ install, v }: { install: ManagedInstall; v: ManagedVersion }) {
  const c = v.components
  const named: [string, string[]][] = [
    ['Skills', c.skills],
    ['Commands', c.commands],
    ['Agents', c.agents],
    ['MCP servers', c.mcp_servers],
  ]
  return (
    <div className="pd-ov">
      <div className="pd-ov-main">
        {install.description !== null && <p className="pd-desc">{install.description}</p>}
        {c.hooks.length > 0 && (
          <Panel title="Hooks" count={c.hooks.length} tip={TIPS.hooks}>
            <ul className="pd-hooks" role="list">
              {c.hooks.map((h, i) => <HookLine key={i} h={h} />)}
            </ul>
          </Panel>
        )}
        {named.filter(([, names]) => names.length > 0).map(([title, names]) => (
          <Panel key={title} title={title} count={names.length}>
            <ul className="pd-names" role="list">
              {names.map((n) => <li key={n}><code>{n}</code></li>)}
            </ul>
          </Panel>
        ))}
      </div>
      <aside className="pd-box pd-ov-side" aria-label="The pinned copy">
        <Facts
          rows={[
            ['Source', <code className="pd-wrap">{sourceWords(v.source)}</code>],
            ...(v.license !== null ? [['License', v.license] as [string, ReactNode]] : []),
            ['Pinned', (
              <span className="pd-pin">
                {v.source.commit !== undefined && <span>commit <code>{shortSha(v.source.commit)}</code></span>}
                <span>sha256 <code title={v.runnable_sha256 ?? undefined}>{shortSha(v.runnable_sha256)}</code></span>
                <InfoTip about="the pinned copy" tip={TIPS.pinned} />
              </span>
            )],
            ['Size', `${v.files ?? '—'} files · ${bytesWords(v.unpacked_bytes)}`],
            ['Installed', `${v.created_by ?? '—'} · ${when(v.created_at)}`],
            ...(v.runtimes.length + v.hosts.length > 0
              ? [['Needs', <>{v.runtimes.map((r) => <Chip key={r}>{r}</Chip>)}{v.hosts.map((h) => <Chip key={h}>net: {h}</Chip>)}</>] as [string, ReactNode]]
              : []),
          ]}
        />
      </aside>
    </div>
  )
}

function OverviewCounts({ install }: { install: ManagedInstall }) {
  const counts = Object.entries(install.counts).filter(([, n]) => n > 0)
  return (
    <div className="pd-ov">
      <div className="pd-ov-main">
        {install.description !== null && <p className="pd-desc">{install.description}</p>}
        <dl className="pd-facts pd-counts">
          {counts.map(([k, n]) => (
            <div key={k}>
              <dt>{k === 'mcp_servers' ? 'MCP servers' : k[0]!.toUpperCase() + k.slice(1)}</dt>
              <dd>{n}</dd>
            </div>
          ))}
        </dl>
      </div>
      <aside className="pd-box pd-ov-side" aria-label="The pinned copy">
        <Facts
          rows={[
            ['Source', <code className="pd-wrap">{sourceWords(install.source)}</code>],
            ['Pinned', <code title={install.runnable_sha256 ?? undefined}>{shortSha(install.runnable_sha256)}</code>],
            ['Updated', `${install.updated_by ?? '—'} · ${when(install.updated_at)}`],
            ...(install.hosts.length > 0 ? [['Needs', <>{install.hosts.map((h) => <Chip key={h}>net: {h}</Chip>)}</>] as [string, ReactNode]] : []),
          ]}
        />
      </aside>
    </div>
  )
}

/* ------------------------------------------------------------------ settings */

function settingValue(f: PluginSettingRow): ReactNode {
  if (f.sensitive) {
    if (f.binding !== null) return <span className="pd-fact is-on">Bound: {f.binding}</span>
    return f.set ? <span className="pd-fact is-on">Stored</span> : <span className="pd-fact is-off">Not set</span>
  }
  if (f.kind === 'file') return f.set ? <span className="pd-fact is-on">Stored</span> : <span className="pd-fact is-off">Not set</span>
  if (f.set && f.value !== null) return <code>{String(f.value)}</code>
  if (f.default !== null) return <span className="pd-dim">default <code>{String(f.default)}</code></span>
  return <span className="pd-fact is-off">Not set</span>
}

function SettingsTab({ tenantId, installId }: { tenantId: string; installId: string }) {
  const read = useUrRead(() => loadPluginSettings(tenantId, installId), `plugin-settings:${tenantId}:${installId}`)
  return (
    <UrRegion state={read.state} route={SETTINGS_ROUTE} what="The plugin's settings" plural onRetry={read.reload}>
      {(d: PluginSettings) =>
        d.fields.length === 0 ? (
          <EmptyState kind="empty" heading="No settings">
            <InfoTip about="settings" tip={TIPS.settings} />
          </EmptyState>
        ) : (
          <section className="pd-box" aria-label="Settings">
            <h2 className="pd-box-h">
              Settings <em>{d.fields.length}</em>
              <InfoTip about="settings" tip={TIPS.settings} />
              {d.set_at !== null && <span className="pd-dim pd-push">{d.set_by ?? '—'} · {when(d.set_at)}</span>}
            </h2>
            <div className="pd-rows" role="table" aria-label="Settings">
              {d.fields.map((f) => (
                <div key={f.key} className="pd-row" role="row">
                  <span role="cell" className="pd-row-k">
                    <code>{f.key}</code>
                    {f.title !== null && <span className="pd-dim">{f.title}</span>}
                  </span>
                  <span role="cell" className="pd-dim">{f.sensitive ? 'secret' : f.type}</span>
                  <span role="cell">
                    {settingValue(f)}
                    {f.sensitive && <InfoTip about={`${f.key}, a secret`} tip={TIPS.writeOnly} />}
                    {d.missing.includes(f.key) && <span className="pd-fact is-warn">Required</span>}
                  </span>
                </div>
              ))}
            </div>
          </section>
        )
      }
    </UrRegion>
  )
}

/* ------------------------------------------------------------------ versions */

function VersionsTab({ versions, place, canChange }: { versions: UrRead<PluginVersions>; place: PluginPlace; canChange: boolean }) {
  const [target, setTarget] = useState<string | null>(null)
  return (
    <UrRegion state={versions.state} route={VERSIONS_ROUTE} what="The plugin's versions" plural onRetry={versions.reload}>
      {(d) => {
        const picked = target === null ? null : d.versions.find((x) => x.version_id === target) ?? null
        if (picked !== null && !picked.current) {
          return (
            <UpdateView
              place={place}
              install={d.install}
              from={d.versions.find((x) => x.current) ?? null}
              to={picked}
              canChange={canChange}
              onBack={() => setTarget(null)}
              onApplied={() => {
                setTarget(null)
                versions.reload()
              }}
            />
          )
        }
        return <VersionList data={d} place={place} canChange={canChange} onPick={setTarget} onChecked={versions.reload} />
      }}
    </UrRegion>
  )
}

function VersionList({
  data,
  place,
  canChange,
  onPick,
  onChecked,
}: {
  data: PluginVersions
  place: PluginPlace
  canChange: boolean
  onPick: (versionId: string) => void
  onChecked: () => void
}) {
  const current = data.versions.find((x) => x.current) ?? null
  const newer = (x: ManagedVersion) => current !== null && (x.created_at ?? '') > (current.created_at ?? '')
  return (
    <section className="pd-box" aria-label="Versions">
      <h2 className="pd-box-h">
        Versions <em>{data.versions.length}</em>
        {canChange && data.install.updatable && (
          <span className="pd-push">
            <UpdateCheck install={data.install} place={place} onPinned={onChecked} />
          </span>
        )}
      </h2>
      <ul className="pd-vers" role="list">
        {data.versions.map((x) => (
          <li key={x.version_id} className={`pd-vr${x.current ? ' is-current' : newer(x) ? ' is-new' : ''}`}>
            <code className="pd-vr-v">{x.version ?? x.version_id}</code>
            <span className="pd-dim">
              {x.source.commit !== undefined ? <>commit <code>{shortSha(x.source.commit)}</code></> : <>sha256 <code>{shortSha(x.runnable_sha256)}</code></>}
            </span>
            <span className="pd-dim">{x.created_by ?? '—'} · {when(x.created_at)}</span>
            <span className="pd-vr-s">
              {x.current && <span className="pd-fact is-on">Current</span>}
              {!x.current && newer(x) && <span className="pd-fact is-new">New</span>}
              {!x.current && (
                <Button size="sm" kind={newer(x) ? 'primary' : 'ghost'} onClick={() => onPick(x.version_id)}>
                  {newer(x) ? 'Review the update' : `Go back to ${x.version ?? x.version_id}`}
                </Button>
              )}
            </span>
          </li>
        ))}
      </ul>
    </section>
  )
}

/** Check for a new version: its intake runs as an install's, and its result is a candidate, never current until applied. */
function UpdateCheck({ install, place, onPinned }: { install: ManagedInstall; place: PluginPlace; onPinned: () => void }) {
  const [busy, setBusy] = useState(false)
  const [intake, setIntake] = useState<PluginIntake | null>(null)
  const [failed, setFailed] = useState<ApiError | null>(null)
  if (install.source.kind === 'upload') {
    return <InfoTip about="updating an upload" tip={TIPS.upload} />
  }
  async function act(run: () => Promise<Result<PluginIntake>>) {
    setBusy(true)
    setFailed(null)
    const r = await run()
    setBusy(false)
    if (r.status === 'error') {
      setFailed(r.error)
      return
    }
    if (r.status === 'ok' || r.status === 'stale') {
      setIntake(r.data)
      if (r.data.state === 'pinned') onPinned()
    }
  }
  const running = intake !== null && (intake.state === 'running' || intake.state === 'awaiting_upload')
  return (
    <span className="pd-check">
      {running ? (
        <Button size="sm" kind="ghost" busy={busy} onClick={() => void act(() => loadPluginIntake(place, intake.intake_id))}>
          Checking · read again
        </Button>
      ) : (
        <Button size="sm" kind="ghost" busy={busy} onClick={() => void act(() => checkPluginUpdate(place, null))}>
          Check for a new version
        </Button>
      )}
      {intake?.state === 'refused' && (
        <span className="pd-fact is-warn" role="status">
          Refused: {intake.refusal?.code ?? '—'}
          {intake.refusal?.sentence != null && <InfoTip about="the refusal" tip={intake.refusal.sentence} />}
        </span>
      )}
      {failed !== null && <span className="pd-fact is-warn" role="alert">{errorWords(failed)}</span>}
    </span>
  )
}

function UpdateView({
  place,
  install,
  from,
  to,
  canChange,
  onBack,
  onApplied,
}: {
  place: PluginPlace
  install: ManagedInstall
  from: ManagedVersion | null
  to: ManagedVersion
  canChange: boolean
  onBack: () => void
  onApplied: () => void
}) {
  const diff = useUrRead(() => loadPluginDiff(place, to.version_id), `plugin-diff:${place.tenantId ?? ''}:${place.installId}:${to.version_id}`)
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<ApiError | null>(null)
  const label = to.version ?? to.version_id

  async function apply(d: PluginDiffAnswer, confirmName: string | null) {
    setBusy(true)
    setFailed(null)
    const r = await applyPluginVersion(place, to.version_id, d.diff_sha256, confirmName)
    setBusy(false)
    if (r.status === 'error') {
      setFailed(r.error)
      // The diff moved since it was read: read it again, so the next apply quotes what is shown.
      if (r.error.code === 'diff_changed') {
        setConfirming(false)
        diff.reload()
      }
      return
    }
    setConfirming(false)
    onApplied()
  }

  return (
    <section className="pd-box pd-upd" aria-label={`Update ${install.name}`}>
      <h2 className="pd-box-h">
        {install.name} <code>{from?.version ?? '—'}</code> <span aria-hidden>→</span> <code>{label}</code>
        <InfoTip about="applying" tip={TIPS.apply} />
      </h2>
      <UrRegion state={diff.state} route={DIFF_ROUTE} what="The diff" onRetry={diff.reload}>
        {(d) => (
          <>
            <PluginDiff diff={d.diff} />
            {failed !== null && !confirming && (
              <Banner tone="bad" title="Not applied" role="alert">
                {errorWords(failed)}
              </Banner>
            )}
            <div className="pd-bar">
              <Button kind="ghost" onClick={onBack}>
                Cancel
              </Button>
              {canChange && (
                <Button
                  kind="primary"
                  busy={busy && !confirming}
                  onClick={() => (d.diff.needs_confirmation ? setConfirming(true) : void apply(d, null))}
                >
                  {`Update to ${label}`}
                </Button>
              )}
            </div>
            {confirming && (
              <TypedConfirm
                title={`Update ${install.name} to ${label}`}
                name={install.name}
                verb={`Update to ${label}`}
                keep="Cancel"
                busy={busy}
                error={failed === null ? undefined : errorWords(failed)}
                onConfirm={() => void apply(d, install.name)}
                onClose={() => setConfirming(false)}
              >
                <p className="pd-marked-line">
                  {d.diff.marked.map((m) => <span key={m} className="pd-mark">{m === 'mcp_servers' ? 'MCP servers' : m}</span>)}
                  <InfoTip about="the marked changes" tip={TIPS.apply} />
                </p>
              </TypedConfirm>
            )}
          </>
        )}
      </UrRegion>
    </section>
  )
}

/* ------------------------------------------------------------------ usage */

function usageCount(u: PluginUsage): number {
  return u.workflows.length + u.schedules.length + u.steps.length
}

/** The workspace's own facts: allowed (a platform plugin), on for every step. */
function WorkspaceFacts({ workspace }: { workspace: PluginInWorkspace }) {
  return (
    <dl className="pd-facts pd-wsfacts" aria-label="This workspace">
      {workspace.entry?.allowed != null && (
        <div>
          <dt>Allowed</dt>
          <dd>{workspace.entry.allowed ? 'Yes' : 'No'}</dd>
        </div>
      )}
      <div>
        <dt>Every step</dt>
        <dd>{workspace.every_step ? 'Yes' : 'No'}</dd>
      </div>
    </dl>
  )
}

function UsageLists({ u, showTenant }: { u: PluginUsage; showTenant: boolean }) {
  if (usageCount(u) === 0 && u.workspaces.length === 0) {
    return <EmptyState kind="empty" heading="Nothing uses it">{null}</EmptyState>
  }
  return (
    <div className="pd-usage">
      {u.workspaces.length > 0 && (
        <div className="pd-rows" role="table" aria-label="Workspaces">
          {u.workspaces.map((x) => (
            <div key={x.tenant_id} className="pd-row" role="row">
              <span role="cell"><b>{x.tenant_id}</b></span>
              <span role="cell">{x.allowed ? <span className="pd-fact is-on">Allowed</span> : <span className="pd-fact">Not allowed</span>}</span>
              <span role="cell">{x.every_step && <span className="pd-fact is-on">Every step</span>}</span>
            </div>
          ))}
        </div>
      )}
      {u.workflows.length > 0 && (
        <div className="pd-rows" role="table" aria-label="Workflows">
          {u.workflows.map((x) => (
            <div key={`${x.tenant_id}:${x.id}`} className="pd-row" role="row">
              <span role="cell" className="pd-row-k">
                <b>{x.name}</b>
                {showTenant && <span className="pd-dim">{x.tenant_id}</span>}
              </span>
              <span role="cell" className="pd-chips">{x.steps.map((s) => <Chip key={s}>{s}</Chip>)}</span>
              <span role="cell" className="pd-dim">{x.how === 'every_step' ? 'Every step' : x.kind}</span>
            </div>
          ))}
        </div>
      )}
      {u.schedules.length > 0 && (
        <div className="pd-rows" role="table" aria-label="Schedules">
          {u.schedules.map((x) => (
            <div key={`${x.tenant_id}:${x.schedule_id}`} className="pd-row" role="row">
              <span role="cell"><b>{x.name}</b></span>
              <span role="cell" className="pd-dim">{showTenant ? x.tenant_id : ''}</span>
            </div>
          ))}
        </div>
      )}
      {u.steps.length > 0 && (
        <div className="pd-rows" role="table" aria-label="Steps now">
          {u.steps.map((x) => (
            <div key={x.task_id} className="pd-row" role="row">
              <span role="cell"><code>{x.step ?? x.task_id}</code></span>
              <span role="cell" className="pd-dim">{x.workflow_id ?? ''}</span>
              <span role="cell"><span className="pd-fact">{x.state}</span></span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function useUsage(place: PluginPlace): UrRead<PluginUsage> {
  return useUrRead(() => loadPluginUsage(place), `plugin-usage:${place.tenantId ?? ''}:${place.installId}`)
}

function UsageTab({ place, workspace }: { place: PluginPlace; workspace: PluginInWorkspace | null }) {
  const usage = useUsage(place)
  return (
    <div className="pd-usage-tab">
      {workspace !== null && <WorkspaceFacts workspace={workspace} />}
      <UrRegion state={usage.state} route={USAGE_ROUTE} what="Who uses it" onRetry={usage.reload}>
        {(u) => <UsageLists u={u} showTenant={place.tenantId === null} />}
      </UrRegion>
    </div>
  )
}

/* ------------------------------------------------------------------ remove */

function RemoveDialog({
  install,
  place,
  workspace,
  onClose,
  onRemoved,
}: {
  install: ManagedInstall
  place: PluginPlace
  workspace: PluginInWorkspace | null
  onClose: () => void
  onRemoved: (r: ManagedInstall) => void
}) {
  const usage = useUsage(place)
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<ApiError | null>(null)

  async function remove() {
    setBusy(true)
    setFailed(null)
    const r = await removePlugin(place, install.name)
    setBusy(false)
    if (r.status === 'error') {
      setFailed(r.error)
      return
    }
    if (r.status === 'ok' || r.status === 'stale') onRemoved(r.data)
  }

  return (
    <TypedConfirm
      title={`Remove ${install.name}`}
      name={install.name}
      verb="Remove"
      keep="Keep it"
      busy={busy}
      error={failed === null ? undefined : errorWords(failed)}
      onConfirm={() => void remove()}
      onClose={onClose}
    >
      <div className="pd-rm">
        <p className="pd-rm-h">
          {usage.state.status === 'ok' || usage.state.status === 'stale' ? <b>{`Used by ${usageCount(usage.state.data)}`}</b> : <b>Used by</b>}
          <InfoTip about="removing" tip={TIPS.remove} />
        </p>
        {workspace !== null && <WorkspaceFacts workspace={workspace} />}
        <UrRegion state={usage.state} route={USAGE_ROUTE} what="Who uses it" onRetry={usage.reload} lines={2}>
          {(u) => <UsageLists u={u} showTenant={place.tenantId === null} />}
        </UrRegion>
      </div>
    </TypedConfirm>
  )
}
