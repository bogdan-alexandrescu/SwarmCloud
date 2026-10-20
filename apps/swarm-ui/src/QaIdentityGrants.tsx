/**
 * THE QA IDENTITY, THE CONSOLE'S SIDE (lane QI-UI; docs/design/qa-identity.md
 * §4.6, part of #1649).
 *
 * Owner decisions 2026-10-19 (§7): a browser agent signs in to this console
 * as a QA service account whose IAP credential the live gate adds (option B);
 * a workspace may name it only once a platform admin has granted it, with a
 * reason, `eng` first (Q2); the account has member rights in its own
 * workspace and nothing lent (Q3).
 *
 * THREE PIECES, ONE FILE, so the words for the identity are written once:
 *
 *   * `QaIdentityGrants`, the Admin › Tenants panel. It is mounted for an
 *     admin only, and reads nothing for anyone else: the list route is
 *     `require_admin`, and a non-admin's 403 drawn as a red panel would read
 *     as a fault. No nav section is added, so the issue forms' Where list
 *     does not move (`tests/unit/scripts/test_issue_forms.py`).
 *   * `QaIdentityOption`, the Submit form's checkbox. It is drawn from the
 *     profile's DECLARATION (`input_contract.inputs.qa_identity`, contract
 *     request 106), never from a list of ours -- swarm-api refuses an
 *     undeclared key with 422 `invalid_input` -- and offered only when
 *     `GET /v1/qa-identities/mine` says this workspace is granted. Offering
 *     it otherwise invites a 403 `qa_identity_not_granted` after the click.
 *     Unticked sends nothing: no identity is the absence of the key.
 *   * `qaIdentityOf`, which the Browser tab's badge reads off the task's own
 *     input, so "Signed in as QA identity" is said only of a task that named
 *     it, whatever the profile could have done.
 *
 * GRANT NEEDS A REASON; REVOKE TAKES NONE. The design (§4.6) asks a reason of
 * both, but the API on main takes one only on the grant
 * (`PUT .../grants/{tenant_id}` with `{reason}`); its `DELETE` takes no body,
 * and a reason typed here would be thrown away. The code is the fact; the
 * question is in this lane's PR.
 *
 * A REFUSAL IS THE API'S OWN WORDS: its code and its sentence, word for word
 * (`qa_identity_self` for the QA workspace's own tenant, `not_found` for an
 * unknown one). Each is a fixed sentence of swarm-api's, never an echo of
 * anything sent.
 */
import { useCallback, useEffect, useId, useRef, useState } from 'react'

import { grantQaIdentity, loadQaIdentities, loadQaIdentityMine, revokeQaIdentity } from './api'
import { Button } from './components'
import type { ApiError, Result } from './fetch'
import type { QaIdentityAdminEntry, QaIdentityGrant, QaIdentityList, RunnerProfile } from './types'

/** The task input the identity travels under (contract request 106). */
export const QA_IDENTITY_KEY = 'qa_identity'

/** What the checkbox says, and what the badge calls the identity. */
export const QA_OPTION_LABEL = 'Sign in to the console as the QA identity'

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

// ---------------------------------------------------------------------------
// The task's own input, for the badge
// ---------------------------------------------------------------------------

/** The identity a task's input names, or null. The input is the masked copy; a name is never masked. */
export function qaIdentityOf(input: unknown): string | null {
  if (!isRecord(input)) return null
  const v = input[QA_IDENTITY_KEY]
  return typeof v === 'string' && v.trim() !== '' ? v : null
}

// ---------------------------------------------------------------------------
// The Submit option
// ---------------------------------------------------------------------------

/** The identities this profile declares, or null when it declares none (only `claude-code-browser` does). */
export function declaredQaIdentities(profile: RunnerProfile | null | undefined): string[] | null {
  const contract: unknown = profile?.input_contract
  const inputs = isRecord(contract) ? contract.inputs : undefined
  const decl = isRecord(inputs) ? inputs[QA_IDENTITY_KEY] : undefined
  if (!isRecord(decl) || !Array.isArray(decl.choices)) return null
  const choices = decl.choices.filter((c): c is string => typeof c === 'string' && c !== '')
  return choices.length === 0 ? null : [...new Set(choices)]
}

/**
 * The declared identity this workspace may name, or null: read once, and only
 * while `declared` is non-null. Before the read answers, when it fails, or when
 * the deployment has no account, nothing is offered -- never on a guess.
 */
export function useGrantedQaIdentity(declared: string[] | null): string | null {
  const wanted = declared !== null
  const [granted, setGranted] = useState<string[] | null>(null)
  useEffect(() => {
    if (!wanted) return
    let live = true
    Promise.resolve()
      .then(() => loadQaIdentityMine())
      .then(
        (r) => {
          if (!live) return
          setGranted(
            r.status === 'ok' || r.status === 'stale'
              ? r.data.identities.filter((e) => e.configured && e.granted).map((e) => e.identity)
              : [],
          )
        },
        () => {
          if (live) setGranted([])
        },
      )
    return () => {
      live = false
    }
  }, [wanted])
  if (declared === null || granted === null) return null
  return declared.find((d) => granted.includes(d)) ?? null
}

/** Add the identity to a built input; the caller has already refused a typed duplicate. */
export function withQaIdentity(input: Record<string, unknown>, identity: string): Record<string, unknown> {
  // A prototype-free copy, as `withBrowserTool` makes: a typed `__proto__` key stays a key.
  const out: Record<string, unknown> = Object.create(null) as Record<string, unknown>
  Object.assign(out, input)
  out[QA_IDENTITY_KEY] = identity
  return out
}

/** The checkbox, beside the browser tool picker. Drawn only for a granted identity. */
export function QaIdentityOption({ identity, checked, onChange }: { identity: string; checked: boolean; onChange: (on: boolean) => void }) {
  const hint = useId()
  return (
    <div className="c-field sbf-qa-identity" data-testid="qa-identity-option">
      <label>
        <input type="checkbox" checked={checked} aria-describedby={hint} onChange={(e) => onChange(e.target.checked)} />{' '}
        {QA_OPTION_LABEL}
      </label>
      <p id={hint} className="ur-hint">
        The live browser is signed in to this console as its QA identity ({identity}), a service account with its own
        workspace, not as you. The platform adds the credential; the agent never holds it.
      </p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Admin › Tenants › QA identity
// ---------------------------------------------------------------------------

type Said = { ok: true; text: string } | { ok: false; error: ApiError }

function SaidLine({ said }: { said: Said | null }) {
  if (said === null) return null
  if (said.ok)
    return (
      <p className="ur-hint" role="status">
        {said.text}
      </p>
    )
  return (
    <p className="ur-small ob-issue" role="alert" data-code={said.error.code ?? undefined}>
      {said.error.code !== null && <b className="ur-bad">{said.error.code}</b>} {said.error.message}
    </p>
  )
}

function saidOf<T>(r: Result<T>, text: string): Said {
  return r.status === 'error' ? { ok: false, error: r.error } : { ok: true, text }
}

function GrantRow({ identity, grant, busy, onRevoke }: { identity: string; grant: QaIdentityGrant; busy: boolean; onRevoke: () => void }) {
  const granted = grant.state === 'granted'
  return (
    <li data-grant-tenant={grant.tenant_id} data-state={grant.state}>
      <p>
        <code>{grant.tenant_id}</code> <span className="tag">{grant.state}</span>
        {grant.granted_by !== null && grant.granted_by !== '' && (
          <span className="ur-small">
            {' '}
            · granted by {grant.granted_by}
            {grant.granted_at !== null && grant.granted_at !== '' ? ` at ${grant.granted_at}` : ''}
          </span>
        )}
      </p>
      {grant.reason !== null && grant.reason !== '' && <p className="ur-small">Reason: {grant.reason}</p>}
      {granted && (
        <Button size="sm" disabled={busy} onClick={onRevoke} aria-label={`Revoke ${identity} for ${grant.tenant_id}`}>
          Revoke
        </Button>
      )}
    </li>
  )
}

function IdentityBlock({
  entry,
  tenants,
  reload,
}: {
  entry: QaIdentityAdminEntry
  tenants: readonly string[]
  reload: () => Promise<void>
}) {
  const [tenant, setTenant] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [said, setSaid] = useState<Said | null>(null)
  const run = async (call: () => Promise<Said>, after?: (s: Said) => void) => {
    if (busy) return
    setBusy(true)
    const s = await call()
    setBusy(false)
    setSaid(s)
    after?.(s)
    if (s.ok) await reload()
  }
  const granted = new Set(entry.grants.filter((g) => g.state === 'granted').map((g) => g.tenant_id))
  // A revoked workspace is offered again: a re-grant overwrites the document (§4.2).
  const offered = tenants.filter((t) => !granted.has(t))
  const grant = () =>
    run(
      async () => saidOf(await grantQaIdentity(entry.identity, tenant, reason.trim()), `Granted ${entry.identity} to ${tenant}.`),
      (s) => {
        if (s.ok) {
          setTenant('')
          setReason('')
        }
      },
    )
  const revoke = (t: string) => run(async () => saidOf(await revokeQaIdentity(entry.identity, t), `Revoked ${entry.identity} for ${t}.`))
  return (
    <div data-identity={entry.identity}>
      <p>
        <b>{entry.identity}</b>{' '}
        {entry.configured ? (
          <span className="ur-small">
            · configured · host <code>{entry.host}</code> · account <code>{entry.account}</code>
          </span>
        ) : (
          <span className="ur-small">
            · not configured: this deployment has no QA account, and every task naming it is refused
          </span>
        )}
      </p>
      {entry.grants.length === 0 ? (
        <p className="ur-hint">No workspace has been granted it.</p>
      ) : (
        <ul className="ten-qa-grants">
          {entry.grants.map((g) => (
            <GrantRow key={g.tenant_id} identity={entry.identity} grant={g} busy={busy} onRevoke={() => void revoke(g.tenant_id)} />
          ))}
        </ul>
      )}
      {entry.configured && (
        <div className="pp-deny">
          <label className="c-field">
            <span className="c-lbl">Workspace</span>
            <select className="c-inp" value={tenant} onChange={(e) => setTenant(e.target.value)}>
              <option value="">Choose a workspace</option>
              {offered.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </label>
          <label className="c-field">
            <span className="c-lbl">Reason (required)</span>
            <textarea className="c-inp" value={reason} maxLength={200} rows={2} onChange={(e) => setReason(e.target.value)} />
          </label>
          <Button size="sm" kind="primary" disabled={busy || tenant === '' || reason.trim() === ''} onClick={() => void grant()}>
            Grant
          </Button>
        </div>
      )}
      <SaidLine said={said} />
    </div>
  )
}

/** Admin › Tenants' "QA identity" panel. Mount it for an admin only. */
export function QaIdentityGrants({ tenants }: { tenants: readonly string[] }) {
  const [list, setList] = useState<QaIdentityList | null>(null)
  const [unread, setUnread] = useState<ApiError | null>(null)
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])
  const reload = useCallback(async () => {
    const r = await loadQaIdentities()
    if (!mounted.current) return
    if (r.status === 'ok' || r.status === 'stale') {
      setList(r.data)
      setUnread(null)
    } else if (r.status === 'error') setUnread(r.error)
    else setList({ identities: [] })
  }, [])
  useEffect(() => {
    void reload()
  }, [reload])
  // An API without these routes (and the fixture build) answers 404 with no
  // code: that is "not served here", information rather than a failure.
  const notServed = unread !== null && unread.httpStatus === 404 && unread.code === null
  return (
    <section className="section" data-testid="qa-identity" aria-labelledby="ten-qa-identity">
      <h2 id="ten-qa-identity">QA identity</h2>
      <p className="ur-hint">
        A workspace granted here may ask for a browser task signed in to this console as the QA account. Grant it only with a
        reason; a revoke takes effect at the task’s next credential renewal.
      </p>
      {notServed && <p className="ur-hint">This API serves no QA identity routes.</p>}
      {unread !== null && !notServed && <SaidLine said={{ ok: false, error: unread }} />}
      {list === null && unread === null && <p className="ur-hint">Reading…</p>}
      {list !== null && list.identities.length === 0 && <p className="ur-hint">This API offers no QA identity.</p>}
      {list?.identities.map((e) => (
        <IdentityBlock key={e.identity} entry={e} tenants={tenants} reload={reload} />
      ))}
    </section>
  )
}
