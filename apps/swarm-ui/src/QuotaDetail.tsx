import { loadAdminQuota } from './api'
import { num } from './fetch'
import { Screen, timeAgo } from './Shell'
import { providerTone, type QuotaState } from './types'

/**
 * Provider quota, across every tenant. Admin.
 *
 * The tenant-scoped version of this lives on the Trouble board and answers
 * "is my provider healthy". This one answers "whose quota is in trouble",
 * which is a different question and only an admin can ask it.
 *
 * QUOTA IS PER PROVIDER PER TENANT, on purpose: tenants bring their own keys,
 * so one tenant's 429 is not a platform outage. Rows are therefore grouped by
 * provider with the tenants inside, never summed into a per-provider figure —
 * a sum across tenants would describe a thing that does not exist.
 */
export function QuotaDetailScreen() {
  return (
    <Screen
      title="Provider quota"
      load={loadAdminQuota}
      summary={(d) => {
        const providers = new Set(d.quota.map((q) => q.provider)).size
        const tenants = new Set(d.quota.map((q) => q.tenant_id)).size
        return `${d.quota.length} quota documents · ${providers} providers · ${tenants} tenants`
      }}
      empty={{
        heading: 'No quota documents',
        body: 'The read succeeded and returned nothing. A quota document is written the first time a worker reports on a provider for a tenant, so an environment where nothing has run yet has none.',
      }}
    >
      {(d) => <Grouped rows={d.quota} />}
    </Screen>
  )
}

function Grouped({ rows }: { rows: QuotaState[] }) {
  const byProvider = new Map<string, QuotaState[]>()
  for (const q of rows) {
    const list = byProvider.get(q.provider)
    if (list) list.push(q)
    else byProvider.set(q.provider, [q])
  }

  return (
    <>
      {Array.from(byProvider.entries())
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([provider, list]) => (
          <section className="section panel" key={provider}>
            <h2>
              {provider}
              <span className="count-chip">
                {list.length} tenant{list.length === 1 ? '' : 's'}
              </span>
            </h2>
            <div className="table-wrap">
              <table className="pools">
                <thead>
                  <tr>
                    <th scope="col">Tenant</th>
                    <th scope="col">State</th>
                    <th scope="col" className="n">Effective limit</th>
                    <th scope="col" className="n">Requests left</th>
                    <th scope="col" className="n">429s</th>
                    <th scope="col">Last 429</th>
                    <th scope="col">Reported</th>
                  </tr>
                </thead>
                <tbody>
                  {list
                    .sort((a, b) => a.tenant_id.localeCompare(b.tenant_id))
                    .map((q) => (
                      <Row key={`${q.provider}:${q.tenant_id}`} q={q} />
                    ))}
                </tbody>
              </table>
            </div>
          </section>
        ))}
      <Legend />
    </>
  )
}

function Row({ q }: { q: QuotaState }) {
  const tone = providerTone(q.state)
  // EXHAUSTED, DISABLED and COOLDOWN all admit nothing, and the reason differs.
  const admitsNothing = q.effective_limit === 0

  return (
    <tr className={admitsNothing ? 'over' : tone === 'wait' ? 'silent' : undefined}>
      {/* No text-transform here. Tenant ids are opaque and a displayed id that
          differs from the real one is unusable — `.filters button` elsewhere
          capitalises, which is why this is a plain cell. */}
      <th scope="row" className="mono">{q.tenant_id}</th>
      <td>
        <span className={`tag ${tone}`}>{q.state}</span>
      </td>
      {/* THE ONE ZERO ON THIS SCREEN THAT IS A FACT. effective_limit returns 0
          deliberately when the state is EXHAUSTED, DISABLED or COOLDOWN, so it
          renders as 0 with the state chip beside it doing the explaining —
          not as an em dash, which would read as "not measured". */}
      <td className="n">{q.effective_limit}</td>
      {/* These genuinely can be absent, and absent is not zero. */}
      <td className="n">{num(q.requests_remaining)}</td>
      <td className="n">{q.rate_limit_count}</td>
      <td>{q.last_429_at ? timeAgo(q.last_429_at) : '—'}</td>
      <td>{q.updated_at ? timeAgo(q.updated_at) : '—'}</td>
    </tr>
  )
}

function Legend() {
  return (
    <section className="section legend">
      <h2>Reading this screen</h2>
      <dl>
        <dt>Six states, not five</dt>
        <dd>
          <code>AVAILABLE</code>, <code>THROTTLED</code>, <code>EXHAUSTED</code>,{' '}
          <code>COOLDOWN</code>, <code>DISABLED</code>, <code>UNKNOWN</code>.
          THROTTLED is the common case during a squeeze and the one most easily
          missed — a chip that fell through to &ldquo;unknown&rdquo; for it
          would mislabel exactly the condition this screen is for.
        </dd>
        <dt>UNKNOWN is not healthy</dt>
        <dd>
          It means no worker has reported on this provider for this tenant
          recently. That is an absence of information, not an assurance.
        </dd>
        <dt>An effective limit of 0 is a fact</dt>
        <dd>
          It is returned deliberately when the state is EXHAUSTED, DISABLED or
          COOLDOWN — the only zero here that means something rather than
          nothing.
        </dd>
        <dt>A provider with no document does not appear</dt>
        <dd>
          This route lists quota documents, not providers. A provider that no
          tenant has ever driven has no document, so its absence here means
          &ldquo;never used&rdquo;, not &ldquo;no such provider&rdquo;. The
          tenant-scoped <code>/v1/providers</code> derives its list from the
          frozen runner-profile catalogue and does not have this gap.
        </dd>
      </dl>
    </section>
  )
}
