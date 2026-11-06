import { loadPlatformSecrets, type PlatformSecretRow, type PlatformSecrets } from './api'
import { Banner, Card } from './components'
import { UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { PageHead } from './Shell'
import { timeAgo } from './types'
import './styles/keys.css'
import './styles/secrets.css'

/**
 * ADMIN › PLATFORM SECRETS (lane SSC-UI over SSC-PLATFORM-STATUS, part of
 * #2158; docs/design/secrets-scopes.md §5, proposal E S7). STATUS ONLY: each
 * platform secret's set, enabled versions, the newest one's age and what
 * writes it, read by `GET /v1/admin/platform-secrets` from Secret Manager
 * METADATA under a role that cannot read a value. No value, length or last
 * characters, and no form: every platform secret is written by the
 * operator's `scripts/create-secrets.sh --stdin` or by the system that owns
 * it, and the row says which.
 *
 * A PLATFORM ADMIN'S ALONE (§5.3). The tab is `admin: true` in SECTIONS and
 * the spine lists it only for a platform admin (Spine.tsx `adminPagesFor`);
 * anyone else who types the address gets the route's 403, drawn as "admin
 * only" by `UrRegion`. The API is the gate; this page only describes it.
 */

const ROUTE = 'GET /v1/admin/platform-secrets'

/**
 * `not_declared` IS "NO SECRET" (visual QA pass 1, admin-shell-22, part of
 * #2596). The status means the name is in the catalogue and Secret Manager
 * holds no secret of that name. "Not declared" read as the opposite -- the
 * banner went on to say the name IS in the catalogue -- while the row's own
 * columns already said "no secret".
 */
const STATUS_WORDS: Readonly<Record<string, string>> = {
  set: 'set',
  not_set: 'declared, no version',
  not_declared: 'no secret',
  unreadable: 'unreadable',
}

const REASON_WORDS: Readonly<Record<string, string>> = {
  permission_denied: 'the status role is not granted yet (enable_platform_secret_status, make bootstrap)',
  read_failed: 'Secret Manager did not answer',
}

/**
 * WHAT TO DO ABOUT AN UNREAD STATUS, by its reason: the lead's second line.
 * `permission_denied` is the metadata role swarm-api has not been granted,
 * which `make bootstrap` grants with `enable_platform_secret_status`
 * (terraform/bootstrap/platform_secret_status.tf, #2158).
 */
const REASON_STEPS: Readonly<Record<string, string>> = {
  permission_denied: 'The status role is not granted yet. Run make bootstrap with enable_platform_secret_status = true to grant it.',
  read_failed: 'Secret Manager did not answer. Refresh to read again.',
}

/**
 * A SECRET THAT IS NOT THERE, NOT ONE NOBODY COULD SEE (VQ-049, part of
 * #2391). An `unreadable` row is a status nobody measured: with the status
 * role not granted every row is one, and counting them as missing told a
 * running platform that 31 of its secrets were not set. They are counted
 * apart, and their figures are a dash with its reason (PICKS.md: an unknown
 * figure is a dash, never 0).
 */
function needsLook(r: PlatformSecretRow): boolean {
  return r.status !== 'set' && r.status !== 'unreadable'
}

function unread(r: PlatformSecretRow): boolean {
  return r.status === 'unreadable'
}

/** An unmeasured figure: the dash and why, as S7a draws its `—` with a reason. */
function NotRead() {
  return (
    <span className="sec-unread">
      — <small>not read</small>
    </span>
  )
}

/** One reason for every unread row, or null when they differ. */
function sharedReason(rows: readonly PlatformSecretRow[]): string | null {
  const reasons = new Set(rows.map((r) => r.reason ?? ''))
  return reasons.size === 1 ? [...reasons][0]! || null : null
}

/** The broker's account secrets, two halves each: one line for all of them, as S7 draws it. */
function accountsLine(rows: readonly PlatformSecretRow[]) {
  const accounts = new Set(rows.map((r) => `${r.owner_tenant ?? ''}--${r.label ?? ''}`)).size
  const set = rows.filter((r) => r.status === 'set').length
  const unreadCount = rows.filter(unread).length
  const newest = rows.map((r) => r.newest_enabled_at).filter((t): t is string => t !== null).sort().pop() ?? null
  return { accounts, set, unread: unreadCount, total: rows.length, newest }
}

export function AdminPlatformSecretsScreen() {
  const read = useUrRead(loadPlatformSecrets, 'platform-secrets')
  return (
    <div className="pk-page sec-page">
      <PageHead title="Platform secrets">
        <UrRefresh reads={[read]} />
      </PageHead>
      <UrRegion state={read.state} route={ROUTE} what="The platform's secrets" plural onRetry={read.reload}>
        {(d) => <PlatformSecretsBody data={d} />}
      </UrRegion>
    </div>
  )
}

function PlatformSecretsBody({ data }: { data: PlatformSecrets }) {
  const platform = data.secrets.filter((r) => r.family !== 'account')
  const accounts = data.secrets.filter((r) => r.family === 'account')
  const look = data.secrets.filter(needsLook)
  const blind = data.secrets.filter(unread)
  const blindReason = sharedReason(blind)
  const grace = platform.filter((r) => r.rotation_in_grace)
  const line = accountsLine(accounts)
  return (
    <>
      {look.length > 0 ? (
        <section className="sec-lead is-warn" aria-label="Needs a look">
          <span className="sec-k">Needs a look</span>
          <b>
            {look.length !== 1
              ? `${look.length} platform secrets are not set`
              : look[0]!.status === 'not_declared'
                ? `${look[0]!.secret_id} has no secret yet`
                : `${look[0]!.secret_id} is ${STATUS_WORDS[look[0]!.status] ?? look[0]!.status}`}
          </b>
          <span>
            {look.length === 1 && look[0]!.status === 'not_declared'
              ? 'It is in the catalogue but nothing was stored. '
              : ''}
            {grace.length > 0 ? `${grace.length === 1 ? 'One other is' : `${grace.length} are`} mid-rotation, which is expected for 24 h. ` : ''}
            {blind.length > 0
              ? `${blind.length} other${blind.length === 1 ? '' : 's'} could not be read${blindReason !== null ? `: ${REASON_WORDS[blindReason] ?? blindReason}` : ''}.`
              : ''}
          </span>
        </section>
      ) : blind.length > 0 ? (
        <section className="sec-lead" aria-label="Status not read">
          <span className="sec-k">Status not read</span>
          <b>
            {blind.length === data.secrets.length
              ? `The status of ${blind.length === 1 ? 'the one platform secret' : `${blind.length} platform secrets`} cannot be read`
              : `The status of ${blind.length} of ${data.secrets.length} platform secrets cannot be read`}
          </b>
          <span>
            {blindReason !== null
              ? REASON_STEPS[blindReason] ?? `Secret Manager said ${blindReason}.`
              : 'Each row says why.'}
            {' This is not a count of missing secrets.'}
          </span>
        </section>
      ) : (
        <section className="sec-lead is-ok" aria-label="All set">
          <span className="sec-k">All set</span>
          <b>Every platform secret in the catalogue has an enabled version</b>
          {grace.length > 0 && <span>{grace.length === 1 ? 'One is' : `${grace.length} are`} mid-rotation, which is expected for 24 h.</span>}
        </section>
      )}
      <Banner tone="info" title="Status only">
        Read from Secret Manager metadata. No value, length or version content is read, and nothing on this page can set
        one.
      </Banner>
      {data.accounts_unread && <Banner tone="warn" title="The quota broker's account documents were not read">The catalogue rows below are still current.</Banner>}
      {data.accounts_refused > 0 && (
        <Banner tone="warn" title={`${data.accounts_refused} account ${data.accounts_refused === 1 ? 'document names' : 'documents name'} no swarm-account- secret`}>
          They were not read: this page asks Secret Manager about swarm- names only.
        </Banner>
      )}
      <Card>
        <table className="mcp-servers" aria-label="Platform secrets">
          <thead>
            <tr>
              <th scope="col">Name</th>
              <th scope="col">Set</th>
              <th scope="col">Enabled versions</th>
              <th scope="col">Age of newest</th>
              <th scope="col">Managed by</th>
            </tr>
          </thead>
          <tbody>
            {platform.map((r) => (
              <tr key={r.secret_id} className={needsLook(r) ? 'is-need' : undefined}>
                <th scope="row" data-label="Name"><code>{r.secret_id}</code></th>
                <td data-label="Set">
                  <span className={r.status === 'set' ? 'sec-state is-ok' : unread(r) ? 'sec-chip is-info' : 'sec-chip is-warn'}>{STATUS_WORDS[r.status] ?? r.status}</span>
                  {r.reason !== null && <small className="sec-sub">{REASON_WORDS[r.reason] ?? r.reason}</small>}
                </td>
                {/* SAID ONCE (visual QA pass 2, admin2-16): the pill says "no
                    secret"; the figures beside it are a dash, not the same words twice more. */}
                <td data-label="Enabled versions">
                  {unread(r) ? <NotRead /> : r.enabled_versions === null ? '—' : r.enabled_versions}
                  {r.rotation_in_grace && <small className="sec-sub">rotation in its grace</small>}
                </td>
                <td data-label="Age of newest">
                  {unread(r) ? <NotRead /> : r.newest_enabled_at === null ? (r.status === 'not_declared' ? '—' : 'no version') : timeAgo(r.newest_enabled_at)}
                </td>
                <td data-label="Managed by">{r.managed_by}<small className="sec-sub">{r.written_by}</small></td>
              </tr>
            ))}
            {accounts.length > 0 && (
              <tr className={line.set + line.unread < line.total ? 'is-need' : undefined}>
                <th scope="row" data-label="Name">
                  <code>swarm-account-&lt;tenant&gt;--&lt;label&gt;</code>
                  <small className="sec-sub">{line.accounts} {line.accounts === 1 ? 'account' : 'accounts'}, two halves each</small>
                </th>
                <td data-label="Set">
                  {line.unread === line.total ? (
                    <span className="sec-chip is-info">{STATUS_WORDS.unreadable}</span>
                  ) : (
                    <span className={line.set === line.total ? 'sec-state is-ok' : 'sec-chip is-warn'}>
                      {line.set === line.total ? `all ${line.total} set` : `${line.set} of ${line.total} set`}
                    </span>
                  )}
                  {line.unread > 0 && line.unread < line.total && <small className="sec-sub">{line.unread} not read</small>}
                </td>
                {/* A sum over halves nobody read is not a count: a dash once any is unread. */}
                <td data-label="Enabled versions">{line.unread > 0 ? <NotRead /> : accounts.reduce((n, r) => n + (r.enabled_versions ?? 0), 0)}</td>
                <td data-label="Age of newest">
                  {line.newest !== null ? `newest ${timeAgo(line.newest)}` : line.unread > 0 ? <NotRead /> : 'no version'}
                </td>
                <td data-label="Managed by">the quota broker<small className="sec-sub">Capacity › Accounts</small></td>
              </tr>
            )}
          </tbody>
        </table>
        {data.accounts_truncated && <p className="sec-hint">More accounts exist than this page reads at once; the line above counts the first ones.</p>}
      </Card>
    </>
  )
}
