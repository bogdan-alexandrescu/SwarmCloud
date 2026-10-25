import { loadPlatformSecrets, type PlatformSecretRow, type PlatformSecrets } from './api'
import { Banner, Card, Chip } from './components'
import { UrRefresh, UrRegion, useUrRead } from './RepositoriesParts'
import { PageHead } from './Shell'
import { timeAgo } from './types'
import './styles/keys.css'
import './styles/workspace-settings.css'
import './styles/secrets.css'

/**
 * ADMIN › PLATFORM SECRETS (lane SSC-UI, part of #2158;
 * docs/design/secrets-scopes.md §5, drawn as Config proposal E's S7).
 *
 * STATUS ONLY. Each platform secret the catalogue names
 * (swarm_api `platform_secrets.PLATFORM_SECRETS`), then the quota broker's
 * account secrets, two rows each: set or not, how many enabled versions (two
 * is a rotation in its grace), the newest one's age, and who manages it and
 * writes its value. Read from Secret Manager METADATA by
 * `GET /v1/admin/platform-secrets` (SSC-PLATFORM-STATUS), which never reads a
 * version's payload. There is NO FORM: no route writes a platform secret
 * (§5.3), and the page names the operator's command that does.
 *
 * A PLATFORM ADMIN'S ALONE. The tab is `admin: true` in SECTIONS and has a
 * row only in the platform admin's Admin panel (Spine.tsx `adminPagesFor`),
 * so a member, a viewer or a workspace admin is never shown it; the route is
 * `require_admin` whatever the console draws, and its 403 is said as such.
 *
 * `unreadable` with `permission_denied` is the deployed state until BGD sets
 * `enable_platform_secret_status = true` and runs `make bootstrap` (§12), so
 * it is said as that, not as a failure of the page.
 */

const ROUTE = 'GET /v1/admin/platform-secrets'

const STATUS_WORD: Readonly<Record<string, string>> = {
  set: 'set',
  not_set: 'not set',
  not_declared: 'not declared',
  unreadable: 'unreadable',
}

const MANAGED_WORD: Readonly<Record<string, string>> = {
  terraform: 'Terraform declares',
  'scripts/create-secrets.sh': 'the operator’s script creates',
  'quota-broker': 'the quota broker',
}

/** The rows that need a look: anything but set. */
function needsLook(rows: readonly PlatformSecretRow[]): PlatformSecretRow[] {
  return rows.filter((r) => r.status !== 'set')
}

function Lead({ d }: { d: PlatformSecrets }) {
  const look = needsLook(d.secrets)
  const denied = look.filter((r) => r.status === 'unreadable' && r.reason === 'permission_denied')
  const rotating = d.secrets.filter((r) => r.rotation_in_grace).length
  if (denied.length > 0 && denied.length === look.length && denied.length === d.secrets.length) {
    return (
      <section className="ws-lead is-next" aria-label="Not readable yet">
        <div className="ws-lead-t">
          <span className="ws-lead-k">Not readable yet</span>
          <b>swarm-api may not read Secret Manager metadata here</b>
          <span className="ws-lead-s">
            The status role is granted by <code>enable_platform_secret_status = true</code> in the bootstrap tfvars and
            {' '}<code>make bootstrap</code>, which only the owner applies.
          </span>
        </div>
      </section>
    )
  }
  if (look.length === 0) {
    return (
      <section className="ws-lead is-ok" aria-label="Nothing needs a look">
        <div className="ws-lead-t">
          <span className="ws-lead-k">Nothing needs a look</span>
          <b>{`All ${d.secrets.length} are set`}</b>
          {rotating > 0 && <span className="ws-lead-s">{`${rotating} ${rotating === 1 ? 'is' : 'are'} mid-rotation, which is expected for 24 h.`}</span>}
        </div>
      </section>
    )
  }
  const first = look[0]!
  return (
    <section className="ws-lead is-next" aria-label="Needs a look">
      <div className="ws-lead-t">
        <span className="ws-lead-k">Needs a look</span>
        <b>{look.length === 1 ? `${first.secret_id} is ${STATUS_WORD[first.status] ?? first.status}` : `${look.length} are not set: ${look.slice(0, 3).map((r) => r.secret_id).join(', ')}${look.length > 3 ? '…' : ''}`}</b>
        <span className="ws-lead-s">
          {rotating > 0 ? `${rotating} ${rotating === 1 ? 'is' : 'are'} mid-rotation, which is expected for 24 h. ` : ''}
          Its value is written by the command in Managed by, on the operator&rsquo;s machine.
        </span>
      </div>
    </section>
  )
}

function StateCell({ r }: { r: PlatformSecretRow }) {
  if (r.status === 'set') return <>set</>
  const why = r.reason === 'permission_denied' ? 'swarm-api may not read its metadata' : r.reason === 'read_failed' ? 'the read failed' : undefined
  return <Chip tone={r.status === 'unreadable' ? 'bad' : 'park'} title={why}>{STATUS_WORD[r.status] ?? r.status}</Chip>
}

export function AdminPlatformSecretsScreen() {
  const read = useUrRead(loadPlatformSecrets, 'platform-secrets')
  return (
    <div className="pk-page sec-page">
      <PageHead title="Platform secrets">
        <UrRefresh reads={[read]} />
      </PageHead>
      <UrRegion state={read.state} route={ROUTE} what="The platform's secrets" plural onRetry={read.reload}>
        {(d) => (
          <>
            <Lead d={d} />
            <Banner tone="info" title="Status only">
              Read from Secret Manager metadata. No value, length or version content is read, and nothing on this page can
              set one.
            </Banner>
            <Card title="Secrets">
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
                  {d.secrets.map((r) => (
                    <tr key={r.secret_id}>
                      <th scope="row" data-label="Name">
                        <code>{r.secret_id}</code>
                        {r.family === 'account' && <small className="sec-sub">{`account ${r.owner_tenant ?? '?'} · ${r.label ?? '?'}, ${r.half ?? 'access'}`}</small>}
                      </th>
                      <td data-label="Set"><StateCell r={r} /></td>
                      <td data-label="Enabled versions">
                        {r.enabled_versions === null ? 'not read' : r.enabled_versions}
                        {r.rotation_in_grace && <small className="sec-sub">rotation in its grace</small>}
                      </td>
                      <td data-label="Age of newest" title={r.newest_enabled_at ?? undefined}>{r.newest_enabled_at === null ? 'no version' : timeAgo(r.newest_enabled_at)}</td>
                      <td data-label="Managed by">
                        {r.family === 'account' ? <>the quota broker, on <a href="/capacity/accounts">Capacity › Accounts</a></> : MANAGED_WORD[r.managed_by] ?? r.managed_by}
                        {r.family !== 'account' && <small className="sec-sub"><code>{r.written_by}</code></small>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {(d.accounts_unread || d.accounts_refused > 0 || d.accounts_truncated) && (
                <p className="ur-small">
                  {d.accounts_unread ? 'The pool accounts could not be read, so their secrets are not listed. ' : ''}
                  {d.accounts_refused > 0 ? `${d.accounts_refused} account${d.accounts_refused === 1 ? ' names' : 's name'} no swarm-account- secret and ${d.accounts_refused === 1 ? 'was' : 'were'} not read. ` : ''}
                  {d.accounts_truncated ? 'More accounts exist than one read lists.' : ''}
                </p>
              )}
              <p className="ur-small">Platform settings stay on Pool limits and Runner sizes. Nothing platform-level is on Config.</p>
            </Card>
          </>
        )}
      </UrRegion>
    </div>
  )
}
