import { useEffect, useRef, useState, type FormEvent } from 'react'
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
  cannotRunBecause,
  createQaTarget,
  deleteQaTarget,
  loadQaTargets,
  MORE_FIELDS,
  mintQaCredentialLink,
  normQaTarget,
  patchQaTarget,
  putQaCredential,
  qaCredentialLinkFrom,
  repoSlug,
  runQaTarget,
  type QaLink,
  type QaScreen,
  type QaTarget,
  type QaTargetSend,
} from './qaTargetsApi'
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
 * in memory, never drawn. Either way the token is sent at most once: the API
 * consumes it before the write, so a retry needs a new link.
 */

/** This page's address under Repositories (`RepositoriesScreen` mounts it on `page`). */
export const QA_TARGETS_PAGE = 'work/repositories?page=qa-targets'

const LIST_ROUTE = 'GET /v1/qa-targets'

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

export function QaTargetsScreen({ go, link }: { go: (to: string) => void; link?: QaLink | null }) {
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

  async function openCredential(t: QaTarget) {
    setSaid(null)
    const r = await mintQaCredentialLink(t.id)
    if (r.status === 'error') {
      setSaid({ tone: 'bad', title: `No credential link for ${t.name}`, text: writeFailure(r.error) })
      return
    }
    if (r.status === 'ok') setCred({ targetId: t.id, token: r.data.token })
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
      <p className="ur-small">
        Web applications this workspace&rsquo;s visual-QA runs test, each filing its findings in one of the workspace&rsquo;s
        registered repositories. A new or changed host waits for a platform admin&rsquo;s approval before any run.
      </p>
      {adminGate !== null && (
        <p className="ur-small" data-role-gate="admin">
          {adminGate}
        </p>
      )}
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
            setSaid({ tone: 'info', title: 'Credential saved', text: `The test credential for ${nameOf(cred.targetId)} is set. No page, route or log can show it again.` })
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
                onCredential={() => void openCredential(t)}
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

function hostWords(t: QaTarget): string {
  if (t.host_state === 'approved') {
    return t.approved_by === null ? 'approved' : `approved by ${t.approved_by}${t.approved_at === null ? '' : ` ${timeAgo(t.approved_at)}`}`
  }
  return t.host_state ?? 'unknown'
}

function credentialWords(t: QaTarget): string | null {
  if (t.sign_in.kind !== 'own_login') return null
  const c = t.credential
  if (!c.set) return 'Own login · test credential not set'
  const parts = ['Own login · test credential set']
  if (c.set_at !== null) parts.push(timeAgo(c.set_at))
  if (c.set_by !== null) parts.push(`by ${c.set_by}`)
  return parts.join(' ')
}

function TargetCard({
  t,
  go,
  admin,
  platformAdmin,
  runGate,
  onApproved,
  onEdit,
  onCredential,
  onDeleted,
}: {
  t: QaTarget
  go: (to: string) => void
  admin: boolean
  /** The session is a platform admin (`principal.is_admin`): Approve host is drawn. */
  platformAdmin: boolean
  /** The target the approve route answered. */
  onApproved: (doc: QaTarget) => void
  /** The caller's role is below `member`: Run is disabled and this says why. */
  runGate: string | null
  onEdit: () => void
  onCredential: () => void
  onDeleted: () => void
}) {
  const why = cannotRunBecause(t)
  const [reportOnly, setReportOnly] = useState(false)
  const [busy, setBusy] = useState(false)
  const [started, setStarted] = useState<string | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [approving, setApproving] = useState(false)
  const canApprove = platformAdmin && t.host !== null && t.host_state !== 'approved'

  async function run() {
    if (why !== null || runGate !== null) return
    setBusy(true)
    setFailed(null)
    setStarted(null)
    const r = await runQaTarget(t.id, reportOnly ? 'none' : 'issues')
    setBusy(false)
    if (r.status === 'error') setFailed(writeFailure(r.error))
    else if (r.status === 'ok') setStarted(r.data.workflow_id)
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
  const login = credentialWords(t)
  return (
    <div className="qa-target" data-target-id={t.id} data-host-state={t.host_state ?? undefined}>
    <Card className="ur-card" title={t.name}>
      <div className="ur-cmeta">
        <span>
          Address <b>{t.base_url ?? t.host ?? t.id}</b>
        </span>
        <span>
          Files in <b>{slug ?? t.repository ?? t.repo_id ?? 'an unnamed repository'}</b>
        </span>
      </div>
      <div className="ur-cmeta">
        <span>
          Host <b className={t.host_state === 'approved' ? undefined : 'ur-bad'}>{hostWords(t)}</b>
        </span>
        <span>{t.environment === 'non_production' ? 'Non-production' : 'Production'}</span>
        <span>{t.screens.length === 0 ? 'Screens discovered' : `${t.screens.length} screen${t.screens.length === 1 ? '' : 's'}`}</span>
      </div>
      {login !== null && <div className="ur-cmeta">{login}</div>}
      {why !== null && (
        <p className="ur-small ur-bad" data-cannot-run="">
          Cannot run: {why}
        </p>
      )}
      {runGate !== null && why === null && (
        <p className="ur-small" data-role-gate="member">
          {runGate}
        </p>
      )}
      <div className="ur-cacts">
        <Button size="sm" kind="primary" busy={busy} disabled={why !== null || runGate !== null} onClick={() => void run()}>
          Run
        </Button>
        <label className="ur-small">
          <input type="checkbox" checked={reportOnly} onChange={(e) => setReportOnly(e.target.checked)} data-report-only="" /> report
          only, file no issues
        </label>
        {admin && (
          <>
            <Button size="sm" onClick={onEdit}>
              Edit
            </Button>
            {t.sign_in.kind === 'own_login' && (
              <Button size="sm" onClick={onCredential}>
                Set credential
              </Button>
            )}
            <Button size="sm" kind="danger" disabled={busy} onClick={() => setConfirmDelete(true)}>
              Delete
            </Button>
          </>
        )}
        {canApprove && !approving && (
          <Button size="sm" onClick={() => setApproving(true)}>
            Approve host
          </Button>
        )}
      </div>
      {canApprove && approving && (
        <ApproveHostForm
          t={t}
          onClose={() => setApproving(false)}
          onApproved={(doc) => {
            setApproving(false)
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
      {started !== null && (
        <p className="ur-small" role="status">
          Started{' '}
          <UrLink to={`work/workflows?wf=${encodeURIComponent(started)}`} go={go}>
            {started}
          </UrLink>
        </p>
      )}
      {failed !== null && (
        <p className="ur-small ur-bad" role="alert">
          {failed}
        </p>
      )}
    </Card>
    </div>
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
 */
function ApproveHostForm({ t, onClose, onApproved }: { t: QaTarget; onClose: () => void; onApproved: (doc: QaTarget) => void }) {
  const host = t.host ?? ''
  const origin = t.sign_in.extra_origin
  const [typed, setTyped] = useState('')
  const [typedOrigin, setTypedOrigin] = useState('')
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  const matches = host !== '' && typedHost(typed) === host && (origin === null || typedOrigin.trim() === origin)

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (!matches || busy) return
    setBusy(true)
    setFailed(null)
    const r = await approveQaTargetHost(t.id, host, origin)
    setBusy(false)
    if (r.status === 'error') {
      setFailed(writeFailure(r.error))
      return
    }
    if (r.status === 'ok' && r.data !== null) onApproved(r.data)
    else setFailed('The approval was answered without the target; refresh to read its host.')
  }

  return (
    <form className="qa-approve" data-target-id={t.id} onSubmit={(e) => void submit(e)} autoComplete="off">
      <p className="ur-small">
        {`Approving ${host} lets this workspace's visual-QA runs open it. The approval is recorded under your name.`}
        {origin !== null && ` The login also signs in through ${origin}, approved with it.`}
      </p>
      <label>
        Type the host to confirm{' '}
        <input name="confirm_host" value={typed} onChange={(e) => setTyped(e.target.value)} placeholder={host} spellCheck={false} />
      </label>
      {origin !== null && (
        <label>
          Type the login&rsquo;s extra origin{' '}
          <input name="confirm_extra_origin" value={typedOrigin} onChange={(e) => setTypedOrigin(e.target.value)} placeholder={origin} spellCheck={false} />
        </label>
      )}
      <span className="ur-cacts">
        <Button type="submit" kind="primary" size="sm" busy={busy} disabled={!matches}>
          {`Approve ${host}`}
        </Button>
        <Button size="sm" onClick={onClose}>
          Cancel
        </Button>
      </span>
      {failed !== null && (
        <p className="ur-small ur-bad" role="alert">
          {failed}
        </p>
      )}
    </form>
  )
}

/**
 * The test credential's form. The fields are UNCONTROLLED: their values live
 * in the DOM inputs and nowhere else, read once on submit and emptied before
 * the request is sent.
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
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)

  async function submit(e: FormEvent) {
    e.preventDefault()
    const username = user.current?.value ?? ''
    const password = pass.current?.value ?? ''
    if (user.current !== null) user.current.value = ''
    if (pass.current !== null) pass.current.value = ''
    if (link.token === null) {
      setFailed('This form needs a one-time link, and its link was already used or never came. Ask for a new one with Set credential.')
      return
    }
    if (username === '' || password === '') {
      setFailed('Type both the username and the password; nothing was sent.')
      return
    }
    setBusy(true)
    setFailed(null)
    const r = await putQaCredential(link.targetId, username, password, link.token)
    // Sent once: the API consumed the link before it wrote.
    onSent()
    setBusy(false)
    if (r.status === 'error') {
      setFailed(`${writeFailure(r.error)} The link is used; ask for a new one to try again.`)
      return
    }
    onSaved()
  }

  return (
    <Card title={`Test credential for ${name}`}>
      <p className="ur-small">
        Type the test account&rsquo;s username and password. They go from this page to the workspace&rsquo;s secret store and
        nowhere else: Claude, the plugin and this page&rsquo;s address never see them, and the live browser types them into
        the app&rsquo;s login without the agent holding them. Use a test account with no MFA or CAPTCHA. The link works once.
      </p>
      <form className="qa-cred" data-target-id={link.targetId} onSubmit={(e) => void submit(e)} autoComplete="off">
        <label>
          Username <input name="username" type="text" ref={user} autoComplete="off" spellCheck={false} />
        </label>
        <label>
          Password <input name="password" type="password" ref={pass} autoComplete="new-password" />
        </label>
        <span className="ur-cacts">
          <Button type="submit" kind="primary" size="sm" busy={busy}>
            Save credential
          </Button>
          <Button size="sm" onClick={onClose}>
            Cancel
          </Button>
        </span>
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
  kind: 'none' | 'own_login'
  login_path: string
  signed_in_selector: string
  username_selector: string
  password_selector: string
  submit_selector: string
  screens: string
  /** `MORE_FIELDS` as one JSON object; empty is none. */
  more: string
}

function draftOf(t: QaTarget | null): Draft {
  return {
    name: t?.name ?? '',
    base_url: t?.base_url ?? '',
    repository: repoSlug(t?.repository ?? null) ?? '',
    authorized_by: t?.authorized_by ?? '',
    environment: t?.environment ?? 'production',
    kind: t?.sign_in.kind ?? 'none',
    login_path: t?.sign_in.login_path ?? '',
    signed_in_selector: t?.sign_in.signed_in_selector ?? '',
    username_selector: t?.sign_in.username_selector ?? '',
    password_selector: t?.sign_in.password_selector ?? '',
    submit_selector: t?.sign_in.submit_selector ?? '',
    screens: screensText(t?.screens ?? []),
    more: t === null || Object.keys(t.more).length === 0 ? '' : JSON.stringify(t.more, null, 2),
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
  const signIn: Record<string, string> = { kind: d.kind }
  if (d.kind === 'own_login') {
    signIn.login_path = d.login_path.trim()
    signIn.signed_in_selector = d.signed_in_selector.trim()
    for (const k of ['username_selector', 'password_selector', 'submit_selector'] as const) {
      if (d[k].trim() !== '') signIn[k] = d[k].trim()
    }
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
  }
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
  const set = (k: keyof Draft) => (e: { target: { value: string } }) => setD((was) => ({ ...was, [k]: e.target.value }))
  const slugs = repos.state.status === 'ok' || repos.state.status === 'stale' ? repos.state.data.map((r) => repoName(r)) : []
  if (d.repository !== '' && !slugs.includes(d.repository)) slugs.push(d.repository)

  async function submit(e: FormEvent) {
    e.preventDefault()
    const body = bodyOf(d, target === null ? null : draftOf(target))
    if (typeof body === 'string') {
      setFailed(body)
      return
    }
    if (target !== null && Object.keys(body).length === 0) {
      onClose()
      return
    }
    setBusy(true)
    setFailed(null)
    const r = target === null ? await createQaTarget(body) : await patchQaTarget(target.id, body)
    setBusy(false)
    if (r.status === 'error') {
      setFailed(writeFailure(r.error))
      return
    }
    const doc = r.status === 'ok' ? normQaTarget((r.data as { qa_target?: unknown } | null)?.qa_target) : null
    onSaved(doc, target === null)
  }

  return (
    <Card title={target === null ? 'Register a QA target' : `Change ${target.name}`}>
      <form className="qa-edit" onSubmit={(e) => void submit(e)}>
        <label>
          Name <input name="name" value={d.name} onChange={set('name')} maxLength={100} />
        </label>
        <label>
          Address (https only) <input name="base_url" value={d.base_url} onChange={set('base_url')} placeholder="https://app.example.com/" />
        </label>
        <label>
          Files findings in{' '}
          <select name="repository" value={d.repository} onChange={set('repository')}>
            <option value="">Pick a registered repository</option>
            {slugs.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        <label>
          Who said this application may be tested{' '}
          <input name="authorized_by" value={d.authorized_by} onChange={set('authorized_by')} maxLength={200} />
        </label>
        <label>
          Environment{' '}
          <select name="environment" value={d.environment} onChange={set('environment')}>
            <option value="production">Production (read-only flows)</option>
            <option value="non_production">Non-production</option>
          </select>
        </label>
        <label>
          Sign-in{' '}
          <select name="kind" value={d.kind} onChange={set('kind')}>
            <option value="none">Public pages, no sign-in</option>
            <option value="own_login">The app&rsquo;s own login</option>
          </select>
        </label>
        {d.kind === 'own_login' && (
          <fieldset>
            <label>
              Login path <input name="login_path" value={d.login_path} onChange={set('login_path')} placeholder="/login" />
            </label>
            <label>
              Signed-in selector (required){' '}
              <input name="signed_in_selector" value={d.signed_in_selector} onChange={set('signed_in_selector')} />
            </label>
            <label>
              Username selector <input name="username_selector" value={d.username_selector} onChange={set('username_selector')} />
            </label>
            <label>
              Password selector <input name="password_selector" value={d.password_selector} onChange={set('password_selector')} />
            </label>
            <label>
              Submit selector <input name="submit_selector" value={d.submit_selector} onChange={set('submit_selector')} />
            </label>
          </fieldset>
        )}
        <label>
          Screens, one &ldquo;id path&rdquo; a line{' '}
          <textarea name="screens" value={d.screens} onChange={set('screens')} rows={4} placeholder={'home /\npricing /pricing'} />
        </label>
        <label>
          More settings (JSON: {MORE_FIELDS.join(', ')}){' '}
          <textarea name="more" value={d.more} onChange={set('more')} rows={4} placeholder={'{"widths": ["desktop", "phone-390"]}'} />
        </label>
        {target !== null && target.host !== null && (
          <p className="ur-small">{`Changing the address's host sets ${target.host}'s approval back to pending.`}</p>
        )}
        <span className="ur-cacts">
          <Button type="submit" kind="primary" size="sm" busy={busy}>
            {target === null ? 'Register' : 'Save changes'}
          </Button>
          <Button size="sm" onClick={onClose}>
            Cancel
          </Button>
        </span>
      </form>
      {failed !== null && (
        <p className="ur-small ur-bad" role="alert">
          {failed}
        </p>
      )}
    </Card>
  )
}
