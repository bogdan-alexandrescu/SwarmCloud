import { useContext, useState, type FormEvent, type MouseEvent, type ReactNode } from 'react'
import { loadMe, loadProfile, renameWorkspace } from './api'
import { CodeIntelligenceCard } from './CodeIntelligence'
import type { ApiError } from './fetch'
import { Button, ButtonLink, Card, Tabs, routedClick } from './components'
import { SetupContext } from './ConfigSetup'
import { NewWorkspaceDialog } from './NewWorkspace'
import { ACCESS as GITHUB_ACCESS_ADDRESS, OnboardingScreen, STEP_LABEL, countLabel } from './Onboarding'
import { WORKSPACE_SETUP_ADDRESS, addressToPath } from './paths'
import { UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { PageHead } from './Shell'
import type { Profile } from './types'
import './styles/workspace-settings.css'

/**
 * CONFIG › WORKSPACE SETTINGS (WS-SETTINGS-PAGE; part of #2157, BGD's
 * decisions of 2026-10-23 and his pick of proposal E,
 * docs/web-ui/mockups/config-e.html S6a-S6f). ONE PAGE, IN PARTS, each at its
 * own address so a link reproduces it:
 *
 *   * Settings (`/config/workspace`): the workspace's settings, as Settings ›
 *     Workspace had them (code intelligence), and which workspace they are
 *     about, read from `GET /v1/profile`'s acting workspace;
 *   * Workspace setup (`/config/workspace/setup`): Work › Setup's checklist,
 *     mounted as a part (Onboarding.tsx), so the setup strip above every
 *     Config page has its long form here;
 *   * Add a new workspace (`/config/workspace?part=new`): A PLATFORM ADMIN'S
 *     PART ONLY (BGD, 2026-10-23T23:57Z), the rule of Admin › Tenants › New
 *     workspace: drawn when `GET /v1/profile` says `is_admin`, never while it
 *     is unread. A workspace admin sees two parts; members keep Request your
 *     workspace in the setup checklist.
 *
 * NO NEW CREATION PATH. Add a new workspace opens Admin › Tenants' own dialog
 * (NewWorkspace.tsx), which posts `POST /v1/admin/workspaces` and nothing
 * else. The API is the gate either way: a non-admin who reached the dialog
 * would be refused there, so hiding the part is for clarity, not security.
 *
 * Each part is led by one sentence and one button (proposal E): Settings and
 * Workspace setup by the checklist's next step, from the read the strip and
 * the checklist draw, so the lead cannot disagree with either.
 */

export type WorkspacePart = 'settings' | 'setup' | 'new'

const SETTINGS_ADDRESS = 'config/workspace'
/** The admin's part: a query on the tab, as Workspace setup's address was before it had a path of its own. */
export const WORKSPACE_NEW_ADDRESS = 'config/workspace?part=new'

/** The part a route's view names: `part=setup` or `part=new`; anything else is Settings. */
export function workspacePartOf(view: string | null): WorkspacePart {
  const part = new URLSearchParams(view ?? '').get('part')
  return part === 'setup' || part === 'new' ? part : 'settings'
}

function routed(to: string, go?: (to: string) => void) {
  return (e: MouseEvent) => {
    if (go === undefined || !routedClick(e)) return
    e.preventDefault()
    go(to)
  }
}

/** A part's lead: what it is about, in one line, and its one next action. */
function Lead({ kicker, title, body, tone = 'next', action }: { kicker: string; title: string; body?: ReactNode; tone?: 'next' | 'ok'; action?: ReactNode }) {
  return (
    <section className={`ws-lead is-${tone}`} aria-label={kicker}>
      <div className="ws-lead-t">
        <span className="ws-lead-k">{kicker}</span>
        <b>{title}</b>
        {body !== undefined && <span className="ws-lead-s">{body}</span>}
      </div>
      {action !== undefined && <div className="ws-lead-a">{action}</div>}
    </section>
  )
}

/** The checklist steps whose next action is on GitHub access (Onboarding.tsx links each there). */
const ON_GITHUB_ACCESS: ReadonlySet<string> = new Set(['github_connected', 'orgs_enabled', 'repos_chosen', 'access_verified'])

const STATE_WORD: Readonly<Record<string, string>> = { done: 'done', in_progress: 'in progress', failed: 'failed', stale: 'needs a re-check', todo: 'to do' }

/**
 * The checklist's next step as a lead, from the shell's onboarding read (the
 * one the strip and Workspace setup draw), so the lead can never call setup
 * finished while the checklist under it shows a failed step. Nothing until
 * the read lands: a lead is never a guess.
 */
function SetupLead({ part, go }: { part: 'settings' | 'setup'; go?: (to: string) => void }) {
  const { doc } = useContext(SetupContext)
  if (doc.status !== 'ok' && doc.status !== 'stale') return null
  const d = doc.data
  if (!Array.isArray(d.steps) || d.steps.length === 0) return null
  if (d.complete) {
    return <Lead tone="ok" kicker={part === 'settings' ? 'Nothing here needs you' : 'Setup is complete'} title={`Every step is done: ${countLabel(d)}`} />
  }
  const at = d.steps.findIndex((s) => s.step === d.next_step)
  const next = at === -1 ? null : d.steps[at]!
  if (next === null) return null
  const label = STEP_LABEL[next.step] ?? next.step
  const title = `${label}: ${STATE_WORD[next.state] ?? next.state}`
  if (part === 'settings') {
    return (
      <Lead
        kicker="Setup’s next step"
        title={title}
        body={countLabel(d)}
        action={
          <ButtonLink kind="primary" size="sm" href={addressToPath(WORKSPACE_SETUP_ADDRESS)} onClick={routed(WORKSPACE_SETUP_ADDRESS, go)}>
            Open Workspace setup
          </ButtonLink>
        }
      />
    )
  }
  return (
    <Lead
      kicker={`Step ${at + 1} of ${d.steps.length} · next`}
      title={title}
      body="The strip above summarises this checklist."
      action={
        ON_GITHUB_ACCESS.has(next.step) ? (
          <ButtonLink kind="primary" size="sm" href={addressToPath(GITHUB_ACCESS_ADDRESS)} onClick={routed(GITHUB_ACCESS_ADDRESS, go)}>
            Open GitHub access
          </ButtonLink>
        ) : undefined
      }
    />
  )
}

/** `workspaces.MAX_DISPLAY_NAME`; the API refuses a longer name as 422. */
const MAX_WORKSPACE_NAME = 60

/**
 * RENAME (WORKSPACE-ADMIN-FIELDS, part of #2157; config-e.html S6a): the workspace's display name, never its id. Offered
 * to the workspace's admins in a group workspace -- a personal workspace is always "Your workspace" in the switcher --
 * and the API is the gate: anyone else is refused 403 with nothing written. Every rule on the name is the API's (1-60
 * characters, no control or invisible character), drawn as its code and sentence. A rename tells the shell
 * (`WORKSPACE_RENAMED_EVENT`), which reads its name again, so the switcher and the headers follow.
 */
function RenameForm({ tenantId, current, onDone, onCancel }: { tenantId: string; current: string; onDone: (name: string) => void; onCancel: () => void }) {
  const [name, setName] = useState(current)
  const [saving, setSaving] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const typed = name.trim()
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (saving || typed === '') return
    if (typed === current) {
      onCancel()
      return
    }
    setSaving(true)
    setRefused(null)
    const r = await renameWorkspace(tenantId, typed)
    setSaving(false)
    if (r.status === 'ok' || r.status === 'stale') onDone(r.data.display_name)
    else if (r.status === 'error') setRefused(r.error)
  }
  return (
    <form className="ws-rename" aria-label="Rename this workspace" onSubmit={(e) => void submit(e)}>
      <label className="c-field">
        <span className="c-lbl">New name</span>
        <input
          className="c-inp"
          type="text"
          value={name}
          maxLength={MAX_WORKSPACE_NAME}
          autoComplete="off"
          autoFocus
          disabled={saving}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      <p className="ur-hint">
        Changes the name everyone in it sees. The id <code>{tenantId}</code> stays as it is.
      </p>
      {refused !== null && (
        <p className="ur-small ob-issue" role="alert" data-code={refused.code ?? undefined}>
          {refused.code !== null && <b className="ur-bad">{refused.code}</b>} {refused.message}
        </p>
      )}
      <div className="ws-rename-a">
        <Button kind="primary" size="sm" type="submit" busy={saving ? 'Saving…' : false} disabled={typed === ''}>
          Save name
        </Button>
        <Button size="sm" disabled={saving} onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  )
}

/** The acting workspace's name as the shell draws it: `/v1/tenants/me`'s, which a rename changes. */
const loadThisMe = () => loadMe()

/** The workspace these settings are about: the profile's acting workspace. */
function ThisWorkspace({ profile }: { profile: Profile }) {
  const ws = profile.workspaces.find((w) => w.is_acting) ?? null
  const me = useUrRead(loadThisMe, `me:${ws?.tenant_id ?? ''}`)
  const [renamed, setRenamed] = useState<string | null>(null)
  const [renaming, setRenaming] = useState(false)
  const read = me.state.status === 'ok' || me.state.status === 'stale' ? me.state.data : null
  const stored = read !== null && ws !== null && read.tenant.tenant_id === ws.tenant_id ? read.tenant.display_name || null : null
  const name = renamed ?? stored ?? ws?.display_name ?? ''
  const mayRename = ws !== null && ws.kind === 'group' && ws.role.name === 'admin'
  return (
    <Card
      title="This workspace"
      action={
        mayRename && !renaming ? (
          <Button size="sm" onClick={() => setRenaming(true)}>
            Rename
          </Button>
        ) : undefined
      }
    >
      {ws === null ? (
        <p className="ur-hint">The profile read names no acting workspace.</p>
      ) : (
        <dl className="ws-kv">
          <dt>Name</dt>
          <dd>{name}</dd>
          <dt>Id</dt>
          <dd>
            <code>{ws.tenant_id}</code>
          </dd>
          <dt>Kind</dt>
          <dd>{ws.kind === 'user' ? 'personal' : ws.kind || 'unknown'}</dd>
          <dt>Your role</dt>
          <dd>{ws.role.name || 'unknown'}</dd>
        </dl>
      )}
      {ws !== null && renaming && (
        <RenameForm
          tenantId={ws.tenant_id}
          current={name}
          onCancel={() => setRenaming(false)}
          onDone={(next) => {
            setRenamed(next)
            setRenaming(false)
          }}
        />
      )}
    </Card>
  )
}

/** Add a new workspace, for a platform admin: Admin › Tenants' New workspace dialog, unchanged. */
function AddWorkspace() {
  const [open, setOpen] = useState(false)
  const [filed, setFiled] = useState<string | null>(null)
  return (
    <>
      <Lead
        kicker="Platform admins only"
        title="Add a new workspace"
        body="A personal or group workspace you file is approved at once, because you are a platform admin; a service workspace waits for the platform owner’s approval. Its progress is on Admin › Tenants."
        action={
          <Button kind="primary" size="sm" onClick={() => setOpen(true)}>
            New workspace
          </Button>
        }
      />
      <Card title="What creating it does">
        <p className="ur-hint">
          The form asks for the kind, one address, an optional display name and, for a group, an optional first admin, and
          posts them to{' '}
          <code>POST /v1/admin/workspaces</code>, the route Admin › Tenants › New workspace uses. The workspace job then
          gives it its own worker account, IAM, storage prefix, namespace and pool, and the form ticks each step as the job
          checks it.
        </p>
        {filed !== null && (
          <p className="ur-hint">
            Filed <code>{filed}</code>. Admin › Tenants lists it under Being created until it is ready.
          </p>
        )}
      </Card>
      {open && <NewWorkspaceDialog onClose={() => setOpen(false)} onFiled={(r) => setFiled(r.workspace_id ?? r.request_id ?? 'the request')} />}
    </>
  )
}

export function WorkspaceSettingsScreen({ part = 'settings', go }: { part?: WorkspacePart; go?: (to: string) => void }) {
  const profile = useUrRead(loadProfile, 'profile')
  const read = profile.state.status === 'ok' || profile.state.status === 'stale' ? profile.state.data : null
  const platformAdmin = read !== null && read.identity.is_admin === true
  const tabs = [
    { key: 'settings', label: 'Settings', href: addressToPath(SETTINGS_ADDRESS) },
    { key: 'setup', label: 'Workspace setup', href: addressToPath(WORKSPACE_SETUP_ADDRESS) },
    ...(platformAdmin ? [{ key: 'new', label: 'Add a new workspace', href: addressToPath(WORKSPACE_NEW_ADDRESS) }] : []),
  ]
  return (
    <div className="pk-page ws-page">
      <PageHead title="Workspace Settings">
        <UrRefresh reads={[profile]} />
      </PageHead>
      <Tabs className="cfg-parts" label="Workspace Settings" current={part} onGo={go} tabs={tabs} />
      {part === 'settings' && (
        <>
          <SetupLead part="settings" go={go} />
          <div className="ws-grid">
            <UrRegion state={profile.state} route="GET /v1/profile" what="This workspace" onRetry={profile.reload}>
              {(p) => <ThisWorkspace profile={p} />}
            </UrRegion>
            <CodeIntelligenceCard />
          </div>
        </>
      )}
      {part === 'setup' && (
        <>
          <SetupLead part="setup" go={go} />
          <OnboardingScreen />
        </>
      )}
      {part === 'new' &&
        (platformAdmin ? (
          <AddWorkspace />
        ) : read === null && profile.state.status === 'loading' ? (
          <p className="ur-hint">Reading whether you are a platform admin…</p>
        ) : (
          <Lead
            kicker="Not available to you"
            title="Adding a workspace is for platform admins"
            body="To ask for a workspace of your own, use Request your workspace in Workspace setup."
            action={
              <ButtonLink size="sm" href={addressToPath(WORKSPACE_SETUP_ADDRESS)} onClick={routed(WORKSPACE_SETUP_ADDRESS, go)}>
                Open Workspace setup
              </ButtonLink>
            }
          />
        ))}
    </div>
  )
}
