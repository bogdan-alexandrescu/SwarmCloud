import { loadAdminQuota } from './api'
import type { TopicId } from './help'
import { HelpLinks } from './HelpCard'
import { Screen, timeAgo } from './Shell'
import { providerTone, type QuotaState } from './types'

/**
 * Provider quota, across every tenant. Admin.
 *
 * QUOTA IS PER PROVIDER PER TENANT, on purpose: tenants bring their own keys,
 * so one tenant's 429 is not a platform outage. Rows are therefore grouped by
 * provider with the tenants inside, never summed into a per-provider figure --
 * a sum across tenants would describe a thing that does not exist.
 *
 * THAT RULE IS NOW DRAWN RATHER THAN STATED. The paragraph above used to sit
 * on the screen; what replaces it is the absence of a per-provider figure
 * anywhere on it. The provider is an eyebrow with a tenant count beside it and
 * no number of its own, so there is nothing for a reader to mistake for a
 * platform total, which is the thing the sentence was guarding against.
 */
export function QuotaDetailScreen() {
  return (
    <Screen
      title="Provider quota"
      load={loadAdminQuota}
      summary={(d) => {
        const providers = new Set(d.quota.map((q) => q.provider)).size
        const tenants = new Set(d.quota.map((q) => q.tenant_id)).size
        return `${d.quota.length} documents · ${providers} providers · ${tenants} tenants`
      }}
      empty={{
        heading: 'No quota documents',
        // One sentence, and it is the one that says what WRITES a document --
        // which is the only thing that makes this particular emptiness mean
        // anything. The rest is at #help/quota-document-absent.
        body: 'A document is written the first time a worker reports on a provider.',
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
          <section className="section" key={provider}>
            {/* A title and a count. NOT a figure: see the note at the top of
                this file -- the provider row deliberately has no number of its
                own to be read as a per-provider total.

                NOT `.ctl-eyebrow`, deliberately. The eyebrow uppercases, and a
                provider name is DATA -- the same B17 reason `.id` exists. An
                eyebrow is for a category word the product chose (`BINDING`,
                `CEILINGS`); a value that came off a response keeps its case. */}
            <div className="ctl-toolbar">
              <h2 className="ctl-card-title">{provider}</h2>
              <span className="count-chip">
                {list.length} tenant{list.length === 1 ? '' : 's'}
              </span>
            </div>
            <div className="ctl-table">
              <table>
                <thead>
                  <tr>
                    <th scope="col">Tenant</th>
                    <th scope="col">State</th>
                    {/* The unit rides on the column name: these are the two
                        columns where a 0 and a blank mean different things and
                        the heading is where that is cheapest to say. */}
                    <th scope="col" className="is-num">Limit</th>
                    <th scope="col" className="is-num">Requests left</th>
                    <th scope="col" className="is-num">429s</th>
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
      <HelpLinks topics={QUOTA_TOPICS} />
    </>
  )
}

/** `providerTone`'s vocabulary, in the chip's. */
function chipTone(state: string): string {
  switch (providerTone(state)) {
    case 'ok': return 'is-ok'
    case 'bad': return 'is-bad'
    case 'wait': return 'is-warn'
    case 'live': return 'is-live'
    default: return 'is-unknown'
  }
}

function Row({ q }: { q: QuotaState }) {
  const tone = providerTone(q.state)
  // EXHAUSTED, DISABLED and COOLDOWN all admit nothing, and the reason differs.
  const admitsNothing = q.effective_limit === 0

  return (
    <tr className={admitsNothing ? 'is-bad' : tone === 'wait' ? 'is-warn' : undefined}>
      {/* No text-transform here. Tenant ids are opaque and a displayed id that
          differs from the real one is unusable. */}
      <th scope="row" className="mono">{q.tenant_id}</th>
      <td>
        {/* The chip carries the state WORD and repeats it as a silhouette, so
            the six provider states stay apart in a greyscale screenshot --
            which is where this screen is actually read. */}
        <span className={`ctl-chip ${chipTone(q.state)}`}>
          <i />
          {q.state}
        </span>
      </td>
      {/* THE ONE ZERO ON THIS SCREEN THAT IS A FACT. effective_limit returns 0
          deliberately when the state is EXHAUSTED, DISABLED or COOLDOWN, so it
          renders as 0 with the state chip beside it doing the explaining --
          not as an em dash, which would read as "not measured". */}
      <td className="is-num">{q.effective_limit}</td>
      {/* These genuinely can be absent, and absent is not zero. The em dash
          carries `.ctl-em`, which is this sheet's one mark for "nothing was
          ever recorded" -- dimmed, non-tabular and unselectable, so it cannot
          be copied out of the table as if it were a value. */}
      <td className="is-num">
        {q.requests_remaining === null ? <Em what="requests remaining" /> : q.requests_remaining}
      </td>
      <td className="is-num">{q.rate_limit_count}</td>
      <td>{q.last_429_at ? timeAgo(q.last_429_at) : <Em what="last 429" />}</td>
      <td>{q.updated_at ? timeAgo(q.updated_at) : <Em what="reported" />}</td>
    </tr>
  )
}

/**
 * The em dash, with the sentence attached to it rather than beside it.
 *
 * `aria-label` is the accessible route the brief requires: the mark is the
 * visible anchor and this is the words. A `title=` would have neither a
 * keyboard route nor a screen-reader one, which is the mistake that turned
 * the honesty suite red the last time this migration was attempted.
 */
function Em({ what }: { what: string }) {
  return (
    <i className="ctl-em" aria-label={`No ${what} was recorded. That is an absence, not a zero.`}>
      —
    </i>
  )
}

/**
 * Four `<dt>`/`<dd>` pairs, as three topics.
 *
 * "Six states, not five", "unknown is not healthy" and "an effective limit of
 * 0 is a fact" were three statements of one thing: which state a row is in is
 * the measurement, and only one kind of zero on this screen means anything.
 * They are one topic now. The others are the ones that make an ABSENCE here
 * mean something specific.
 */
const QUOTA_TOPICS: readonly TopicId[] = [
  'provider-quota-states',
  'quota-document-absent',
  'absent-vs-zero',
]
