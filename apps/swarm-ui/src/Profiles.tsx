import { loadCapacity } from './api'
import { isPaused } from './fetch'
import { Screen } from './Shell'
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
      title="Runner profiles"
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
        <ProfileCard key={name} name={name} profile={profile} byName={byName} tenant={tenant} />
      ))}
      <p className="provenance">
        {entries.length} profiles · the whole catalogue this response carried, not a page of it
      </p>
      <Legend />
    </>
  )
}

function ProfileCard({ name, profile, byName, tenant }: {
  name: string
  profile: RunnerProfile
  /** Every pool this caller may see, by name. A name absent from it is uncapped. */
  byName: ReadonlyMap<string, Pool>
  tenant: string | null
}) {
  const head = headroomFor(profile, byName)
  // The guard headroomFor applies, repeated so a per-pool "fits" can never
  // disagree with the headline it is supposed to explain.
  const weight = profile.units > 0 ? profile.units : 1
  const rows = profile.pools.map((pool) => ({ pool, row: byName.get(pool) ?? null }))
  // Counted here rather than read off head.missing: headroomFor returns early
  // on the first paused pool, so its `missing` stops wherever that happened.
  const uncapped = rows.filter((r) => r.row === null).length

  return (
    <section className="section panel">
      <h2>
        {/* `.section > h2` uppercases, and this is an identifier: `claude-code`
            is the exact string a caller sends as runner_profile, so CLAUDE-CODE
            would be a name nobody can copy. Overridden here rather than in
            styles.css, where it would change every other screen's headings. */}
        <span className="mono" style={{ textTransform: 'none' }}>{name}</span>
        {/* headroomFor returns agents: 0 when NOTHING constrained it, because
            it will not report an unbounded number. Printing that 0 beside a
            profile nothing is limiting would read as blocked, so that case says
            so in words instead. */}
        <span className="count-chip">
          {head.binding === null
            ? 'nothing here caps this'
            : `${head.agents} could start for ${tenant ?? 'your tenant'}`}
        </span>
      </h2>

      <dl className="kv">
        <dt>Backend</dt>
        <dd className="mono">{profile.backend}</dd>
        <dt>Provider</dt>
        {/* null is not "unknown": the profile needs no provider key at all, so
            no provider pool exists and no provider quota can block it. */}
        <dd className="mono">{profile.provider ?? <span title="Uses no provider">—</span>}</dd>
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

      <div className="table-wrap">
        <table className="pools">
          <thead>
            <tr>
              <th scope="col">Pool it must clear</th>
              <th scope="col">Scope</th>
              {/* "units", never "agents": admission increments by the class's
                  weight, so 8 in use may be four browser agents. */}
              <th scope="col" className="n">Units free</th>
              <th scope="col" className="n">Fits</th>
              <th scope="col">Status</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ pool, row }) => {
              const paused = row !== null && isPaused(row)
              const binding = head.binding === pool
              const scope = poolScope(pool)
              return (
                <tr key={pool} className={paused ? 'paused' : binding ? 'full' : undefined}>
                  <th scope="row" className="pool-name" title={pool}>
                    {poolLabel(pool)}
                    <span className="raw">{pool}</span>
                  </th>
                  <td>
                    <span className={`scope ${scope}`}>{scope === 'platform' ? 'platform-wide' : 'this tenant'}</span>
                  </td>
                  <td className="n">{row === null ? '—' : row.available}</td>
                  {/* A paused pool fits 0 however much headroom it reports — that is
                      a fact admission enforces, not a missing value coalesced to zero. */}
                  <td className="n">{row === null ? '—' : paused ? 0 : Math.floor(Math.max(0, row.available) / weight)}</td>
                  <td>
                    <span className="tags">
                      {row === null && (
                        <span className="tag ok" title="No pool of this name is configured, so nothing caps it. The global and tenant pools always exist, so this is a narrow named pool that was never given a limit.">uncapped</span>
                      )}
                      {paused && (
                        <span className="tag paused" title="An operator paused this pool. It admits nothing until resumed, whatever its headroom says.">paused</span>
                      )}
                      {binding && <span className="tag capped">binding</span>}
                    </span>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      <p className="muted small">
        {head.binding === null
          ? 'No pool in this list is configured, so nothing here limits this profile and there is no number to report.'
          : head.agents === 0
            ? `Nothing more of this profile can start: ${poolLabel(head.binding)} is holding it at zero.`
            : `${head.agents} more could start; ${poolLabel(head.binding)} is the first pool that would run out.`}
        {uncapped > 0 && head.binding !== null &&
          ` ${uncapped} of these ${rows.length} pools ${uncapped === 1 ? 'is' : 'are'} unconfigured and constrains nothing.`}
      </p>
    </section>
  )
}

function Legend() {
  return (
    <section className="section legend">
      <h2>Reading this screen</h2>
      <dl>
        <dt>Units, not agents</dt>
        <dd>
          Every number in the pool columns is weighted units. A profile&apos;s weight
          is what one agent of it adds to each of its pools, which is why &ldquo;fits&rdquo;
          is free units divided by that weight rather than the free units themselves.
        </dd>
        <dt>Callers pick a profile by name</dt>
        <dd>
          The API accepts <code>runner_profile</code> and nothing else — never an image,
          a command, a resource spec or a backend. What a name <em>means</em> — its image,
          its declared and resolved backend, its credentials, its ceiling and its size — is
          served by <code>/v1/runtimes</code> and drawn under Runtimes. Only the command is
          served nowhere, so it is absent here rather than copied in.
        </dd>
      </dl>
    </section>
  )
}
