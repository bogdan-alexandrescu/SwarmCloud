import { useState, type MouseEvent, type ReactNode } from 'react'
import { loadProfile } from './api'
import { CodeIntelligenceCard } from './CodeIntelligence'
import { Button, ButtonLink, Card, Tabs, routedClick } from './components'
import { useSetupSteps } from './ConfigSetup'
import { NewWorkspaceDialog } from './NewWorkspace'
import { OnboardingScreen } from './Onboarding'
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
 * Workspace setup by the setup strip's next step, from the same read, so the
 * lead and the strip cannot disagree; a step nothing measures is never
 * called done.
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

/** The Config page each strip step is done on, by its address, for the lead's button. */
const PAGE_NAME: Readonly<Record<string, string>> = {
  'config/github': 'GitHub access',
  'config/repositories': 'Repositories',
  'config/secrets': 'Secrets',
  'config/people': 'People and roles',
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

/** The setup strip's next step as a lead, or that every measured step is done. */
function SetupLead({ part, go }: { part: 'settings' | 'setup'; go?: (to: string) => void }) {
  const steps = useSetupSteps()
  const at = steps.findIndex((s) => s.state !== 'done' && s.state !== 'unknown')
  const unknown = steps.filter((s) => s.state === 'unknown').length
  if (at === -1) {
    const measured = steps.length - unknown
    return (
      <Lead
        tone="ok"
        kicker={part === 'settings' ? 'Nothing here needs you' : 'No measured step is left'}
        title={measured === 0 ? 'Setup has not been read yet' : `${measured} of ${steps.length} setup steps measured done`}
        body={unknown > 0 ? `${unknown} ${unknown === 1 ? 'step has' : 'steps have'} no read yet, so ${unknown === 1 ? 'it is' : 'they are'} not counted as done.` : undefined}
      />
    )
  }
  const next = steps[at]!
  const page = PAGE_NAME[next.to] ?? next.label
  return (
    <Lead
      kicker={part === 'settings' ? 'Setup’s next step' : `Step ${at + 1} of ${steps.length} · next`}
      title={`${next.label}: ${next.line}`}
      body={part === 'setup' ? 'The strip above is this checklist, summarised.' : undefined}
      action={
        <ButtonLink kind="primary" size="sm" href={addressToPath(next.to)} onClick={routed(next.to, go)}>
          Open {page}
        </ButtonLink>
      }
    />
  )
}

/** The workspace these settings are about: the profile's acting workspace. */
function ThisWorkspace({ profile }: { profile: Profile }) {
  const ws = profile.workspaces.find((w) => w.is_acting) ?? null
  return (
    <Card title="This workspace">
      {ws === null ? (
        <p className="ur-hint">The profile read names no acting workspace.</p>
      ) : (
        <dl className="ws-kv">
          <dt>Name</dt>
          <dd>{ws.display_name}</dd>
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
          The form asks for the kind, one address and an optional display name, and posts them to{' '}
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
