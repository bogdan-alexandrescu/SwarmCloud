import { useContext, useEffect, useId, useState, type FormEvent, type MouseEvent, type ReactNode } from 'react'
import {
  BUDGET_DOES,
  budgetAmount,
  budgetLimitsWords,
  budgetRefusalWords,
  dismissOnboarding,
  loadMe,
  loadProfile,
  loadQaSettings,
  loadWorkspaceBudget,
  loadWorkspaceRoles,
  parseBudgetAmount,
  putQaSettings,
  putWorkspaceBudget,
  renameWorkspace,
  type BudgetLimits,
  type BudgetUnit,
  type QaSettings,
  type WorkspaceBudget,
} from './api'
import { CodeIntelligenceCard } from './CodeIntelligence'
import { Banner, Button, ButtonLink, Card, Dash, Tabs, routedClick } from './components'
import { SetupContext, checklistCount, setupCount, useSetupSteps, type SetupStep } from './ConfigSetup'
import type { ApiError } from './fetch'
import { Refusal } from './GitHubConnect'
import { NewWorkspaceForm, STEP_LABEL as JOB_STEP_LABEL, STEP_ORDER as JOB_STEP_ORDER } from './NewWorkspace'
import { ACCESS as GITHUB_ACCESS_ADDRESS, OnboardingScreen, STEP_LABEL, countLabel } from './Onboarding'
import { GitHubPermissions } from './PermissionCards'
import { WORKSPACE_SETUP_ADDRESS, addressToPath } from './paths'
import { notServed } from './RepositoriesData'
import { UrRefresh, UrRegion, useUrRead, type UrRead } from './RepositoriesParts'
import { PageHead } from './Shell'
import { OWN_WORKSPACE_LABEL } from './Spine'
import { timeAgo, type OnboardingDoc, type Profile, type WorkspaceRoles } from './types'
import './styles/workspace-settings.css'

/**
 * CONFIG › WORKSPACE SETTINGS (WS-SETTINGS-PAGE; part of #2157, BGD's
 * decisions of 2026-10-23 and his pick of proposal E,
 * docs/web-ui/mockups/config-e.html S6a-S6f). ONE PAGE, IN PARTS, each at its
 * own address so a link reproduces it:
 *
 *   * Settings (`/config/workspace`): the workspace's settings, as Settings ›
 *     Workspace had them (code intelligence), its per-agent budget (#2459,
 *     `WorkspaceBudgetCard`), and which workspace they are about, read from
 *     `GET /v1/profile`'s acting workspace;
 *   * Workspace setup (`/config/workspace/setup`): Work › Setup's checklist,
 *     mounted as a part (Onboarding.tsx), so the setup strip above every
 *     Config page has its long form here;
 *   * Add a new workspace (`/config/workspace?part=new`): A PLATFORM ADMIN'S
 *     PART ONLY (BGD, 2026-10-23T23:57Z), the rule of Admin › Tenants › New
 *     workspace: drawn when `GET /v1/profile` says `is_admin`, never while it
 *     is unread. A workspace admin sees two parts; members keep Request your
 *     workspace in the setup checklist.
 *
 * NO NEW CREATION PATH. Add a new workspace draws Admin › Tenants' own form
 * (NewWorkspace.tsx's `NewWorkspaceForm`, the body of its dialog) on the page,
 * as S6c draws it (WS-SETTINGS-E-REST), and it posts `POST /v1/admin/workspaces`
 * and nothing else. The API is the gate either way: a non-admin who reached
 * the form would be refused there, so hiding the part is for clarity, not
 * security.
 *
 * Each part is led by one sentence and one button (proposal E): Settings and
 * Workspace setup by setup's next step, from the read the strip and the
 * checklist draw, so the lead cannot disagree with either. While the
 * checklist has a step left, that step leads (the strip's GitHub and
 * Repositories steps are checklist steps); once the checklist is complete the
 * lead moves on to the strip's next step (`useSetupSteps`, the strip's own
 * derivation: Secrets set, then People invited), and it is green only when
 * the strip has nothing left and nothing unmeasured (WS-SETTINGS-E-REST, S6a:
 * "Setup's next step is on Secrets · Go to Secrets").
 */

export type WorkspacePart = 'settings' | 'setup' | 'new'

/** Workspace Settings' own address; People and roles links a personal workspace back here. */
export const WORKSPACE_SETTINGS_ADDRESS = 'config/workspace'
const SETTINGS_ADDRESS = WORKSPACE_SETTINGS_ADDRESS
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
 * The Config page each strip step is done on, by name, for the lead's words.
 * Pairs, not an object literal: the publish scan reads a `secrets:` key with a
 * string value as a credential.
 */
const PAGE_OF: ReadonlyMap<SetupStep['key'], string> = new Map<SetupStep['key'], string>([
  ['github', 'GitHub access'],
  ['repositories', 'Repositories'],
  ['secrets', 'Secrets'],
  ['people', 'People and roles'],
])

/** The strip's next step: the first measured step not done, the rule the strip draws its `is-next` by. */
function stripNextAt(steps: readonly SetupStep[]): number {
  return steps.findIndex((s) => s.state !== 'done' && s.state !== 'unknown')
}

/**
 * Setup's next step as a lead, from the shell's onboarding read (the one the
 * strip and Workspace setup draw), so the lead can never call setup finished
 * while the checklist under it shows a failed step, or while the strip above
 * it still has Secrets or People open. Nothing until the read lands: a lead
 * is never a guess.
 */
function SetupLead({ part, go }: { part: 'settings' | 'setup'; go?: (to: string) => void }) {
  const { doc } = useContext(SetupContext)
  const strip = useSetupSteps()
  if (doc.status !== 'ok' && doc.status !== 'stale') return null
  const d = doc.data
  if (!Array.isArray(d.steps) || d.steps.length === 0) return null
  if (d.complete) return <StripLead part={part} steps={strip} doc={d} go={go} />
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
        // config2-03: the strip beside it counts steps; this names the checklist it counts.
        body={checklistCount(d) ?? countLabel(d)}
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
      // config-08: the strip counts its own three or four steps, not these; say both counts, each named.
      body={`The strip above counts its own ${strip.length} steps, ${setupCount(strip).done} of ${strip.length} done; this checklist counts ${countLabel(d)}.`}
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

/**
 * The lead once the checklist is complete: the strip's next step (S6a, S6b),
 * green only when every strip step is measured done. A step nothing measured
 * is said, never counted as done.
 */
function StripLead({ part, steps, doc, go }: { part: 'settings' | 'setup'; steps: readonly SetupStep[]; doc: OnboardingDoc; go?: (to: string) => void }) {
  const at = stripNextAt(steps)
  if (at === -1) {
    const unmeasured = steps.filter((s) => s.state === 'unknown')
    if (unmeasured.length === 0) {
      return <Lead tone="ok" kicker={part === 'settings' ? 'Nothing here needs you' : 'Setup is complete'} title={`Every step is done: ${countLabel(doc)}`} />
    }
    return (
      <Lead
        kicker={part === 'settings' ? 'Setup’s next step' : 'The checklist is done'}
        title={`${unmeasured.length} of ${steps.length} setup steps not measured`}
        body={unmeasured.map((s) => `${s.label}: ${s.line}`).join('; ')}
      />
    )
  }
  const next = steps[at]!
  const page = PAGE_OF.get(next.key) ?? next.label
  if (part === 'settings') {
    return (
      <Lead
        kicker="Setup’s next step"
        title={`Setup’s next step is on ${page}`}
        body={`${next.label}: ${next.line}`}
        action={
          <ButtonLink kind="primary" size="sm" href={addressToPath(next.to)} onClick={routed(next.to, go)}>
            Go to {page}
          </ButtonLink>
        }
      />
    )
  }
  return (
    <Lead
      kicker={`Step ${at + 1} of ${steps.length} · next`}
      title={`${next.label}: ${next.line}`}
      body="The checklist is done; the strip above has this step left."
      action={
        <ButtonLink kind="primary" size="sm" href={addressToPath(next.to)} onClick={routed(next.to, go)}>
          Open {page}
        </ButtonLink>
      }
    />
  )
}

/** A strip step's mark, as the strip draws it: a tick for done, a filled ring for next, a dashed ring for unmeasured. */
function StepRow({ step, next, action }: { step: Pick<SetupStep, 'label' | 'state'> & { line?: string }; next: boolean; action?: ReactNode }) {
  return (
    <li className={`ws-step is-${step.state}${next ? ' is-next' : ''}`} aria-label={step.label}>
      <span className="ws-mark" aria-hidden="true" />
      <span className="ws-step-t">
        <b>{step.label}</b>
        {step.line !== undefined && <small>{step.line}</small>}
      </span>
      {action}
    </li>
  )
}

/** Each after-the-checklist step's page and its button's word (S6b). */
const AFTER: ReadonlyArray<{ key: SetupStep['key']; verb: string }> = [
  { key: 'secrets', verb: 'Set' },
  { key: 'people', verb: 'Invite' },
]

/**
 * AFTER THE CHECKLIST (S6b): the strip's steps 3-4, Secrets set and People
 * invited, which the onboarding checklist does not hold, each with its line
 * and its button to the page it is done on. The same steps the strip draws
 * (`useSetupSteps`), so the card and the strip cannot disagree; People
 * invited is absent in a personal workspace, as it is from the strip (S1e).
 */
function AfterChecklist({ go }: { go?: (to: string) => void }) {
  const steps = useSetupSteps()
  const at = stripNextAt(steps)
  const nextKey = at === -1 ? null : steps[at]!.key
  const rows = AFTER.flatMap((a) => {
    const s = steps.find((x) => x.key === a.key)
    return s === undefined ? [] : [{ step: s, verb: a.verb }]
  })
  return (
    <Card title="After the checklist">
      <ol className="ws-steps" aria-label="After the checklist">
        {rows.map(({ step, verb }) => (
          <StepRow
            key={step.key}
            step={step}
            next={step.key === nextKey}
            action={
              <ButtonLink kind={step.key === nextKey ? 'primary' : undefined} size="sm" href={addressToPath(step.to)} onClick={routed(step.to, go)}>
                {verb}
              </ButtonLink>
            }
          />
        ))}
      </ol>
    </Card>
  )
}

/**
 * ON OVERVIEW (S6b): whether the setup card is on Overview for the reader,
 * and Show on Overview while they have hidden it: `{dismissed: false}`, the
 * write the checklist's own button makes. Read from the shell's onboarding
 * read; nothing until it lands.
 */
function OnOverview() {
  const { doc } = useContext(SetupContext)
  const [busy, setBusy] = useState(false)
  const [shown, setShown] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  if (doc.status !== 'ok' && doc.status !== 'stale') return null
  const d = doc.data
  const hidden = d.dismissed === true && !d.complete && !shown
  return (
    <Card title="On Overview">
      <p className="ur-hint">
        {d.complete
          ? 'The checklist is complete, so the setup card has left Overview by itself.'
          : hidden
            ? 'The setup card is hidden on Overview for you.'
            : shown
              ? 'The setup card is back on Overview for you.'
              : 'The setup card is on Overview until every checklist step is done. Hide there takes it off for you alone.'}
      </p>
      {hidden && (
        <div className="ws-acts">
          <Button
            size="sm"
            busy={busy}
            onClick={() => {
              setBusy(true)
              setRefused(null)
              void dismissOnboarding(false).then((res) => {
                setBusy(false)
                if (res.status === 'error') setRefused(res.error)
                else setShown(true)
              })
            }}
          >
            Show on Overview
          </Button>
        </div>
      )}
      {refused !== null && <Refusal error={refused} title="The checklist is still hidden from Overview" />}
    </Card>
  )
}

const PEOPLE_ADDRESS = 'config/people'

/** The workspace's default role, from the roles read People and roles makes (`GET /v1/workspace/roles`). */
function DefaultRole({ roles }: { roles: UrRead<WorkspaceRoles> }) {
  const r = roles.state
  if (r.status === 'loading') return <Dash why="Reading the workspace’s roles" />
  if (r.status === 'error') return <Dash why={`GET /v1/workspace/roles: ${r.error.message}`} />
  if (r.status === 'empty') return <Dash why="The roles read answered nothing" />
  return r.data.default_role === null ? <Dash why="The roles read names no default role" /> : <>{r.data.default_role}</>
}

/**
 * YOUR ROLE IS THE ONE THE API ACTS ON (VQ-044; part of #2391), not the
 * stored one. The profile's `role` is the person's stored grant (`member` by
 * the default), but `RoleStore.resolve` answers `admin` for a platform admin
 * in every group workspace, and that is what People and roles (`me` of
 * `GET /v1/workspace/roles`), Profile and Workspaces you admin draw: so this
 * card reads `me` too, and until that read answers, applies the same rule to
 * the profile (`identity.is_admin` is the same `auth.is_admin` the API
 * resolves with). A platform admin's role is drawn as Profile draws it,
 * "admin (platform admin)".
 */
export function effectiveRole(
  profile: Profile,
  ws: Profile['workspaces'][number],
  roles: UrRead<WorkspaceRoles>,
): { name: string; source: string } {
  const r = roles.state
  const read = r.status === 'ok' || r.status === 'stale' ? r.data : null
  if (read !== null && read.tenant_id === ws.tenant_id && read.me.role !== '') return { name: read.me.role, source: read.me.source }
  if (ws.kind !== 'user' && profile.identity.is_admin === true) return { name: 'admin', source: 'platform_admin' }
  return { name: ws.role.name, source: ws.role.source }
}

/** The workspace these settings are about: the profile's acting workspace, and its default role (S6a). */
function ThisWorkspace({ profile, roles, go }: { profile: Profile; roles: UrRead<WorkspaceRoles>; go?: (to: string) => void }) {
  const ws = profile.workspaces.find((w) => w.is_acting) ?? null
  const role = ws === null ? null : effectiveRole(profile, ws, roles)
  const me = useUrRead(loadThisMe, `me:${ws?.tenant_id ?? ''}`)
  const [renamed, setRenamed] = useState<string | null>(null)
  const [renaming, setRenaming] = useState(false)
  const read = me.state.status === 'ok' || me.state.status === 'stale' ? me.state.data : null
  const stored = read !== null && ws !== null && read.tenant.tenant_id === ws.tenant_id ? read.tenant.display_name || null : null
  // ONE NAME FOR THE PERSON'S OWN WORKSPACE (visual QA pass 2, admin-shell-21;
  // part of #2694): the switcher's `Your workspace`, the id on its own row
  // below, never `Your workspace (u-…)` here and `Bogdan` in the refusal.
  const name = ws?.kind === 'user' ? OWN_WORKSPACE_LABEL : (renamed ?? stored ?? ws?.display_name ?? '')
  const mayRename = ws !== null && ws.kind === 'group' && role?.name === 'admin'
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
          <dd data-testid="ws-your-role">
            {role === null || role.name === '' ? 'unknown' : role.source === 'platform_admin' ? `${role.name} (platform admin)` : role.name}
          </dd>
          {/* A personal workspace has nobody else in it, so no default to hold. */}
          {ws.kind !== 'user' && (
            <>
              <dt>Default role</dt>
              <dd data-testid="ws-default-role">
                <DefaultRole roles={roles} /> ·{' '}
                <a className="ctl-link" href={addressToPath(PEOPLE_ADDRESS)} onClick={routed(PEOPLE_ADDRESS, go)}>
                  People and roles
                </a>
              </dd>
            </>
          )}
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

/**
 * A CARD'S REFRESH, ONLY ON A ROUTE THIS API SERVES (visual QA pass 1, config-31; part of #2596). A card that says
 * "Not served yet" offered a refresh beside it, and a re-read cannot serve a route the build does not have. The
 * page head's refresh still re-reads the page; any other failure keeps the control, since a retry may answer.
 */
function servedRefresh(read: UrRead<unknown>): ReactNode {
  return read.state.status === 'error' && notServed(read.state.error) ? undefined : <UrRefresh reads={[read]} />
}

/**
 * VISUAL QA (lane QA-FLOWS-UI over QA-FLOWS-CAP, part of #2136; BGD, 2026-10-26): the workspace's `qa_flows_per_step`,
 * how many of a QA run's read-only flows one live-browser step runs when the run names none of its own (a run may,
 * beside Run on the QA targets page). Its default and its 1-30 bound are the API's, served with it. Its owner or admin,
 * or a platform admin, changes it (`can_edit`); the API is the gate, and anyone else sees it read-only, as Code
 * intelligence's switch is drawn.
 */
function QaSettingsCard() {
  const qa = useUrRead(loadQaSettings, 'qa-settings')
  return (
    <Card title="Visual QA" action={servedRefresh(qa)}>
      <UrRegion state={qa.state} route="GET /v1/qa-settings" what="the Visual QA settings" onRetry={qa.reload}>
        {(d) => <QaFlowsPerStep key={`${d.tenant_id}:${d.qa_flows_per_step}`} read={d} />}
      </UrRegion>
    </Card>
  )
}

function QaFlowsPerStep({ read }: { read: QaSettings }) {
  // What the PUT answered, drawn over the read until the next one.
  const [saved, setSaved] = useState<QaSettings | null>(null)
  const d = saved ?? read
  const [typed, setTyped] = useState(String(d.qa_flows_per_step))
  const [saving, setSaving] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  const value = /^\d+$/.test(typed.trim()) ? Number(typed.trim()) : null
  const inBounds = value !== null && value >= d.min_flows_per_step && value <= d.max_flows_per_step
  const bound = `${d.min_flows_per_step}-${d.max_flows_per_step}`
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (saving || !inBounds || value === d.qa_flows_per_step) return
    setSaving(true)
    setRefused(null)
    const r = await putQaSettings(d.tenant_id, value)
    setSaving(false)
    if (r.status === 'ok' || r.status === 'stale') {
      setSaved(r.data)
      setTyped(String(r.data.qa_flows_per_step))
    } else if (r.status === 'error') setRefused(r.error)
  }
  return (
    <>
      <p className="ur-hint">
        A QA run&rsquo;s read-only flows run in batches, one live-browser step to a batch. This is the batch size a run
        uses when it names none of its own; a run may pick another beside Run on the QA targets page.
      </p>
      {/* ONE LABELLED FIGURE (visual QA pass 1, config-30; part of #2596): for someone who may edit it, the field
          below is the figure and its hint notes the default, so the read-out above it said both twice. It is
          drawn for a reader only; Set by stays for both. */}
      {(!d.can_edit || d.set_by !== null) && (
        <dl className="ws-kv">
          {!d.can_edit && (
            <>
              <dt>QA flows per step</dt>
              <dd data-qa-flows-per-step="">
                {d.qa_flows_per_step}
                {d.set_by === null ? ' (the default)' : ` · default ${d.default_flows_per_step}`}
              </dd>
            </>
          )}
          {d.set_by !== null && (
            <>
              <dt>Set by</dt>
              <dd>{`${d.set_by}${d.set_at !== null ? ` ${timeAgo(d.set_at)}` : ''}`}</dd>
            </>
          )}
        </dl>
      )}
      {d.can_edit ? (
        <form className="ws-rename" aria-label="Change QA flows per step" onSubmit={(e) => void submit(e)}>
          <label className="c-field">
            <span className="c-lbl">QA flows per step</span>
            <input
              className="c-inp"
              type="number"
              inputMode="numeric"
              name="qa_flows_per_step"
              min={d.min_flows_per_step}
              max={d.max_flows_per_step}
              step={1}
              value={typed}
              disabled={saving}
              aria-invalid={!inBounds || undefined}
              onChange={(e) => setTyped(e.target.value)}
            />
          </label>
          <p className="ur-hint">{`A whole number, ${bound}. The default is ${d.default_flows_per_step}.`}</p>
          {/* Its own line: .ur-hint is declared after .ur-bad, so a hint carrying both stays grey. */}
          {!inBounds && (
            <small className="ur-bad" role="alert">{`QA flows per step is a whole number, ${bound}.`}</small>
          )}
          {refused !== null && (
            <p className="ur-small ob-issue" role="alert" data-code={refused.code ?? undefined}>
              {refused.code !== null && <b className="ur-bad">{refused.code}</b>} {refused.message}
            </p>
          )}
          <div className="ws-rename-a">
            <Button kind="primary" size="sm" type="submit" busy={saving ? 'Saving…' : false} disabled={!inBounds || value === d.qa_flows_per_step}>
              Save
            </Button>
            {saved !== null && value === saved.qa_flows_per_step && (
              <span className="pk-saved" role="status">
                Saved.
              </span>
            )}
          </div>
        </form>
      ) : (
        <p className="ur-small">{`Only the workspace’s owner or admin, or a platform admin, can change this (${bound}).`}</p>
      )}
    </>
  )
}

/**
 * PER-AGENT BUDGET (BUDGET-UI-SETTINGS; part of #2459, docs/design/agent-budget.md §2, §4). The most one agent may
 * spend on one attempt in this workspace, in dollars OR tokens, the unit chosen here for the whole workspace. OFF
 * UNTIL A WORKSPACE ADMIN SETS ONE (BGD, 2026-10-26): Off is drawn as the default, and a workspace that sets nothing
 * runs as it always has. A step may ask for less in its spec, never more.
 *
 * The platform ceiling is drawn as the most it can be. A default above it is refused 422 `budget_above_ceiling` with
 * nothing changed, and the refusal is said in words (`budgetRefusalWords`); the API is the judge, so a figure typed
 * over the ceiling is warned about but not blocked here. A ceiling an admin lowers later still binds: every attempt's
 * limit is the least of the step's, this default and the ceiling, taken at submission.
 *
 * Only the workspace's owner or admin, or a platform admin, changes it (`can_edit`, `budget.authorize_write`);
 * anyone else reads it, with no control drawn.
 */
type BudgetPick = 'off' | BudgetUnit

/** A unit's name on its choice. A function, not a map: the publish scan reads a `tokens:` key with a string value as a credential. */
const unitLabel = (u: BudgetUnit): string => (u === 'usd' ? 'Dollars' : 'Tokens')

function WorkspaceBudgetCard() {
  const budget = useUrRead(loadWorkspaceBudget, 'workspace-budget')
  return (
    <Card title="Per-agent budget" action={servedRefresh(budget)}>
      <p className="pk-lead">
        The most one agent may spend on a step in this workspace. {BUDGET_DOES} A step may set a lower budget, never a
        higher one.
      </p>
      <UrRegion state={budget.state} route="GET /v1/budget" what="the Per-agent budget setting" onRetry={budget.reload}>
        {(d) => (d.can_edit ? <BudgetForm read={d} /> : <BudgetReadOnly read={d} />)}
      </UrRegion>
    </Card>
  )
}

/** The default in words: "Off" or "$5.00 per agent". */
function budgetNow(b: WorkspaceBudget['budget']): string {
  return b === null ? 'Off' : `${budgetAmount(b.unit, b.amount)} per agent`
}

function CeilingLine({ ceiling }: { ceiling: BudgetLimits }) {
  const words = budgetLimitsWords(ceiling)
  return <>{words === null ? 'None set: a budget here may be any amount the platform takes' : `${words}, the most a budget here can be`}</>
}

function SetBy({ read }: { read: WorkspaceBudget }) {
  if (read.budget === null || read.set_by === null) return null
  return <p className="ur-small">{`Set by ${read.set_by}${read.set_at !== null ? ` ${timeAgo(read.set_at)}` : ''}.`}</p>
}

/** A member's view: what the budget is and the ceiling over it, with no control. */
function BudgetReadOnly({ read }: { read: WorkspaceBudget }) {
  return (
    <div className="jb-choice" data-testid="ws-budget">
      <dl className="ws-kv">
        <dt>Budget</dt>
        <dd data-testid="ws-budget-now">{budgetNow(read.budget)}</dd>
        <dt>Platform ceiling</dt>
        <dd>
          <CeilingLine ceiling={read.ceiling} />
        </dd>
      </dl>
      <SetBy read={read} />
      <p className="ur-small">Only the workspace&rsquo;s owner or admin, or a platform admin, can change this.</p>
    </div>
  )
}

function BudgetForm({ read }: { read: WorkspaceBudget }) {
  const id = useId()
  const [held, setHeld] = useState(read)
  const [pick, setPick] = useState<BudgetPick>(read.budget?.unit ?? 'off')
  const [typed, setTyped] = useState<Record<BudgetUnit, string>>({
    usd: read.budget?.unit === 'usd' ? String(read.budget.amount) : '',
    tokens: read.budget?.unit === 'tokens' ? String(read.budget.amount) : '',
  })
  const [busy, setBusy] = useState(false)
  const [saved, setSaved] = useState(false)
  const [refused, setRefused] = useState<ApiError | null>(null)
  // A re-read from the card's refresh is the newer answer: it resets the form.
  useEffect(() => {
    setHeld(read)
    setPick(read.budget?.unit ?? 'off')
    setTyped({ usd: read.budget?.unit === 'usd' ? String(read.budget.amount) : '', tokens: read.budget?.unit === 'tokens' ? String(read.budget.amount) : '' })
  }, [read])

  const unit = pick === 'off' ? null : pick
  const amount = unit === null ? null : parseBudgetAmount(unit, typed[unit])
  const most = unit === null ? null : held.max[unit]
  const top = unit === null ? null : held.ceiling[unit]
  const overMax = amount !== null && most !== null && amount > most
  const overCeiling = amount !== null && top !== null && amount > top
  const invalid = unit !== null && (amount === null || overMax)
  const was = held.budget
  const unchanged = unit === null ? was === null : was !== null && was.unit === unit && was.amount === amount

  const choose = (next: BudgetPick) => {
    setPick(next)
    setSaved(false)
    setRefused(null)
  }

  async function save(e: FormEvent) {
    e.preventDefault()
    if (busy || invalid || unchanged) return
    setBusy(true)
    setSaved(false)
    setRefused(null)
    const r = await putWorkspaceBudget(held.tenant_id, unit === null || amount === null ? null : { unit, amount })
    setBusy(false)
    if (r.status === 'error') {
      setRefused(r.error)
      return
    }
    if (r.status === 'ok' || r.status === 'stale') {
      setHeld(r.data)
      setSaved(true)
    }
  }

  const refusalWords = refused === null ? null : budgetRefusalWords(refused)
  return (
    <form className="jb-choice" aria-label="Per-agent budget" data-testid="ws-budget" onSubmit={(e) => void save(e)}>
      <dl className="ws-kv">
        <dt>Now</dt>
        <dd data-testid="ws-budget-now">{budgetNow(held.budget)}</dd>
        <dt>Platform ceiling</dt>
        <dd>
          <CeilingLine ceiling={held.ceiling} />
        </dd>
      </dl>
      <div className="jb-options" role="radiogroup" aria-label="Limit">
        {(['off', 'usd', 'tokens'] as const).map((p) => (
          <label key={p} className={`jb-option${pick === p ? ' is-on' : ''}`}>
            <input type="radio" name={`${id}-pick`} value={p} checked={pick === p} disabled={busy} onChange={() => choose(p)} />
            <span className="jb-text">
              <span className="jb-name">
                {p === 'off' ? 'Off' : unitLabel(p)}
                {p === 'off' && <span className="jb-default"> (the default)</span>}
              </span>
              <span className="jb-note">
                {p === 'off'
                  ? 'No limit: an agent runs until its step ends or times out.'
                  : p === 'usd'
                    ? 'The estimated cost at list prices, as each agent’s cost is reported.'
                    : 'Input and output tokens, with cache reads, as the runner reports them.'}
              </span>
            </span>
          </label>
        ))}
      </div>
      {unit !== null && (
        <>
          <label className="c-field" htmlFor={`${id}-amount`}>
            <span className="c-lbl">{unit === 'usd' ? 'Dollars per agent' : 'Tokens per agent'}</span>
            <input
              id={`${id}-amount`}
              className="c-inp mono"
              type="text"
              inputMode={unit === 'usd' ? 'decimal' : 'numeric'}
              autoComplete="off"
              placeholder={unit === 'usd' ? '5.00' : '15,000,000'}
              value={typed[unit]}
              disabled={busy}
              aria-invalid={invalid || overCeiling || undefined}
              aria-describedby={`${id}-amount-hint`}
              onChange={(e) => {
                setSaved(false)
                setRefused(null)
                setTyped((t) => ({ ...t, [unit]: e.target.value }))
              }}
            />
          </label>
          <p id={`${id}-amount-hint`} className="ur-hint">
            {top !== null ? `At most ${budgetAmount(unit, top)}, the platform ceiling.` : `No platform ceiling in ${unit === 'usd' ? 'dollars' : 'tokens'}.`}
          </p>
        </>
      )}
      {unit !== null && typed[unit].trim() !== '' && amount === null && (
        <p className="warn-text">{unit === 'usd' ? 'A dollar budget is a number above 0.' : 'A token budget is a whole number, 1 or more.'}</p>
      )}
      {overMax && most !== null && unit !== null && <p className="warn-text">{`The most the platform takes is ${budgetAmount(unit, most)}.`}</p>}
      {overCeiling && !overMax && top !== null && unit !== null && (
        <p className="warn-text">{`Above the platform ceiling of ${budgetAmount(unit, top)}: saving it will be refused.`}</p>
      )}
      <div className="ws-acts">
        <Button kind="primary" size="sm" type="submit" busy={busy ? 'Saving…' : false} disabled={invalid || unchanged}>
          Save budget
        </Button>
      </div>
      <SetBy read={held} />
      {saved && (
        <p className="pk-saved" role="status">
          {held.budget === null ? 'Saved: the budget is off.' : `Saved: ${budgetNow(held.budget)}. It applies to steps submitted from now on.`}
        </p>
      )}
      {refused !== null &&
        (refusalWords !== null ? (
          <Banner tone="bad" title="The budget was not saved" role="alert">
            <p className="ur-small" data-code={refused.code ?? undefined}>
              {refusalWords}
            </p>
          </Banner>
        ) : (
          <Refusal error={refused} title="The budget was not saved" />
        ))}
    </form>
  )
}

/** The job's steps only a service workspace runs: in its order, not in a group's. */
const SERVICE_ONLY: ReadonlySet<string> = new Set(JOB_STEP_ORDER.service.filter((id) => !JOB_STEP_ORDER.group.includes(id)))

/**
 * Add a new workspace, for a platform admin (S6c): Admin › Tenants' form,
 * drawn on the page, beside the list of what creating it does, which is the
 * workspace job's own steps (NewWorkspace.tsx's labels, in a service's
 * order, the longest). After Create the form gives way to the record's
 * progress, and Add another starts a fresh form.
 */
function AddWorkspace() {
  const [round, setRound] = useState(0)
  const [filed, setFiled] = useState<string | null>(null)
  return (
    <>
      <Lead
        kicker="Platform admins only"
        title="Add a new workspace"
        body="It opens on Config with the setup strip at step 1, for whoever will run it. A personal or group workspace you file is approved at once, because you are a platform admin; a service workspace waits for the platform owner’s approval. Its progress is also on Admin › Tenants."
      />
      <div className="ws-grid is-form">
        <Card
          title="New workspace"
          action={
            filed !== null ? (
              <Button
                size="sm"
                onClick={() => {
                  setFiled(null)
                  setRound((n) => n + 1)
                }}
              >
                Add another
              </Button>
            ) : undefined
          }
        >
          <NewWorkspaceForm key={round} inline onFiled={(r) => setFiled(r.workspace_id ?? r.request_id ?? 'the request')} />
        </Card>
        <Card title="What creating it does">
          <ol className="ws-steps" aria-label="What creating it does">
            {JOB_STEP_ORDER.service.map((id) => (
              <StepRow key={id} step={{ label: JOB_STEP_LABEL[id] ?? id, state: 'todo', line: SERVICE_ONLY.has(id) ? 'a service workspace only' : undefined }} next={false} />
            ))}
          </ol>
          <p className="ur-hint">
            Create posts the kind, one address, an optional display name and, for a group, an optional first admin to{' '}
            <code>POST /v1/admin/workspaces</code>, the
            route Admin › Tenants › New workspace uses. Each step then ticks in the form as the workspace job checks it.
          </p>
          {filed !== null && (
            <p className="ur-hint">
              Filed <code>{filed}</code>. Admin › Tenants lists it under Being created until it is ready.
            </p>
          )}
        </Card>
      </div>
    </>
  )
}

export function WorkspaceSettingsScreen({ part = 'settings', go }: { part?: WorkspacePart; go?: (to: string) => void }) {
  const profile = useUrRead(loadProfile, 'profile')
  const roles = useUrRead(loadWorkspaceRoles, 'workspace-roles')
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
        <UrRefresh reads={part === 'settings' ? [profile, roles] : [profile]} />
      </PageHead>
      <Tabs className="cfg-parts" label="Workspace Settings" current={part} onGo={go} tabs={tabs} />
      {part === 'settings' && (
        <>
          <SetupLead part="settings" go={go} />
          <div className="ws-grid">
            <UrRegion state={profile.state} route="GET /v1/profile" what="This workspace" onRetry={profile.reload}>
              {(p) => <ThisWorkspace profile={p} roles={roles} go={go} />}
            </UrRegion>
            <CodeIntelligenceCard />
            <QaSettingsCard />
            <WorkspaceBudgetCard />
          </div>
        </>
      )}
      {part === 'setup' && (
        <>
          <SetupLead part="setup" go={go} />
          <div className="ws-grid is-setup">
            <OnboardingScreen />
            <div className="ws-col">
              <AfterChecklist go={go} />
              <OnOverview />
            </div>
          </div>
          {/* Setup › GitHub (#1086, design 2 "Org cards"): what the App and each org grant, under the grid
              and across both its columns (visual QA pass 1, config-12): inside the checklist's two-thirds
              column it left the right-hand column empty for the GitHub section's whole height. */}
          <GitHubPermissions />
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
