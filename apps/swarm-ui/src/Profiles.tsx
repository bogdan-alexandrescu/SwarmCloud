import { loadCapacity } from './api'
import { isPaused } from './fetch'
import type { TopicId } from './help'
import { HelpLinks } from './HelpCard'
import { Screen } from './Shell'
import { ProfileAdmissionPanel, headroomFigure } from './Blockers'
import { headroomFor, poolLabel, poolScope, type Capacity, type Pool, type RunnerProfile } from './types'

/**
 * The runner-profile catalogue — what kinds of agent this platform can run.
 *
 * Capacity answers "which ceiling is binding right now", pool by pool. This
 * answers the question before it: what may I ask for, what does one of them
 * weigh, and which pools does asking for it touch.
 *
 * Three things it must not get wrong.
 *
 * 1. A profile's `pools` list is the CALLING TENANT'S, including for an admin:
 *    service.capacity() builds it with pool_names_for(tenant_id=ctx.tenant_id)
 *    unconditionally (service.py:313-329). Every count here answers "how many
 *    more could I submit", so the tenant is named in words, repeatedly.
 * 2. A task must clear EVERY pool in its list at the same moment — admission is
 *    all-or-nothing across it (models.py:90-94). Capacity is the MINIMUM across
 *    them, never a sum; the per-pool "fits" column makes that minimum visible
 *    rather than asserted.
 * 3. The catalogue is frozen contract data that can GAIN entries, so everything
 *    below renders whatever arrived: no list of five, no per-name special case,
 *    no client-side weight table.
 */
export function ProfilesScreen() {
  return (
    <Screen
      /* "Profile headroom", NOT "Runner profiles", AND THE HEADING MOVED
         BECAUSE THE TAB DID -- test_nav_headings_agree.py asserts the two are
         one name and would have failed the build otherwise.
         The rename is what pays for the three-section nav putting this screen
         and `Runtimes.tsx` in one section. Those two were kept in SEPARATE
         sections for exactly this reason: "Runtimes" and "Runner profiles" are
         near-synonyms answering different questions, and two adjacent tabs
         with near-synonymous labels is how a reader takes the per-tenant
         figures on THIS screen for the platform-wide ones on that one. Every
         count here is built from `pool_names_for(tenant_id=ctx.tenant_id)`
         unconditionally, admin included (service.py:313-329); nothing on the
         runtime topology reads a tenant document at all. "Headroom" is the
         word this product already uses for a measurement of one tenant against
         a ceiling, so the label now says which of the two questions this
         screen answers instead of leaving it to be inferred from the section.
         The route is still `#capacity/profiles`: `runner_profile` is the
         contract's field name and invariant 10 is why this screen exists, so
         the address keeps the contract's noun. */
      title="Profile headroom"
      load={loadCapacity}
      summary={(d) => {
        const profiles = Object.values(d.runner_profiles)
        const backends = new Set(profiles.map((p) => p.backend)).size
        return `${profiles.length} profiles · ${backends} backend${backends === 1 ? '' : 's'}`
      }}
      /* loadCapacity calls a response empty when `pools` is empty, not when the
         catalogue is — so this copy names THAT query. A response with pools and
         no profiles never reaches here; Catalogue below renders that case. */
      empty={{
        heading: 'The capacity read returned no pools',
        body: 'The read succeeded and its pool list was empty. Pools are created at provisioning time, so profiles cannot be priced against anything yet — this is a real absence, not a failed lookup.',
      }}
    >
      {(d) => <Catalogue capacity={d} />}
    </Screen>
  )
}

/**
 * Whose view this is, taken from the PROFILES' pool lists rather than the
 * top-level pool list.
 *
 * It matters which. An admin sees OTHER tenants' pools in `capacity.pools`
 * (service.py:299-306 filters them for non-admins only) and they sort ahead of the
 * caller's — `provider:anthropic:tenant:eng` precedes `tenant:u-bogdan` — so reading
 * the first tenant-ish name out of that list names the wrong tenant for exactly the
 * reader most likely to be misled. A profile's list is built for ctx.tenant_id alone
 * and always carries `tenant:<id>` (models.py:95-101). `slice`, not a `[^:]+` pattern:
 * an id truncated at a colon is a different id.
 */
function tenantOf(capacity: Capacity): string | null {
  for (const profile of Object.values(capacity.runner_profiles)) {
    for (const name of profile.pools) {
      if (name.startsWith('tenant:')) return name.slice('tenant:'.length)
    }
  }
  return null
}

function Catalogue({ capacity }: { capacity: Capacity }) {
  const entries = Object.entries(capacity.runner_profiles).sort(([a], [b]) => a.localeCompare(b))
  const byName = new Map(capacity.pools.map((p) => [p.name, p]))
  const tenant = tenantOf(capacity)

  // A read that succeeded with an empty catalogue. Not the Screen's `empty`,
  // which can only mean "no pools", and not a failure — so nothing here is red.
  if (entries.length === 0) {
    return (
      <section className="section panel">
        <h2>The catalogue came back with no entries</h2>
        <p className="muted">
          The capacity read succeeded and returned {capacity.pools.length} pools, so
          this is not a failed query — <code>runner_profiles</code> itself arrived
          empty, and nothing can be submitted until a profile is registered.
        </p>
      </section>
    )
  }

  return (
    <>
      <p className="conjunction">
        A task must clear <strong>every</strong> pool in its profile&apos;s list at
        the same moment. Capacity is the <strong>minimum</strong> across them, never a sum.
      </p>
      <p className="muted">
        These pool lists are{' '}
        {tenant ? <>for tenant <strong>{tenant}</strong></> : <>for your own tenant</>}, including if
        you are an admin. They describe what a task <em>you</em> submit has to clear — not
        what the platform as a whole can run.
      </p>
      {entries.map(([name, profile]) => (
        <ProfileCard key={name} name={name} profile={profile} byName={byName} tenant={tenant} capacity={capacity} />
      ))}
      <p className="provenance">
        {entries.length} profiles · the whole catalogue this response carried, not a page of it
      </p>
      <HelpLinks topics={PROFILE_TOPICS} />
    </>
  )
}

function ProfileCard({ name, profile, byName, tenant, capacity }: {
  name: string
  profile: RunnerProfile
  /** Every pool this caller may see, by name. A name absent from it is uncapped. */
  byName: ReadonlyMap<string, Pool>
  tenant: string | null
  capacity: Capacity
}) {
  // Read, not computed. `profile.admission` is what swarm_api/headroom.py got
  // out of `evaluate_capacity`; this card used to re-derive it and kept only
  // the running minimum, which is how two pools refusing at once became one
  // name on the screen.
  const head = headroomFor(profile)
  const figure = headroomFigure(head)
  const weight = profile.units > 0 ? profile.units : 1
  const rows = profile.pools.map((pool) => ({ pool, row: byName.get(pool) ?? null }))
  const uncapped = rows.filter((r) => r.row === null).length
  // EVERY pool refusing this profile, and every pool capping it. Two sets,
  // because "refusing now" and "would run out first" are different facts and
  // the row tags have to say which one a pool is.
  const refusing = new Set(head.blockers.map((b) => b.pool))
  const unread = new Set(head.unread)

  return (
    <section className="section panel">
      <h2>
        {/* `.section > h2` uppercases, and this is an identifier: `claude-code`
            is the exact string a caller sends as runner_profile, so CLAUDE-CODE
            would be a name nobody can copy. Overridden here rather than in
            styles.css, where it would change every other screen's headings. */}
        <span className="mono" style={{ textTransform: 'none' }}>{name}</span>
        {/* Three cases, not two. A measured count, a profile nothing caps (no
            number exists), and a profile with an unread pool (a number exists
            and we do not have it). The last two both render an em dash and
            must not be worded the same. */}
        <span className="count-chip" title={figure.title}>
          {head.agents !== null
            ? `${head.agents} could start for ${tenant ?? 'your tenant'}`
            : head.basis === 'uncapped'
              ? 'nothing here caps this'
              : 'not measured — a pool could not be read'}
        </span>
      </h2>

      <dl className="kv">
        <dt>Backend</dt>
        <dd className="mono">{profile.backend}</dd>
        <dt>Provider</dt>
        {/* null is not "unknown": the profile needs no provider key at all, so
            no provider pool exists and no provider quota can block it. */}
        <dd className="mono">{profile.provider ?? <span title="Uses no provider">&mdash;</span>}</dd>
        <dt>Resource class</dt>
        <dd className="mono">{profile.resource_class}</dd>
        {/* Weight straight from the payload. RESOURCE_UNITS in types.ts is a
            bundled copy of the same table and can drift; the response has
            already resolved the class to units, so that is what is shown. */}
        <dt>Weight</dt>
        <dd>
          {profile.units} unit{profile.units === 1 ? '' : 's'} per agent, added to every pool below on admission
        </dd>
      </dl>

      {/* `is-stacked` — F6 OF `docs/audits/2026-09-23/overflow-inventory.md`.
          At 390pt this table measured `clientWidth: 358` against a
          `scrollWidth` of 543-615 across its five instances: 34-42% of the
          columns were behind an `overflow-x: auto` that paints no scrollbar on
          this platform, so `Units free`, `Fits` and `Status` were simply not
          there and nothing said they existed. Below 900px each row becomes a
          stacked record with its own key column (§B6.3 in `styles.css`), which
          is what `data-label` supplies — as an attribute rather than a second
          element per cell, so that the rendered-word budgets count the same
          screen they always did.

          The explicit `role`s are not decoration either: changing `display` on
          a table element drops its implicit ARIA role in every browser, so
          without them a stacked table is a pile of anonymous blocks to a screen
          reader. They are the roles these elements already have. */}
      <div className="table-wrap is-stacked">
        <table className="pools" role="table">
          <thead role="rowgroup">
            <tr role="row">
              <th role="columnheader" scope="col">Pool it must clear</th>
              <th role="columnheader" scope="col">Scope</th>
              {/* "units", never "agents": admission increments by the class's
                  weight, so 8 in use may be four browser agents. */}
              <th role="columnheader" scope="col" className="n">Units free</th>
              <th role="columnheader" scope="col" className="n">Fits</th>
              <th role="columnheader" scope="col">Status</th>
            </tr>
          </thead>
          <tbody role="rowgroup">
            {rows.map(({ pool, row }) => {
              const paused = row !== null && isPaused(row)
              // Every refusing pool is marked, not just the tightest one.
              const blocking = refusing.has(pool)
              const capping = head.binding === pool || blocking
              const notRead = unread.has(pool)
              const scope = poolScope(pool)
              return (
                <tr role="row" key={pool} className={paused ? 'paused' : blocking ? 'full' : undefined}>
                  <th role="rowheader" scope="row" className="pool-name" title={pool}>
                    {poolLabel(pool)}
                    <span className="raw">{pool}</span>
                  </th>
                  <td role="cell" data-label="Scope">
                    <span className={`scope ${scope}`}>{scope === 'platform' ? 'platform-wide' : 'this tenant'}</span>
                  </td>
                  <td role="cell" data-label="Units free" className="n">{row === null ? '—' : row.available}</td>
                  {/* A paused pool fits 0 however much headroom it reports &mdash; that is
                      a fact admission enforces, not a missing value coalesced to zero. */}
                  <td role="cell" data-label="Fits" className="n">{row === null ? '—' : paused ? 0 : Math.floor(Math.max(0, row.available) / weight)}</td>
                  <td role="cell" data-label="Status">
                    <span className="tags">
                      {notRead && (
                        <span className="tag unknown" title="This pool could not be read. It is not uncapped and it is not empty: nothing is known about it, and it may be the one refusing.">not read</span>
                      )}
                      {row === null && !notRead && (
                        <span className="tag ok" title="No pool of this name is configured, so nothing caps it. The global and tenant pools always exist, so this is a narrow named pool that was never given a limit.">uncapped</span>
                      )}
                      {paused && (
                        <span className="tag paused" title="An operator paused this pool. It admits nothing until resumed, whatever its headroom says &mdash; raising its limit changes nothing.">paused</span>
                      )}
                      {blocking && !paused && (
                        <span className="tag full" title="At its ceiling right now: this pool is refusing the next task of this profile.">full</span>
                      )}
                      {capping && !blocking && <span className="tag capped">binding</span>}
                    </span>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      <p className="muted small">
        {head.agents === null
          ? head.basis === 'uncapped'
            ? 'No pool in this list is configured, so nothing here limits this profile and there is no number to report.'
            : 'A pool in this list could not be read, so there is no number to report. That is not the same as zero.'
          : head.agents === 0
            ? `Nothing more of this profile can start: ${head.blockers.length} of these ${rows.length} pools ${head.blockers.length === 1 ? 'is' : 'are'} refusing it right now.`
            : `${head.agents} more could start; ${head.binding ? poolLabel(head.binding) : 'one of these pools'} would run out first.`}
        {uncapped > 0 && head.basis === 'measured' &&
          ` ${uncapped} of these ${rows.length} pools ${uncapped === 1 ? 'is' : 'are'} unconfigured and constrains nothing.`}
      </p>

      {/* The object that is stuck, on its own page: every reason it is stuck,
          grouped by what would clear it, and what lifting each ceiling would
          have bought at the last read. */}
      <ProfileAdmissionPanel profile={profile} capacity={capacity} />
    </section>
  )
}

/**
 * The two `<dt>`/`<dd>` pairs this screen used to end on, as links.
 *
 * Both were arguments made on other screens too -- `units-not-agents` on six
 * files and `runner-profile-by-name` on three -- so collapsing them into one
 * topic each is where the page actually gets shorter rather than merely
 * tidier.
 */
const PROFILE_TOPICS: readonly TopicId[] = [
  'units-not-agents',
  'runner-profile-by-name',
  'pools-all-at-once',
  'tenant-scope',
]
