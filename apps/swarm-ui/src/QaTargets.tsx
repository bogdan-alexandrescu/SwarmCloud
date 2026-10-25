import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { loadRepositories } from './api'
import { Banner, Button, Card, EmptyState } from './components'
import { repoName } from './RepositoriesData'
import { UrLink, UrRefresh, UrRegion, useUrRead, writeFailure } from './RepositoriesParts'
import { useIsAdmin } from './PermissionCards'
import { useRoleGate } from './Profile'
import { PageHead } from './Shell'
import { timeAgo } from './types'
import {
  approveQaTargetHost,
  cannotBrowseBecause,
  cannotRunBecause,
  createQaTarget,
  deleteQaTarget,
  discoverQaTarget,
  loadQaTargets,
  MORE_FIELDS,
  mintQaCredentialLink,
  normQaTarget,
  patchQaTarget,
  putQaChallengeBypass,
  putQaCredential,
  qaCredentialLinkFrom,
  legsOf,
  linkSpent,
  promoteQaScreen,
  repoSlug,
  runQaTarget,
  type FlowBrowserTool,
  type QaCredentialStatus,
  type QaEnvironment,
  type QaLink,
  type QaRunGroupStart,
  type QaScreen,
  type QaTarget,
  type QaTargetSend,
} from './qaTargetsApi'
import { qaRunAddress } from './paths'
import './styles/repositories.css'

/**
 * WORK › REPOSITORIES › QA TARGETS (lane VQ-6 CONSOLE-PLUGIN, part of #1933;
 * docs/design/visual-qa-any-app.md §1, §2.1, §6).
 *
 * A QA target is a web application a workspace's visual-QA runs test, its
 * findings filed in one of the workspace's registered repositories. This
 * page lists them, registers and changes them (§1), sets an own-login
 * target's test credential (§2.1), and starts a run (§6).
 *
 * WHO SEES WHAT (§1). A workspace ADMIN registers, edits, deletes and sets
 * the credential; a MEMBER runs, and sees Run and not Edit. The server
 * decides either way (`require_workspace_admin`, `require_member`): an
 * unknown role disables nothing here.
 *
 * A HOST IS PENDING UNTIL A PLATFORM ADMIN APPROVES IT (§1.2, VQ-Q1), and a
 * new or changed host is pending again. Run is disabled on a target that
 * cannot run now, with the reason beside it, so nobody presses a button the
 * API is certain to refuse; the API is still the gate.
 *
 * APPROVE HOST (lane VQ6-APPROVE, §1.2) is drawn on a target whose host is
 * not approved, to a PLATFORM admin only: `principal.is_admin`, read by
 * `useIsAdmin` exactly as the permission cards read it, whatever the
 * caller's workspace role. The admin types the host, and the login's extra
 * origin when the target declares one, and nothing is sent until both match;
 * the API's `admin_auth` is still the gate and its refusal is said in its own
 * words. The approved target the API answers is drawn at once, until a read
 * of a later revision replaces it.
 *
 * DISCOVERY (lane QA-DISCOVER, §4.1; BGD 2026-10-23: "build discovery, so a
 * crawl target is started entirely from the console"). A target with
 * `discover` shows Run discovery (Run discovery again once it has discovered
 * screens) to any member, as Run is. While one runs the card says
 * Discovering… and links its workflow, and the list is read again every
 * `DISCOVERY_POLL_MS` until it ends; then it says how many screens were
 * discovered and when, or the API's `discovery_error` sentence. Run is
 * enabled once screens exist (`cannotRunBecause`).
 *
 * THE CREDENTIAL IS NEVER HELD BY THIS PAGE, exactly as ProviderKeys.tsx
 * holds a key: the two fields are uncontrolled, their values live in the DOM
 * input and nowhere else, submit reads them once, empties the fields and
 * hands them to `putQaCredential`, which puts them in that one request's
 * body. The API answers status only.
 *
 * THE ONE-TIME LINK (`/qa-targets/<id>/credential?t=..`, minted by the
 * plugin's `swarm_qa_target_credential_link` or by Set credential here). On
 * the link's path the token is read off the bar once, dropped from it with
 * `history.replaceState`, and sent back as `link_token` with the save. Set
 * credential mints the same link for the signed-in admin and keeps its token
 * in memory, never drawn. The link's `environment` (the entry it was minted
 * for, on a target of several) is sent back with it and named in the form's
 * title (#2280). The token is dropped once an answer says the API consumed
 * it (`linkSpent`); a refusal made before the link was checked keeps it, so
 * the person fixes the field and saves again with the same link.
 *
 * A `field: "challenge_bypass"` link (`/qa-targets/<id>/challenge-bypass?t=..`)
 * opens the same form with the bypass value's one field, and is sent and
 * kept by the same rules.
 *
 * A CHALLENGE BYPASS (lane CAPTCHA-BYPASS, part of #2058; §2.5) is the app
 * owner's own way past their CAPTCHA or WAF challenge: a header or a cookie
 * the live gate adds on the target's approved origins alone. The card says
 * its kind and name and whether its value is set, when and by whom. The
 * target form edits the kind and name, and its value field is write-only as
 * the credential's are: uncontrolled, read once on save and emptied, and sent
 * only in the PUT to `/challenge-bypass` once the target is saved. No link:
 * the admin is signed in here, and the value is a key the owner made for
 * these runs rather than a person's own password.
 *
 * THE FLOW BROWSER (lane VQ9-TOOL; BGD, 2026-10-23: "flows default to
 * Playwright; a target, or one run, may choose Jev", after batch
 * bench3-2026-10-23, docs/browser-bench.md). The form's Flow browser is the
 * target's `flows_browser_tool`, Playwright unless Jev is chosen, sent on a
 * registration and on an edit that changes it, and never dropped by an edit
 * of anything else: an edit sends only what changed, and the API keeps the
 * rest. Beside Run, "Flows with" picks the tool for that one run alone
 * (`browser_tool`); left on the target's, nothing is sent and the target's
 * applies. Capture stays the fixed capture tool either way.
 */

/** This page's address under Repositories (`RepositoriesScreen` mounts it on `page`). */
export const QA_TARGETS_PAGE = 'work/repositories?page=qa-targets'

const LIST_ROUTE = 'GET /v1/qa-targets'

/** How often the list is read again while a discovery runs: a crawl of 60
 *  pages takes minutes, so this is a few reads a discovery, not a poll storm. */
const DISCOVERY_POLL_MS = 20_000

/** Drop `t` from the address bar, keeping everything else. Idempotent. */
function stripLinkToken(): void {
  const url = new URL(window.location.href)
  if (!url.searchParams.has('t')) return
  url.searchParams.delete('t')
  window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`)
}

/** The link the page was opened at, read once off the bar, or null. */
function linkFromLocation(): QaLink | null {
  if (typeof window === 'undefined') return null
  return qaCredentialLinkFrom(window.location.pathname, window.location.search)
}

export function QaTargetsScreen({
  go,
  link,
  tabs,
}: {
  go: (to: string) => void
  link?: QaLink | null
  /** Config › Repositories' Repositories | QA targets strip, drawn under the head as the list draws it (REPOS-PAGE). */
  tabs?: ReactNode
}) {
  const list = useUrRead(loadQaTargets, 'qa-targets')
  // ONLY A WORKSPACE ADMIN REGISTERS, CHANGES OR SETS A CREDENTIAL (§1); a
  // member runs. A viewer does neither. The server decides.
  const adminGate = useRoleGate('admin', 'register, change or set the credential of a QA target')
  const runGate = useRoleGate('member', 'run a QA target')
  // A PLATFORM admin approves a host (VQ-Q1): not a workspace role. Unread is no control.
  const platformAdmin = useIsAdmin() === true
  // Targets the approve route answered, by id, drawn over the list's row of the same revision.
  const [approved, setApproved] = useState<Record<string, QaTarget>>({})
  // The credential form's target and its link token, read once; the token is
  // null once it has been sent.
  const [cred, setCred] = useState<QaLink | null>(() => (link !== undefined ? link : linkFromLocation()))
  const opened = useRef(cred !== null)
  useEffect(() => {
    if (opened.current) stripLinkToken()
  }, [])
  const [editing, setEditing] = useState<'new' | QaTarget | null>(null)
  const [said, setSaid] = useState<{ tone: 'info' | 'bad'; title: string; text: string } | null>(null)
  const targets = list.state.status === 'ok' || list.state.status === 'stale' ? list.state.data : []
  const nameOf = (id: string) => targets.find((t) => t.id === id)?.name ?? id
  // While a discovery runs, read the list again until it ends: the card's
  // Discovering… becomes the count or the error without a manual refresh.
  const discovering = targets.some((t) => legsOf(t).some((leg) => leg.discovering !== null))
  const reload = list.reload
  useEffect(() => {
    if (!discovering) return
    const timer = window.setInterval(reload, DISCOVERY_POLL_MS)
    return () => window.clearInterval(timer)
  }, [discovering, reload])

  // The link is bound to the leg it sets (§1.4): a link for staging never sets production's password.
  async function openCredential(t: QaTarget, leg: QaEnvironment) {
    setSaid(null)
    const r = await mintQaCredentialLink(t.id, leg.name)
    if (r.status === 'error') {
      setSaid({ tone: 'bad', title: `No credential link for ${legLabel(t, leg)}`, text: writeFailure(r.error) })
      return
    }
    if (r.status === 'ok') setCred({ targetId: t.id, token: r.data.token, environment: leg.name, field: 'credential' })
  }

  return (
    <div className="ur-page ur-list qa-page">
      <PageHead title="QA targets">
        <span className="ur-acts">
          <UrRefresh reads={[list]} />
          {adminGate === null && (
            <Button kind="primary" size="sm" onClick={() => setEditing('new')}>
              Register QA target
            </Button>
          )}
        </span>
      </PageHead>
      {tabs}
      <p className="ur-small">
        Web applications this workspace&rsquo;s visual-QA runs test, each filing its findings in one of the workspace&rsquo;s
        registered repositories. A new or changed host waits for a platform admin&rsquo;s approval before any run.
      </p>
      {adminGate !== null && (
        <p className="ur-small" data-role-gate="admin">
          {adminGate}
        </p>
      )}
      <QaLead targets={targets} admin={adminGate === null} onCredential={(t, leg) => void openCredential(t, leg)} />
      {said !== null && (
        <Banner tone={said.tone} title={said.title}>
          {said.text}
        </Banner>
      )}
      {cred !== null && (
        <CredentialForm
          link={cred}
          name={nameOf(cred.targetId)}
          onSent={() => setCred((c) => (c === null ? c : { ...c, token: null }))}
          onSaved={() => {
            const what = cred.field === 'challenge_bypass' ? 'challenge bypass value' : 'test credential'
            const where = cred.environment === null ? nameOf(cred.targetId) : `${nameOf(cred.targetId)} · ${cred.environment}`
            setSaid({
              tone: 'info',
              title: cred.field === 'challenge_bypass' ? 'Challenge bypass value saved' : 'Credential saved',
              text: `The ${what} for ${where} is set. No page, route or log can show it again.`,
            })
            setCred(null)
            list.reload()
          }}
          onClose={() => setCred(null)}
        />
      )}
      {editing !== null && (
        <TargetForm
          target={editing === 'new' ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={(doc, created) => {
            setEditing(null)
            const pending = doc?.host_state === 'pending'
            const name = doc?.name ?? 'The target'
            setSaid({
              tone: 'info',
              title: created ? `Registered ${name}` : `Changed ${name}`,
              text: pending ? `Its host ${doc?.host ?? ''} waits for a platform admin's approval before any run.` : 'Saved.',
            })
            list.reload()
          }}
        />
      )}
      <UrRegion
        state={list.state}
        route={LIST_ROUTE}
        what="The workspace's QA targets"
        plural
        onRetry={list.reload}
        empty={
          <EmptyState kind="empty" heading="No QA targets registered">
            A workspace admin registers a web application here: its address, the repository its findings are filed in, and
            the screens to capture.
          </EmptyState>
        }
      >
        {(rows) => (
          <div className="ur-cards">
            {rows.map((row) => {
              const fresh = approved[row.id]
              const t = fresh !== undefined && fresh.revision === row.revision ? fresh : row
              return (
              <TargetCard
                key={t.id}
                t={t}
                go={go}
                admin={adminGate === null}
                platformAdmin={platformAdmin}
                runGate={runGate}
                onApproved={(doc) => setApproved((was) => ({ ...was, [doc.id]: doc }))}
                onEdit={() => setEditing(t)}
                onChanged={list.reload}
                onCredential={(leg) => void openCredential(t, leg)}
                onDeleted={() => {
                  setSaid({ tone: 'info', title: `Deleted ${t.name}`, text: 'The target and its test credential are gone.' })
                  list.reload()
                }}
              />
              )
            })}
          </div>
        )}
      </UrRegion>
    </div>
  )
}

function toolName(tool: FlowBrowserTool): string {
  return tool === 'jev' ? 'Jev' : 'Playwright'
}

/** The host's state, its one word (S9a draws the column that short). */
function hostWords(leg: QaEnvironment): string {
  return leg.host_state ?? 'unknown'
}

/** `by ops@… 3d ago` under an approved host, or null. */
function approvedWhen(leg: QaEnvironment): string | null {
  if (leg.host_state !== 'approved' || (leg.approved_by === null && leg.approved_at === null)) return null
  return [leg.approved_by === null ? null : `by ${leg.approved_by}`, leg.approved_at === null ? null : timeAgo(leg.approved_at)].filter(Boolean).join(' ')
}

/** `own login · test credential set`, `not set`, or `none needed`: never a value. */
function credentialWords(leg: QaEnvironment): string {
  if (leg.sign_in.kind !== 'own_login') return 'none needed'
  return leg.credential.set ? 'own login · test credential set' : 'not set'
}

/** `30h ago by admin@…` under a set credential, or null. */
function credentialWhen(leg: QaEnvironment): string | null {
  const c = leg.credential
  if (leg.sign_in.kind !== 'own_login' || !c.set || (c.set_at === null && c.set_by === null)) return null
  return [c.set_at === null ? null : timeAgo(c.set_at), c.set_by === null ? null : `by ${c.set_by}`].filter(Boolean).join(' ')
}

/**
 * `Start clicks: Staging → Sign in`, each control's text exactly as stored and
 * in the order the gate clicks them (§2.2); null when the sign-in has none, so
 * a leg says so only when the gate clicks something before the form.
 */
function startClickWords(leg: QaEnvironment): string | null {
  const clicks = leg.sign_in.start_clicks
  if (clicks.length === 0) return null
  return `Start clicks: ${clicks.map((c) => c.text).join(' → ')}`
}

/** `Challenge bypass · header X-QA-Bypass · value set 2h ago by …`: never the value. */
function bypassWords(leg: QaEnvironment): string | null {
  const b = leg.challenge_bypass
  if (b === null) return null
  return `Challenge bypass · ${b.kind} ${b.name} · ${valueWords(leg.challenge_bypass_value)}`
}

function valueWords(v: QaCredentialStatus): string {
  if (!v.set) return 'value not set'
  const parts = ['value set']
  if (v.set_at !== null) parts.push(timeAgo(v.set_at))
  if (v.set_by !== null) parts.push(`by ${v.set_by}`)
  return parts.join(' ')
}

/** A leg's name as a sentence says it: its entry name, or the target's for the one-origin shape. */
function legLabel(t: QaTarget, leg: QaEnvironment): string {
  return leg.name === null ? t.name : `${t.name} ${leg.name}`
}

function kindWords(leg: QaEnvironment): string {
  return leg.environment === 'non_production' ? 'Non-production' : 'Production'
}

/** What the page's lead says about one leg that cannot run (Config proposal E: led by what is missing). */
export interface QaNeed {
  t: QaTarget
  leg: QaEnvironment
  title: string
  body: string
  /** Its next action is a workspace admin's Set credential. */
  credential: boolean
}

/**
 * Every leg of every target that cannot run now, in the cards' order, with
 * the sentence the lead says for it. The reason is `cannotRunBecause`'s, the
 * one the leg's own row says.
 */
export function needsOf(targets: QaTarget[]): QaNeed[] {
  const out: QaNeed[] = []
  for (const t of targets) {
    const legs = legsOf(t)
    for (const leg of legs) {
      const why = cannotRunBecause(t, leg)
      if (why === null) continue
      const who = legLabel(t, leg)
      const others = legs.filter((l) => l !== leg && cannotRunBecause(t, l) === null).map((l) => l.name ?? t.name)
      const rest = others.length === 0 ? '' : ` ${others.join(', ').replace(/^./, (c) => c.toUpperCase())} ${others.length === 1 ? 'runs' : 'run'} now.`
      const credential = cannotBrowseBecause(t, leg) !== null && leg.host_state === 'approved' && leg.sign_in.kind === 'own_login' && !leg.credential.set
      const title =
        credential ? `${who} has no test credential`
        : leg.host_state === 'pending' ? `${who} waits for a platform admin to approve its host`
        : leg.host_state === 'revoked' ? `${who}’s host approval was revoked`
        : `${who} cannot run`
      const body = credential ? `Its leg cannot run until a workspace admin sets it.${rest}` : `${why}${rest}`
      out.push({ t, leg, title, body, credential })
    }
  }
  return out
}

/** The page's one next-action sentence: the first leg that cannot run, or that every leg can. */
function QaLead({ targets, admin, onCredential }: { targets: QaTarget[]; admin: boolean; onCredential: (t: QaTarget, leg: QaEnvironment) => void }) {
  if (targets.length === 0) return null
  const needs = needsOf(targets)
  const first = needs[0]
  if (first === undefined) {
    const n = targets.reduce((sum, t) => sum + legsOf(t).length, 0)
    return (
      <section className="qa-lead is-ok" data-qa-lead="" aria-label="QA targets">
        <div className="qa-lead-t">
          <span className="qa-lead-k">QA targets · nothing missing</span>
          <b>{n === 1 ? 'The one environment can run' : `Every environment can run: ${n}`}</b>
          <span className="qa-lead-s">Tick the environments on a target and press Run.</span>
        </div>
      </section>
    )
  }
  const whose = first.leg.name === null ? 'its' : `${first.leg.name}’s`
  return (
    <section className="qa-lead" data-qa-lead="" aria-label="QA targets">
      <div className="qa-lead-t">
        <span className="qa-lead-k">{`QA targets · ${needs.length} missing`}</span>
        <b>{first.title}</b>
        <span className="qa-lead-s">{first.body}</span>
      </div>
      {first.credential && admin && (
        <div className="qa-lead-a">
          <Button kind="primary" size="sm" onClick={() => onCredential(first.t, first.leg)}>
            {first.leg.name === null ? 'Set its credential' : `Set ${whose} credential`}
          </Button>
        </div>
      )}
    </section>
  )
}

function TargetCard({
  t,
  go,
  admin,
  platformAdmin,
  runGate,
  onApproved,
  onEdit,
  onChanged,
  onCredential,
  onDeleted,
}: {
  t: QaTarget
  go: (to: string) => void
  /** The caller is a workspace admin: Edit, Delete, Set credential and Promote are drawn. */
  admin: boolean
  /** The session is a platform admin (`principal.is_admin`): Approve host is drawn. */
  platformAdmin: boolean
  /** The target the approve route answered. */
  onApproved: (doc: QaTarget) => void
  /** The caller's role is below `member`: Run is disabled and this says why. */
  runGate: string | null
  onEdit: () => void
  /** A discovery was started or a route promoted: read the list again. */
  onChanged: () => void
  onCredential: (leg: QaEnvironment) => void
  onDeleted: () => void
}) {
  const legs = legsOf(t)
  const named = t.environments.length > 0
  const keyOf = (leg: QaEnvironment) => leg.name ?? ''
  // The legs NOT ticked, by name: every runnable leg starts ticked (S9a), and
  // a leg that becomes runnable on a later read is ticked without a click.
  const [unticked, setUnticked] = useState<ReadonlySet<string>>(() => new Set())
  const runnable = legs.filter((leg) => cannotRunBecause(t, leg) === null)
  const ticked = runnable.filter((leg) => !unticked.has(keyOf(leg)))
  const [reportOnly, setReportOnly] = useState(false)
  /** This one run's flow browser; null runs the target's own. */
  const [runTool, setRunTool] = useState<FlowBrowserTool | null>(null)
  const [busy, setBusy] = useState(false)
  const [started, setStarted] = useState<QaRunGroupStart | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  /** The leg whose host the platform admin is approving (its key), or null. */
  const [approving, setApproving] = useState<string | null>(null)
  const [discoveryBusy, setDiscoveryBusy] = useState(false)
  const [promoting, setPromoting] = useState<string | null>(null)
  // An approved leg with a login's extra origin can be approved again, to set or clear its IdP token path (QA-IDP-POST).
  const canApprove = (leg: QaEnvironment) => platformAdmin && leg.host !== null && (leg.host_state !== 'approved' || leg.extra_origin !== null)
  // Discovery browses the ticked legs that may be browsed, one `:discover` each.
  const discoverable = legs.filter((leg) => !unticked.has(keyOf(leg)) && cannotBrowseBecause(t, leg) === null && leg.discovering === null)
  const anyDiscovered = legs.some((leg) => leg.discovered.length > 0)
  const why = legs.length === 1 ? cannotRunBecause(t, legs[0]!) : null

  async function discover() {
    if (runGate !== null || discoverable.length === 0) return
    setDiscoveryBusy(true)
    setFailed(null)
    const said: string[] = []
    for (const leg of discoverable) {
      const r = await discoverQaTarget(t.id, leg.name)
      if (r.status === 'error') said.push(leg.name === null ? writeFailure(r.error) : `${leg.name}: ${writeFailure(r.error)}`)
    }
    setDiscoveryBusy(false)
    if (said.length > 0) setFailed(said.join(' '))
    onChanged()
  }

  async function run() {
    if (ticked.length === 0 || runGate !== null) return
    setBusy(true)
    setFailed(null)
    setStarted(null)
    // Names only (invariant 10). The one-origin shape names none: its only entry runs.
    const names = named ? ticked.flatMap((leg) => (leg.name === null ? [] : [leg.name])) : null
    const r = await runQaTarget(t.id, reportOnly ? 'none' : 'issues', runTool, names)
    setBusy(false)
    if (r.status === 'error') setFailed(writeFailure(r.error))
    else if (r.status === 'ok') setStarted(r.data)
  }

  async function promote(found: QaScreen) {
    setPromoting(found.id)
    setFailed(null)
    const r = await promoteQaScreen(t, found)
    setPromoting(null)
    if (r.status === 'error') setFailed(writeFailure(r.error))
    else onChanged()
  }

  async function remove() {
    setBusy(true)
    setFailed(null)
    const r = await deleteQaTarget(t.id)
    setBusy(false)
    setConfirmDelete(false)
    if (r.status === 'error') setFailed(writeFailure(r.error))
    else onDeleted()
  }

  const slug = repoSlug(t.repository)
  const screensWords = t.screens.length === 0 ? (t.discovers ? 'Discovers its screens' : 'No screens') : `${t.screens.length} screen${t.screens.length === 1 ? '' : 's'}`
  const runLabel = named ? `Run ${ticked.length} environment${ticked.length === 1 ? '' : 's'}` : 'Run'
  const approvingLeg = approving === null ? null : (legs.find((leg) => keyOf(leg) === approving) ?? null)
  return (
    <div className="qa-target" data-target-id={t.id} data-host-state={t.host_state ?? undefined}>
    <Card
      className="ur-card"
      title={t.name}
      action={
        admin ? (
          <span className="ur-cacts qa-head-acts">
            <Button size="sm" onClick={onEdit}>
              Edit
            </Button>
          </span>
        ) : undefined
      }
    >
      <div className="ur-cmeta">
        <span>
          Files in <b>{slug ?? t.repository ?? t.repo_id ?? 'an unnamed repository'}</b>
        </span>
        <span>{screensWords}</span>
      </div>
      <div className="qa-envs-wrap">
        <table className="qa-envs" aria-label={`${t.name}’s environments`}>
          <thead>
            <tr>
              <th scope="col" className="qa-env-tick" aria-label="Run" />
              <th scope="col">Environment</th>
              <th scope="col">Host</th>
              <th scope="col">Host approval</th>
              <th scope="col">Credential</th>
              <th scope="col">Screens</th>
            </tr>
          </thead>
          {legs.map((leg) => {
            const key = keyOf(leg)
            const legWhy = cannotRunBecause(t, leg)
            const label = leg.name ?? kindWords(leg)
            const clicks = startClickWords(leg)
            const bypass = bypassWords(leg)
            return (
              <tbody key={key} className={legWhy === null ? 'qa-env' : 'qa-env is-off'} data-environment={leg.name ?? undefined}>
                <tr>
                  <td className="qa-env-tick">
                    <input
                      type="checkbox"
                      data-run-leg=""
                      aria-label={`Run ${label}`}
                      checked={legWhy === null && !unticked.has(key)}
                      disabled={legWhy !== null}
                      onChange={(e) => {
                        const on = e.target.checked
                        setUnticked((was) => {
                          const next = new Set(was)
                          if (on) next.delete(key)
                          else next.add(key)
                          return next
                        })
                      }}
                    />
                  </td>
                  <td data-label="Environment">
                    <b>{label}</b>
                    {leg.name !== null && <small>{kindWords(leg)}</small>}
                    {legWhy !== null && (
                      <small className="ur-bad" data-cannot-run="">
                        Cannot run: {legWhy}
                      </small>
                    )}
                    {clicks !== null && <small data-start-clicks="">{clicks}</small>}
                    {bypass !== null && <small data-challenge-bypass="">{bypass}</small>}
                  </td>
                  <td data-label="Host" className="qa-env-host">
                    {leg.base_url ?? leg.host ?? '—'}
                  </td>
                  <td data-label="Host approval">
                    <span className={leg.host_state === 'approved' ? 'qa-ok' : 'ur-bad'}>{hostWords(leg)}</span>
                    {approvedWhen(leg) !== null && <small>{` ${approvedWhen(leg)}`}</small>}
                    {canApprove(leg) && approving !== key && (
                      <Button size="sm" onClick={() => setApproving(key)}>
                        {leg.host_state === 'approved' ? 'Approve again' : 'Approve host'}
                      </Button>
                    )}
                  </td>
                  <td data-label="Credential">
                    {leg.sign_in.kind === 'own_login' && !leg.credential.set ? (
                      <span className="qa-chip is-warn">not set</span>
                    ) : (
                      <span className={leg.sign_in.kind === 'own_login' ? 'qa-ok' : undefined}>{credentialWords(leg)}</span>
                    )}
                    {credentialWhen(leg) !== null && <small>{` ${credentialWhen(leg)}`}</small>}
                    {admin && leg.sign_in.kind === 'own_login' && (
                      <Button size="sm" onClick={() => onCredential(leg)}>
                        Set credential
                      </Button>
                    )}
                  </td>
                  <td data-label="Screens">
                    <LegScreens t={t} leg={leg} go={go} />
                  </td>
                </tr>
                {leg.new_since_last_run.length > 0 && (
                  <tr className="qa-env-new">
                    <td className="qa-env-tick" aria-hidden="true" />
                    <td colSpan={5} data-label="New since last run">
                      <span className="qa-new-k">New since last run</span>
                      <ul className="qa-new">
                        {leg.new_since_last_run.map((found) => (
                          <li key={found.id}>
                            <code>{found.path}</code>
                            {found.first_seen !== null && <span className="qa-new-at">{`first seen ${timeAgo(found.first_seen)}`}</span>}
                            {admin && (
                              <Button size="sm" busy={promoting === found.id} disabled={promoting !== null} onClick={() => void promote(found)}>
                                {`Promote ${found.path}`}
                              </Button>
                            )}
                          </li>
                        ))}
                      </ul>
                    </td>
                  </tr>
                )}
              </tbody>
            )
          })}
        </table>
      </div>
      {runGate !== null && why === null && (
        <p className="ur-small" data-role-gate="member">
          {runGate}
        </p>
      )}
      <div className="ur-cacts">
        <Button size="sm" kind="primary" busy={busy} disabled={ticked.length === 0 || runGate !== null} onClick={() => void run()}>
          {runLabel}
        </Button>
        <label className="ur-small">
          <input type="checkbox" checked={reportOnly} onChange={(e) => setReportOnly(e.target.checked)} data-report-only="" /> report
          only, file no issues
        </label>
        <label className="ur-small">
          Flows with{' '}
          <select
            data-run-browser=""
            value={runTool ?? ''}
            onChange={(e) => setRunTool(e.target.value === 'playwright' || e.target.value === 'jev' ? e.target.value : null)}
          >
            <option value="">the target&rsquo;s ({toolName(t.flows_browser_tool)})</option>
            <option value="playwright">Playwright, this run</option>
            <option value="jev">Jev, this run</option>
          </select>
        </label>
        {t.discovers && (
          <Button size="sm" busy={discoveryBusy} disabled={runGate !== null || discoverable.length === 0} onClick={() => void discover()}>
            {anyDiscovered ? 'Run discovery again' : 'Run discovery'}
          </Button>
        )}
        {admin && (
          <Button size="sm" kind="danger" disabled={busy} onClick={() => setConfirmDelete(true)}>
            Delete
          </Button>
        )}
      </div>
      {approvingLeg !== null && canApprove(approvingLeg) && (
        <ApproveHostForm
          t={t}
          leg={approvingLeg}
          onClose={() => setApproving(null)}
          onApproved={(doc) => {
            setApproving(null)
            onApproved(doc)
          }}
        />
      )}
      {confirmDelete && (
        <div className="ur-cacts" role="group" aria-label={`Delete ${t.name}`}>
          <span className="ur-small">{`Delete ${t.name}? Its test credential is deleted with it.`}</span>
          <Button size="sm" kind="danger-filled" busy={busy} onClick={() => void remove()}>
            Delete {t.name}
          </Button>
          <Button size="sm" onClick={() => setConfirmDelete(false)}>
            Cancel
          </Button>
        </div>
      )}
      {started !== null && <StartedLine started={started} go={go} />}
      {failed !== null && (
        <p className="ur-small ur-bad" role="alert">
          {failed}
        </p>
      )}
    </Card>
    </div>
  )
}

/**
 * What a Run started: the run group's page (`/qa-runs/<group_id>`, one
 * column per leg, QAENV-UI-RUNPAGE), and each leg the API could not start
 * with its refusal's sentence (a 207). An answer with no group links each
 * leg's workflow.
 */
function StartedLine({ started, go }: { started: QaRunGroupStart; go: (to: string) => void }) {
  const submitted = started.legs.filter((l) => l.refused === null && l.workflow_id !== null)
  const refused = started.legs.filter((l) => l.refused !== null)
  const workflows = submitted.length > 0 ? submitted.map((l) => l.workflow_id!) : started.workflow_id === null ? [] : [started.workflow_id]
  const names = submitted.flatMap((l) => (l.environment === null ? [] : [l.environment]))
  return (
    <>
      <p className="ur-small" role="status">
        {`Started${names.length === 0 ? '' : ` ${names.join(' and ')}`}`}
        {started.group_id !== null ? (
          <>
            {' · '}
            <UrLink to={qaRunAddress(started.group_id)} go={go}>
              open the run
            </UrLink>
          </>
        ) : null}
        {workflows.map((wf) => (
          <span key={wf}>
            {' · '}
            <UrLink to={`work/workflows?wf=${encodeURIComponent(wf)}`} go={go}>
              {wf}
            </UrLink>
          </span>
        ))}
      </p>
      {refused.map((l) => (
        <p key={l.environment ?? ''} className="ur-small ur-bad" role="alert">
          {`${l.environment ?? 'The run'} did not start: ${l.refused}`}
        </p>
      ))}
    </>
  )
}

/** A leg's Screens cell: the stored count, or its discovery's state (§4.1): running, found, or failed. */
function LegScreens({ t, leg, go }: { t: QaTarget; leg: QaEnvironment; go: (to: string) => void }) {
  const running = leg.discovering
  const n = leg.discovered.length
  const stored = t.screens.length > 0 ? <span>{t.screens.length}</span> : null
  if (!t.discovers) return stored ?? <span>none</span>
  return (
    <>
      {stored}
      <span className="qa-env-disc" data-discovery="">
        {running !== null ? (
          <span role="status">
            Discovering…
            {running.workflow_id !== null && (
              <>
                {' '}
                <UrLink to={`work/workflows?wf=${encodeURIComponent(running.workflow_id)}`} go={go}>
                  {running.workflow_id}
                </UrLink>
              </>
            )}
          </span>
        ) : n > 0 ? (
          <span>
            {`${n} screen${n === 1 ? '' : 's'} discovered`}
            {leg.discovered_at !== null && `, ${timeAgo(leg.discovered_at)}`}
          </span>
        ) : (
          <span>{stored === null ? 'Discovers its screens' : 'No screens discovered yet'}</span>
        )}
      </span>
      {running === null && leg.discovery_error !== null && (
        <small className="ur-bad" data-discovery-error="">
          The last discovery stored nothing: {leg.discovery_error}
        </small>
      )}
    </>
  )
}

/** A typed host as the API compares it (`approve_host`): trimmed, lower case, no trailing dot. */
function typedHost(text: string): string {
  return text.trim().toLowerCase().replace(/\.+$/, '')
}

/**
 * A platform admin's confirmation of the host, and of the login's extra
 * origin when one is declared (§1.2). Nothing is sent until what is typed is
 * the target's own; the API checks the same again and says its refusal.
 * With an extra origin, the admin may name its IdP token path: the one path
 * there the gate lets a POST through to after its sign-in (QA-IDP-POST, BGD
 * 2026-10-23, #2067). It starts as the approved one, and an approval sent
 * without it clears it.
 */
function ApproveHostForm({
  t,
  leg,
  onClose,
  onApproved,
}: {
  t: QaTarget
  /** The leg whose host this is (§1.3): named to the API on a target of entries. */
  leg: QaEnvironment
  onClose: () => void
  onApproved: (doc: QaTarget) => void
}) {
  const host = leg.host ?? ''
  const origin = leg.extra_origin
  const [typed, setTyped] = useState('')
  const [typedOrigin, setTypedOrigin] = useState('')
  const [tokenPath, setTokenPath] = useState(leg.approved_idp_token_path ?? '')
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  const fid = useId()
  const matches = host !== '' && typedHost(typed) === host && (origin === null || typedOrigin.trim() === origin)

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (!matches || busy) return
    setBusy(true)
    setFailed(null)
    const path = tokenPath.trim()
    const r = await approveQaTargetHost(t.id, host, origin, path === '' ? null : path, leg.name)
    setBusy(false)
    if (r.status === 'error') {
      setFailed(writeFailure(r.error))
      return
    }
    if (r.status === 'ok' && r.data !== null) onApproved(r.data)
    else setFailed('The approval was answered without the target; refresh to read its host.')
  }

  return (
    <form className="qa-approve" data-target-id={t.id} data-environment-approve={leg.name ?? undefined} onSubmit={(e) => void submit(e)} autoComplete="off">
      <p className="ur-small">
        {`Approving ${host} lets this workspace's visual-QA runs open it. The approval is recorded under your name.`}
        {origin !== null && ` The login also signs in through ${origin}, approved with it.`}
      </p>
      <div className="ur-fgrid">
        <label className="ur-fl" htmlFor={`${fid}-host`}>
          Type the host to confirm
        </label>
        <div>
          <input
            id={`${fid}-host`}
            name="confirm_host"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            placeholder={host}
            spellCheck={false}
          />
        </div>
        {origin !== null && (
          <>
            <label className="ur-fl" htmlFor={`${fid}-origin`}>
              Type the login&rsquo;s extra origin
            </label>
            <div>
              <input
                id={`${fid}-origin`}
                name="confirm_extra_origin"
                value={typedOrigin}
                onChange={(e) => setTypedOrigin(e.target.value)}
                placeholder={origin}
                spellCheck={false}
              />
            </div>
            <label className="ur-fl" htmlFor={`${fid}-token`}>
              IdP token path (optional)
            </label>
            <div>
              <input
                id={`${fid}-token`}
                name="idp_token_path"
                value={tokenPath}
                onChange={(e) => setTokenPath(e.target.value)}
                placeholder="/realms/<realm>/protocol/openid-connect/token"
                spellCheck={false}
              />
              <p className="ur-small">
                {`The gate lets exactly a POST to ${origin} at this path through, after its own sign-in, so the app can refresh its token. Every other write there stays refused.`}
              </p>
            </div>
          </>
        )}
      </div>
      <div className="ur-formacts">
        <Button type="submit" kind="primary" size="sm" busy={busy} disabled={!matches}>
          {`Approve ${host}`}
        </Button>
        <Button size="sm" onClick={onClose}>
          Cancel
        </Button>
      </div>
      {failed !== null && (
        <p className="ur-small ur-bad" role="alert">
          {failed}
        </p>
      )}
    </form>
  )
}

/**
 * The one-time link's form: the test credential's username and password, or
 * a `challenge_bypass` link's value. The fields are UNCONTROLLED: their values
 * live in the DOM inputs and nowhere else, read once on submit and emptied
 * before the request is sent.
 */
function CredentialForm({
  link,
  name,
  onSent,
  onSaved,
  onClose,
}: {
  link: QaLink
  name: string
  onSent: () => void
  onSaved: () => void
  onClose: () => void
}) {
  const user = useRef<HTMLInputElement>(null)
  const pass = useRef<HTMLInputElement>(null)
  const bypass = useRef<HTMLInputElement>(null)
  const fid = useId()
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  const isBypass = link.field === 'challenge_bypass'
  const where = link.environment === null ? name : `${name} · ${link.environment}`

  async function submit(e: FormEvent) {
    e.preventDefault()
    const username = user.current?.value ?? ''
    const password = pass.current?.value ?? ''
    const value = bypass.current?.value ?? ''
    for (const field of [user, pass, bypass]) if (field.current !== null) field.current.value = ''
    if (link.token === null) {
      setFailed(`This form needs a one-time link, and its link was already used or never came. Ask for a new one${isBypass ? '' : ' with Set credential'}.`)
      return
    }
    if (isBypass ? value === '' : username === '' || password === '') {
      setFailed(isBypass ? 'Type the value; nothing was sent.' : 'Type both the username and the password; nothing was sent.')
      return
    }
    setBusy(true)
    setFailed(null)
    const r = isBypass
      ? await putQaChallengeBypass(link.targetId, value, { token: link.token, environment: link.environment })
      : await putQaCredential(link.targetId, username, password, link.token, link.environment)
    setBusy(false)
    // `set_credential` checks the entry and the sign-in (`set_challenge_bypass`
    // the value's shape) BEFORE `consume_link`: those refusals leave the link
    // unused. A success consumed it, `qa_credential_link_invalid` is a dead
    // link, and the store's 503 comes after the consume (`linkSpent`); only
    // those drop the token.
    if (linkSpent(r)) onSent()
    if (r.status === 'error') {
      setFailed(
        linkSpent(r)
          ? `${writeFailure(r.error)} The link is used; ask for a new one to try again.`
          : `${writeFailure(r.error)} Nothing was saved; the link still works, fix the field and save again.`,
      )
      return
    }
    onSaved()
  }

  return (
    <Card className="ur-form" title={isBypass ? `Challenge bypass value for ${where}` : `Test credential for ${where}`}>
      {isBypass ? (
        <p className="ur-small">
          Type the value of the challenge bypass the app&rsquo;s owner made for these runs. It goes from this page to the
          workspace&rsquo;s secret store and nowhere else, and the live browser adds it on the target&rsquo;s approved origins
          alone. The link works once.
        </p>
      ) : (
        <p className="ur-small">
          Type the test account&rsquo;s username and password. They go from this page to the workspace&rsquo;s secret store and
          nowhere else: Claude, the plugin and this page&rsquo;s address never see them, and the live browser types them into
          the app&rsquo;s login without the agent holding them. Use a test account with no MFA or CAPTCHA. The link works once.
        </p>
      )}
      <form
        className="qa-cred"
        data-target-id={link.targetId}
        data-field={link.field}
        onSubmit={(e) => void submit(e)}
        autoComplete="off"
      >
        <div className="ur-fgrid">
          {isBypass ? (
            <>
              <label className="ur-fl" htmlFor={`${fid}-bypass_value`}>
                Value
              </label>
              <div>
                <input id={`${fid}-bypass_value`} name="bypass_value" type="password" ref={bypass} autoComplete="new-password" spellCheck={false} />
              </div>
            </>
          ) : (
            <>
              <label className="ur-fl" htmlFor={`${fid}-username`}>
                Username
              </label>
              <div>
                <input id={`${fid}-username`} name="username" type="text" ref={user} autoComplete="off" spellCheck={false} />
              </div>
              <label className="ur-fl" htmlFor={`${fid}-password`}>
                Password
              </label>
              <div>
                <input id={`${fid}-password`} name="password" type="password" ref={pass} autoComplete="new-password" />
              </div>
            </>
          )}
        </div>
        <div className="ur-formacts">
          <Button type="submit" kind="primary" size="sm" busy={busy}>
            {isBypass ? 'Save value' : 'Save credential'}
          </Button>
          <Button size="sm" onClick={onClose}>
            Cancel
          </Button>
        </div>
      </form>
      {failed !== null && (
        <p className="ur-small ur-bad" role="alert">
          {failed}
        </p>
      )}
    </Card>
  )
}

/** `id path` a line, as the form's screens box spells them. */
function parseScreens(text: string): QaScreen[] | string {
  const out: QaScreen[] = []
  for (const raw of text.split('\n')) {
    const line = raw.trim()
    if (line === '') continue
    const parts = line.split(/\s+/)
    if (parts.length !== 2 || !parts[1]!.startsWith('/')) return `"${line}" is not "<id> <path>", the path starting with /`
    out.push({ id: parts[0]!, path: parts[1]! })
  }
  return out
}

function screensText(screens: QaScreen[]): string {
  return screens.map((s) => `${s.id} ${s.path}`).join('\n')
}

interface Draft {
  name: string
  base_url: string
  repository: string
  authorized_by: string
  environment: 'production' | 'non_production'
  flows_browser_tool: FlowBrowserTool
  kind: 'none' | 'own_login'
  login_path: string
  signed_in_selector: string
  username_selector: string
  password_selector: string
  submit_selector: string
  /** The sign-in's start clicks, a row each (§2.2); `css` empty is none. */
  start_clicks: { text: string; css: string }[]
  /** Not drawn here (Approve host names it), but carried so a sign-in change never drops it. */
  extra_origin: string
  screens: string
  /** `MORE_FIELDS` as one JSON object; empty is none. */
  more: string
  /** The challenge bypass's kind (§2.5), `none` for none, and its header or cookie name. */
  bypass_kind: 'none' | 'header' | 'cookie'
  bypass_name: string
}

/** `qaspec.MAX_START_CLICKS`: the API refuses a fourth. */
const MAX_START_CLICKS = 3

function draftOf(t: QaTarget | null): Draft {
  return {
    name: t?.name ?? '',
    base_url: t?.base_url ?? '',
    repository: repoSlug(t?.repository ?? null) ?? '',
    authorized_by: t?.authorized_by ?? '',
    environment: t?.environment ?? 'production',
    flows_browser_tool: t?.flows_browser_tool ?? 'playwright',
    kind: t?.sign_in.kind ?? 'none',
    login_path: t?.sign_in.login_path ?? '',
    signed_in_selector: t?.sign_in.signed_in_selector ?? '',
    username_selector: t?.sign_in.username_selector ?? '',
    password_selector: t?.sign_in.password_selector ?? '',
    submit_selector: t?.sign_in.submit_selector ?? '',
    start_clicks: (t?.sign_in.start_clicks ?? []).map((c) => ({ text: c.text, css: c.css ?? '' })),
    extra_origin: t?.sign_in.extra_origin ?? '',
    screens: screensText(t?.screens ?? []),
    more: t === null || Object.keys(t.more).length === 0 ? '' : JSON.stringify(t.more, null, 2),
    bypass_kind: t?.challenge_bypass?.kind ?? 'none',
    bypass_name: t?.challenge_bypass?.name ?? '',
  }
}

/** The More settings box: an object of `MORE_FIELDS` only, or the refusal in words. */
function parseMore(text: string): Record<string, unknown> | string {
  if (text.trim() === '') return {}
  let value: unknown
  try {
    value = JSON.parse(text)
  } catch {
    return 'More settings is not JSON: write one object, for example {"widths": ["desktop"]}.'
  }
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return 'More settings must be one JSON object.'
  const unknown = Object.keys(value).filter((k) => !(MORE_FIELDS as readonly string[]).includes(k))
  if (unknown.length > 0) return `More settings takes only ${MORE_FIELDS.join(', ')}; not ${unknown.join(', ')}. Nothing was sent.`
  return value as Record<string, unknown>
}

/** The body a draft sends: every field for a registration, the changed ones for an edit. */
function bodyOf(d: Draft, was: Draft | null): QaTargetSend | string {
  const screens = parseScreens(d.screens)
  if (typeof screens === 'string') return screens
  const more = parseMore(d.more)
  if (typeof more === 'string') return more
  const signIn: Record<string, unknown> = { kind: d.kind }
  if (d.kind === 'own_login') {
    signIn.login_path = d.login_path.trim()
    signIn.signed_in_selector = d.signed_in_selector.trim()
    for (const k of ['username_selector', 'password_selector', 'submit_selector'] as const) {
      if (d[k].trim() !== '') signIn[k] = d[k].trim()
    }
    if (d.extra_origin !== '') signIn.extra_origin = d.extra_origin
    const clicks = d.start_clicks
      .map((c) => ({ text: c.text.trim(), css: c.css.trim() }))
      .filter((c) => c.text !== '' || c.css !== '')
      .map((c) => (c.css === '' ? { text: c.text } : c))
    if (clicks.length > 0) signIn.start_clicks = clicks
  }
  const full: QaTargetSend = {
    name: d.name.trim(),
    base_url: d.base_url.trim(),
    repository: d.repository,
    authorized_by: d.authorized_by.trim(),
    sign_in: signIn,
    screens,
    environment: d.environment,
    ...more,
    flows_browser_tool: d.flows_browser_tool,
  }
  if (d.bypass_kind !== 'none') full.challenge_bypass = { kind: d.bypass_kind, name: d.bypass_name.trim() }
  if (was === null) return full
  const old = bodyOf(was, null)
  if (typeof old === 'string') return full
  // A change sends what changed; a More setting that was removed is sent as
  // null, which the API reads as "clear it".
  const changed: Record<string, unknown> = {}
  const keys = new Set([...Object.keys(full), ...Object.keys(old)])
  for (const k of keys) {
    const now = (full as Record<string, unknown>)[k] ?? null
    if (JSON.stringify(now) !== JSON.stringify((old as Record<string, unknown>)[k] ?? null)) changed[k] = now
  }
  return changed as QaTargetSend
}

/** Register (target null) or change one. The API checks every field and says its own refusal. */
function TargetForm({
  target,
  onClose,
  onSaved,
}: {
  target: QaTarget | null
  onClose: () => void
  onSaved: (doc: QaTarget | null, created: boolean) => void
}) {
  const repos = useUrRead(loadRepositories, 'qa-target-repos')
  const [d, setD] = useState<Draft>(() => draftOf(target))
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  // The bypass's value: UNCONTROLLED, in the DOM input alone until a save reads and empties it.
  const bypassValue = useRef<HTMLInputElement>(null)
  // Each label cell names its control by id (`ur-fl` beside it, not around it).
  const fid = useId()
  const set = (k: keyof Draft) => (e: { target: { value: string } }) => setD((was) => ({ ...was, [k]: e.target.value }))
  const setClick = (i: number, k: 'text' | 'css') => (e: { target: { value: string } }) =>
    setD((was) => ({ ...was, start_clicks: was.start_clicks.map((c, j) => (j === i ? { ...c, [k]: e.target.value } : c)) }))
  const slugs = repos.state.status === 'ok' || repos.state.status === 'stale' ? repos.state.data.map((r) => repoName(r)) : []
  if (d.repository !== '' && !slugs.includes(d.repository)) slugs.push(d.repository)

  async function submit(e: FormEvent) {
    e.preventDefault()
    const value = d.bypass_kind === 'none' ? '' : (bypassValue.current?.value ?? '')
    if (bypassValue.current !== null) bypassValue.current.value = ''
    const body = bodyOf(d, target === null ? null : draftOf(target))
    if (typeof body === 'string') {
      setFailed(body)
      return
    }
    if (target !== null && Object.keys(body).length === 0 && value === '') {
      onClose()
      return
    }
    setBusy(true)
    setFailed(null)
    let doc: QaTarget | null = target
    if (target === null || Object.keys(body).length > 0) {
      const r = target === null ? await createQaTarget(body) : await patchQaTarget(target.id, body)
      if (r.status === 'error') {
        setBusy(false)
        setFailed(writeFailure(r.error))
        return
      }
      doc = r.status === 'ok' ? normQaTarget((r.data as { qa_target?: unknown } | null)?.qa_target) : null
    }
    // The value travels alone, once the target that names its kind and name is saved.
    const id = doc?.id ?? target?.id ?? null
    if (value !== '' && id !== null) {
      const v = await putQaChallengeBypass(id, value)
      if (v.status === 'error') {
        setBusy(false)
        setFailed(`The target is saved; its challenge bypass value is not: ${writeFailure(v.error)} Type it again to retry.`)
        return
      }
    }
    setBusy(false)
    onSaved(doc, target === null)
  }

  return (
    <Card className="ur-form" title={target === null ? 'Register a QA target' : `Change ${target.name}`}>
      <form className="qa-edit" onSubmit={(e) => void submit(e)}>
        <div className="ur-fgrid">
          <label className="ur-fl" htmlFor={`${fid}-name`}>
            Name
          </label>
          <div>
            <input id={`${fid}-name`} name="name" value={d.name} onChange={set('name')} maxLength={100} />
          </div>
          <label className="ur-fl" htmlFor={`${fid}-base_url`}>
            Address<small>https only</small>
          </label>
          <div>
            <input id={`${fid}-base_url`} name="base_url" value={d.base_url} onChange={set('base_url')} placeholder="https://app.example.com/" />
            {target !== null && target.host !== null && (
              <p className="ur-hint">{`Changing the address's host sets ${target.host}'s approval back to pending.`}</p>
            )}
          </div>
          <label className="ur-fl" htmlFor={`${fid}-repository`}>
            Files findings in
          </label>
          <div>
            <select id={`${fid}-repository`} name="repository" value={d.repository} onChange={set('repository')}>
              <option value="">Pick a registered repository</option>
              {slugs.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </div>
          <label className="ur-fl" htmlFor={`${fid}-authorized_by`}>
            Who said this application may be tested
          </label>
          <div>
            <input id={`${fid}-authorized_by`} name="authorized_by" value={d.authorized_by} onChange={set('authorized_by')} maxLength={200} />
          </div>
          <label className="ur-fl" htmlFor={`${fid}-environment`}>
            Environment
          </label>
          <div>
            <select id={`${fid}-environment`} name="environment" value={d.environment} onChange={set('environment')}>
              <option value="production">Production (read-only flows)</option>
              <option value="non_production">Non-production</option>
            </select>
          </div>
          <label className="ur-fl" htmlFor={`${fid}-flows_browser_tool`}>
            Flow browser
          </label>
          <div>
            <select id={`${fid}-flows_browser_tool`} name="flows_browser_tool" value={d.flows_browser_tool} onChange={set('flows_browser_tool')}>
              <option value="playwright">Playwright (the default)</option>
              <option value="jev">Jev</option>
            </select>
          </div>
          <label className="ur-fl" htmlFor={`${fid}-kind`}>
            Sign-in
          </label>
          <div>
            <select id={`${fid}-kind`} name="kind" value={d.kind} onChange={set('kind')}>
              <option value="none">Public pages, no sign-in</option>
              <option value="own_login">The app&rsquo;s own login</option>
            </select>
          </div>
          {d.kind === 'own_login' && (
            <>
              <label className="ur-fl" htmlFor={`${fid}-login_path`}>
                Login path
              </label>
              <div>
                <input id={`${fid}-login_path`} name="login_path" value={d.login_path} onChange={set('login_path')} placeholder="/login" />
              </div>
              <label className="ur-fl" htmlFor={`${fid}-signed_in_selector`}>
                Signed-in selector<small>required</small>
              </label>
              <div>
                <input id={`${fid}-signed_in_selector`} name="signed_in_selector" value={d.signed_in_selector} onChange={set('signed_in_selector')} />
              </div>
              <label className="ur-fl" htmlFor={`${fid}-username_selector`}>
                Username selector
              </label>
              <div>
                <input id={`${fid}-username_selector`} name="username_selector" value={d.username_selector} onChange={set('username_selector')} />
              </div>
              <label className="ur-fl" htmlFor={`${fid}-password_selector`}>
                Password selector
              </label>
              <div>
                <input id={`${fid}-password_selector`} name="password_selector" value={d.password_selector} onChange={set('password_selector')} />
              </div>
              <label className="ur-fl" htmlFor={`${fid}-submit_selector`}>
                Submit selector
              </label>
              <div>
                <input id={`${fid}-submit_selector`} name="submit_selector" value={d.submit_selector} onChange={set('submit_selector')} />
              </div>
              <div className="ur-fl">
                Start clicks
                <small>
                  What the gate clicks, in order, before the form (a chooser, or a &ldquo;Sign in&rdquo; button before the identity provider).
                  Text is the control&rsquo;s exact visible text; CSS, optional, narrows the candidates (for example button).
                </small>
              </div>
              <div>
                {d.start_clicks.map((c, i) => (
                  <div key={i} className="qa-start-click" role="group" aria-label={`Start click ${i + 1}`}>
                    <b>Click {i + 1}</b>
                    <label>
                      Text{' '}
                      <input
                        name={`start_click_text_${i}`}
                        value={c.text}
                        onChange={setClick(i, 'text')}
                        maxLength={60}
                        aria-label={`Click ${i + 1} text`}
                      />
                    </label>
                    <label>
                      CSS{' '}
                      <input
                        name={`start_click_css_${i}`}
                        value={c.css}
                        onChange={setClick(i, 'css')}
                        maxLength={200}
                        aria-label={`Click ${i + 1} CSS`}
                      />
                    </label>
                    <Button
                      size="sm"
                      name="remove_start_click"
                      onClick={() => setD((was) => ({ ...was, start_clicks: was.start_clicks.filter((_, j) => j !== i) }))}
                    >
                      Remove
                    </Button>
                  </div>
                ))}
                {d.start_clicks.length < MAX_START_CLICKS && (
                  <Button size="sm" onClick={() => setD((was) => ({ ...was, start_clicks: [...was.start_clicks, { text: '', css: '' }] }))}>
                    Add start click
                  </Button>
                )}
              </div>
            </>
          )}
          <label className="ur-fl" htmlFor={`${fid}-bypass_kind`}>
            Challenge bypass
          </label>
          <div>
            <select id={`${fid}-bypass_kind`} name="bypass_kind" value={d.bypass_kind} onChange={set('bypass_kind')}>
              <option value="none">None</option>
              <option value="header">A header the app&rsquo;s owner set up</option>
              <option value="cookie">A cookie the app&rsquo;s owner set up</option>
            </select>
            {d.bypass_kind !== 'none' && (
              <p className="ur-hint">
                Only for an app your workspace owns: the header or cookie its CAPTCHA test keys or WAF skip rule accept. The live
                browser adds it on this target&rsquo;s approved origins and nowhere else, and the agent never sees its value.
              </p>
            )}
          </div>
          {d.bypass_kind !== 'none' && (
            <>
              <label className="ur-fl" htmlFor={`${fid}-bypass_name`}>
                {d.bypass_kind === 'header' ? 'Header name' : 'Cookie name'}
              </label>
              <div>
                <input
                  id={`${fid}-bypass_name`}
                  name="bypass_name"
                  value={d.bypass_name}
                  onChange={set('bypass_name')}
                  maxLength={64}
                  spellCheck={false}
                  placeholder="X-QA-Bypass"
                />
              </div>
              <label className="ur-fl" htmlFor={`${fid}-bypass_value`}>
                Value<small>write-only</small>
              </label>
              <div>
                <input id={`${fid}-bypass_value`} name="bypass_value" type="password" ref={bypassValue} autoComplete="new-password" spellCheck={false} />
                <p className="ur-hint" data-bypass-value="">
                  {target === null || target.challenge_bypass === null
                    ? 'value not set'
                    : `${valueWords(target.challenge_bypass_value)}; type a value to replace it`}
                </p>
              </div>
            </>
          )}
          <label className="ur-fl" htmlFor={`${fid}-screens`}>
            Screens<small>one &ldquo;id path&rdquo; a line</small>
          </label>
          <div>
            <textarea id={`${fid}-screens`} name="screens" value={d.screens} onChange={set('screens')} rows={6} placeholder={'home /\npricing /pricing'} />
          </div>
          <label className="ur-fl" htmlFor={`${fid}-more`}>
            More settings
            <small>
              JSON: {MORE_FIELDS.join(', ')}. <code>file.label_rules</code>, e.g.{' '}
              <code>{'[{"paths": ["/agents/"], "labels": ["agent-page-rework"]}]'}</code>, adds a rule&rsquo;s labels to the
              findings on routes starting with one of its paths
            </small>
          </label>
          <div>
            <textarea id={`${fid}-more`} name="more" value={d.more} onChange={set('more')} rows={6} placeholder={'{"widths": ["desktop", "phone-390"]}'} />
          </div>
        </div>
        <div className="ur-formacts">
          <Button type="submit" kind="primary" size="sm" busy={busy}>
            {target === null ? 'Register' : 'Save changes'}
          </Button>
          <Button size="sm" onClick={onClose}>
            Cancel
          </Button>
        </div>
      </form>
      {failed !== null && (
        <p className="ur-small ur-bad" role="alert">
          {failed}
        </p>
      )}
    </Card>
  )
}
