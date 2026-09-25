import { loadAdminQuota } from './api'
import type { TopicId } from './help'
import { HelpLinks } from './HelpCard'
import { Screen, timeAgo } from './Shell'
import { AGE_TICK_MS, useNow } from './useNow'
import {
  ageSpan,
  pluralise,
  providerTenantPool,
  providerTone,
  quotaReadingAge,
  type QuotaState,
} from './types'

/**
 * The column names that are said twice -- in the header a desktop shows, and
 * as the `data-label` a stacked row shows in its place -- written once, as
 * `COULD_START` is on Pools, so the two cannot drift apart.
 *
 * `Quota cap` (CP-8, #85). It was `Limit`, and read as the tenant's limit for
 * the provider: 50 here beside 20 on Pools, with nothing saying which binds.
 * It is the document's `effective_limit` -- the lowest of its configured hard
 * max, the AIMD target and the cap derived from the provider, forced to 0 by
 * EXHAUSTED, DISABLED and COOLDOWN -- and that figure is ONE input to the
 * pool named beside it, whose own ceiling may be lower. The column name
 * carries the basis; no `?` goes inside the table.
 *
 * `429s (this run)` (CP-10). The count is reset when the run ends -- a clean
 * run reports the provider available, or the broker's refresh retires the
 * cooldown and the reset window -- while `Last 429` keeps its time, so `0`
 * beside `25h ago` is correct and was drawn as a contradiction. The column
 * name carries the window (B7.4 route 2); `#help/quota-row-fields`, in the
 * footer index, defines the run (route 3).
 */
const QUOTA_CAP = 'Quota cap'
const FEEDS_POOL = 'Feeds pool'
const RATE_LIMITS = '429s (this run)'

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
        // All three pluralised, not only the one that happened to be plural
        // in the fixture: the live console said `1 providers` (CP-22).
        return `${pluralise(d.quota.length, 'document')} · ${pluralise(providers, 'provider')} · ${pluralise(tenants, 'tenant')}`
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
  // THE CLOCK THE STALE MARK IS JUDGED BY (CP-9). The five-second age tick the
  // head moves on, so a reading crosses the threshold on screen while the page
  // is open rather than only when it is reloaded.
  const now = useNow(AGE_TICK_MS)
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
            {/* THE COUNT IS THE QUALIFIER SLOT (CP-22). It was a `.count-chip`
                -- body-size mono beside the title -- where every sibling
                capacity screen puts a fact about the section in
                `.ctl-card-note`, right-aligned and muted. Same fact, the slot
                the design system has for it. */}
            <div className="ctl-toolbar">
              <h2 className="ctl-card-title">{provider}</h2>
              <span className="ctl-card-note is-end">{pluralise(list.length, 'tenant')}</span>
            </div>
            <div className="ctl-table is-stacked">
              <table role="table">
                <thead role="rowgroup">
                  <tr role="row">
                    <th role="columnheader" scope="col">Tenant</th>
                    <th role="columnheader" scope="col">State</th>
                    {/* The unit rides on the column name: these are the two
                        columns where a 0 and a blank mean different things and
                        the heading is where that is cheapest to say. */}
                    <th role="columnheader" scope="col" className="is-num">{QUOTA_CAP}</th>
                    {/* WHAT THE CAP FEEDS, right after it (CP-8). */}
                    <th role="columnheader" scope="col">{FEEDS_POOL}</th>
                    <th role="columnheader" scope="col" className="is-num">Requests left</th>
                    <th role="columnheader" scope="col" className="is-num">{RATE_LIMITS}</th>
                    <th role="columnheader" scope="col">Last 429</th>
                    <th role="columnheader" scope="col">Reported</th>
                  </tr>
                </thead>
                <tbody role="rowgroup">
                  {list
                    .sort((a, b) => a.tenant_id.localeCompare(b.tenant_id))
                    .map((q) => (
                      <Row key={`${q.provider}:${q.tenant_id}`} q={q} now={now} />
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

function Row({ q, now }: { q: QuotaState; now: number }) {
  const tone = providerTone(q.state)
  // EXHAUSTED, DISABLED and COOLDOWN all admit nothing, and the reason differs.
  const admitsNothing = q.effective_limit === 0
  // CP-9 (#85): A READING OLDER THAN TWICE THE BROKER'S SWEEP IS NOT A
  // CURRENT VERDICT. The live console drew a document last reported five days
  // earlier as a green `available`, the same mark as one reported a minute
  // ago. `quotaReadingAge` (types.ts) states the interval once, and says why
  // it is the sweep's and not a report's.
  const age = quotaReadingAge(q, now)
  const pool = providerTenantPool(q.provider, q.tenant_id)

  return (
    <tr role="row" className={admitsNothing ? 'is-bad' : tone === 'wait' ? 'is-warn' : undefined}>
      {/* No text-transform here. Tenant ids are opaque and a displayed id that
          differs from the real one is unusable. */}
      <th role="rowheader" scope="row" className="mono">{q.tenant_id}</th>
      <td role="cell" data-label="State">
        {/* The chip carries the state WORD and repeats it as a silhouette, so
            the six provider states stay apart in a greyscale screenshot --
            which is where this screen is actually read.

            A STALE READING KEEPS ITS WORD AND LOSES THE OK VERDICT. The word
            is what the document last said and is still true of that moment;
            the ok mark would claim it is true NOW, so a stale `available`
            takes the unknown ring -- an absence of current information, drawn
            as one -- and the stale mark with its age beside it. A stale
            throttled, spent or disabled reading keeps its own mark: those are
            verdicts nothing later has contradicted, and the age says how old
            they are. */}
        <span className={`ctl-chip ${age.stale && chipTone(q.state) === 'is-ok' ? 'is-unknown' : chipTone(q.state)}`}>
          <i aria-hidden="true" />
          {q.state}
        </span>
        {age.stale && (
          <span
            className="ctl-stale-mark"
            title={`Reported ${q.updated_at ? timeAgo(q.updated_at, now) : 'at an unknown time'}. A reading older than twice the quota broker's sweep is not drawn as current.`}
          >
            {age.ageMs === null ? 'age unknown' : `${ageSpan(age.ageMs)} old`}
          </span>
        )}
      </td>
      {/* THE ONE ZERO ON THIS SCREEN THAT IS A FACT. effective_limit returns 0
          deliberately when the state is EXHAUSTED, DISABLED or COOLDOWN, so it
          renders as 0 with the state chip beside it doing the explaining --
          not as an em dash, which would read as "not measured". */}
      <td role="cell" data-label={QUOTA_CAP} className="is-num">{q.effective_limit}</td>
      {/* THE POOL THAT FIGURE FEEDS (CP-8). Only admins read this screen, and
          `service.capacity()` lists every tenant's provider pool to them, so
          the row this links to exists on Pools, where `Set by` says which
          value binds it. No pool ceiling is drawn here: that would be a
          second read with its own freshness, and a figure beside this one
          that could be older or newer than it without saying so. Mono,
          because it is an identifier; the full name is in `title` for the
          width at which it ellipsizes. */}
      <td role="cell" data-label={FEEDS_POOL}>
        <a className="ctl-link mono" href="#capacity/pools" title={pool}>
          {pool}
        </a>
      </td>
      {/* These genuinely can be absent, and absent is not zero. The em dash
          carries `.ctl-em`, which is this sheet's one mark for "nothing was
          ever recorded" -- dimmed, non-tabular and unselectable, so it cannot
          be copied out of the table as if it were a value. */}
      <td role="cell" data-label="Requests left" className="is-num">
        {q.requests_remaining === null ? <Em what="requests remaining" /> : q.requests_remaining}
      </td>
      <td role="cell" data-label={RATE_LIMITS} className="is-num">{q.rate_limit_count}</td>
      <td role="cell" data-label="Last 429">
        {q.last_429_at ? timeAgo(q.last_429_at, now) : <Em what="last 429" />}
      </td>
      <td role="cell" data-label="Reported">
        {q.updated_at ? timeAgo(q.updated_at, now) : <Em what="reported" />}
      </td>
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
 * Four `<dt>`/`<dd>` pairs, as three topics -- and a fourth for the row's own
 * fields.
 *
 * "Six states, not five", "unknown is not healthy" and "a quota cap of 0 is a
 * fact" were three statements of one thing: which state a row is in is the
 * measurement, and only one kind of zero on this screen means anything.
 * They are one topic now. The others are the ones that make an ABSENCE here
 * mean something specific.
 *
 * `quota-row-fields` (CP-8, CP-10, #85) is what the two renamed columns name
 * and cannot finish saying: that the cap is one input to the pool beside it,
 * and where a rate-limit run starts and ends. It is in the footer index
 * rather than a `?` in the table, which is route 3 of B7.4.
 */
const QUOTA_TOPICS: readonly TopicId[] = [
  'provider-quota-states',
  'quota-row-fields',
  'quota-document-absent',
  'absent-vs-zero',
]
