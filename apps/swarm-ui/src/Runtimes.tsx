import { loadRuntimeTopology, type ResourceClasses, type RuntimeTopology } from './api'
import { isPaused } from './fetch'
import type { TopicId } from './help'
import { HelpCard, HelpLinks } from './HelpCard'
import { Screen } from './Shell'
import {
  humaniseUntil,
  overCeiling,
  poolKind,
  type Pool,
  type ResourceClassSpec,
  type Runtime,
} from './types'

/**
 * THE RUNTIME TOPOLOGY -- what kinds of agent this platform can run, where each
 * one runs, and how big one is.
 *
 * WHY THE SCREEN EXISTS. Invariant 10 says a caller picks a `runner_profile` by
 * NAME and supplies nothing else -- no image, no command, no resource spec, no
 * backend. That is what stops an authenticated caller turning the swarm into
 * arbitrary compute, and it is not negotiable. Its cost is that the name is the
 * caller's entire vocabulary: `GET /v1/runtimes` was built to pay that cost and
 * then nothing rendered it, so the facts it serves stayed where they were
 * before -- in a Python file that the person calling the API does not have.
 *
 * WHAT IT DOES NOT CLAIM TO BE. It is not a node view and not an execution
 * inventory. swarm-api's service account holds no `run.*` and no `container.*`
 * permission, the reconciler's backend inventory has no route, and nothing in
 * this platform reads GKE nodes at all (docs/web-ui/02-cluster-state.md §1,
 * P5/P6). The topology that CAN be drawn honestly is the dispatch topology: the
 * backends the platform routes to, which runtimes land on each, and how loaded
 * each backend's pool is. That is what the first panel draws, and the lead says
 * so rather than letting "topology" imply machines.
 *
 * THE RULE THIS FILE IS MOST AT RISK OF BREAKING. Not one figure here may be
 * hand-written. Resource-class weights, sizes, profile names, backend
 * identifiers and timeouts all live in the frozen catalogue;
 * `check-contract-parity.sh` holds the shell and jq restatements of it to the
 * Python and does not read TypeScript, so a copy in this file would drift the
 * first time a class is resized and nothing would notice. `RESOURCE_UNITS` in
 * types.ts is that mistake already made once and recorded in
 * docs/contract-change-requests.md. Everything below is therefore derived from
 * the response -- including the "what sets it apart" lines, which are
 * COMPARISONS ACROSS THE RESPONSE rather than prose about names this file
 * recognises. A catalogue that gains an entry gains a correct row here without
 * anyone editing this file.
 */
export function RuntimesScreen() {
  return (
    <Screen
      title="Runtimes"
      load={loadRuntimeTopology}
      summary={(d) => {
        const runtimes = Object.values(d.runtimes)
        const backends = new Set(runtimes.map((r) => r.resolved_backend)).size
        return (
          `${runtimes.length} runtime${runtimes.length === 1 ? '' : 's'} · ` +
          `${backends} backend${backends === 1 ? '' : 's'}`
        )
      }}
      /* The read SUCCEEDED and named no runner profile. Distinct from a failed
         read, which never reaches here, and distinct from the capacity screen's
         empty -- pools and the catalogue are different absences. */
      empty={{
        heading: 'The catalogue came back with no runtimes',
        body: 'The read succeeded and named no runner profile. Nothing can be submitted until one is registered, because a caller picks a runtime by name and the API refuses every name that is not in the catalogue — so this is a real absence, not a failed lookup.',
      }}
    >
      {(d) => <Topology data={d} />}
    </Screen>
  )
}

function Topology({ data }: { data: RuntimeTopology }) {
  const runtimes = Object.values(data.runtimes).sort((a, b) => a.name.localeCompare(b.name))

  return (
    <>
      <p className="conjunction">
        A caller picks a runtime by <strong>name</strong> and supplies nothing
        else.
        <HelpCard topic="runner-profile-by-name" />
      </p>

      <Backends runtimes={runtimes} pools={data.pools} poolsDetail={data.poolsDetail} />
      <Sizing
        runtimes={runtimes}
        classes={data.classes}
        classesDetail={data.classesDetail}
      />
      {runtimes.map((r) => (
        <RuntimeCard key={r.name} runtime={r} all={runtimes} />
      ))}
      <p className="provenance">
        {runtimes.length} runtime{runtimes.length === 1 ? '' : 's'} · the whole
        catalogue this response carried, not a page of it
      </p>
      <HelpLinks topics={RUNTIME_TOPICS} />
    </>
  )
}

/* -------------------------------------------------------------------------
 * The backends
 * ---------------------------------------------------------------------- */

/**
 * Backend pools, keyed by the backend they cap.
 *
 * `poolKind` already owns the pool-naming scheme, so the family is asked of it
 * rather than matched on a prefix restated here; the value is everything after
 * the first colon of whatever name it recognised. null in, null out: a failed
 * capacity read must not arrive at the table as "no pools", which is the one
 * confusion this UI exists to prevent.
 */
function backendPools(pools: Pool[] | null): Map<string, Pool> | null {
  if (pools === null) return null
  const out = new Map<string, Pool>()
  for (const p of pools) {
    if (poolKind(p.name) !== 'backend') continue
    const value = p.name.slice(p.name.indexOf(':') + 1)
    if (value) out.set(value, p)
  }
  return out
}

/**
 * One row per backend the catalogue actually resolves to.
 *
 * GROUPED ON `resolved_backend`, NEVER ON `backend`. A profile is allowed to
 * declare that the platform should choose, and that declaration is not itself
 * somewhere a task can run — grouping on it would invent a backend no
 * dispatcher has. The declared value is not lost: it is shown per runtime,
 * where the distinction between "the profile chose this" and "the platform
 * chose this" is the thing being stated.
 */
function Backends({
  runtimes,
  pools,
  poolsDetail,
}: {
  runtimes: Runtime[]
  pools: Pool[] | null
  poolsDetail: string | null
}) {
  const byBackend = new Map<string, Runtime[]>()
  for (const r of runtimes) {
    const list = byBackend.get(r.resolved_backend) ?? []
    list.push(r)
    byBackend.set(r.resolved_backend, list)
  }
  const groups = Array.from(byBackend.entries()).sort(([a], [b]) => a.localeCompare(b))
  const capped = backendPools(pools)

  return (
    <section className="section panel">
      <h2>
        Backends
        <span className="count-chip">
          {groups.length} dispatch target{groups.length === 1 ? '' : 's'}
        </span>
        {/* Trap E: a number may only sit beside a number of the same scope, so
            the scope is declared rather than left to be inferred. Both numbers
            here are platform-wide and cannot be otherwise — the catalogue route
            reads no tenant document, and a backend pool has no tenant in its
            name. Nothing on this row is "yours"; per-tenant headroom is the
            Runner profiles pane under Pools. */}
        <span className="scope platform">platform-wide</span>
        <HelpCard topic="declared-vs-resolved-backend" />
      </h2>

      {capped === null && (
        <div className="state partial" role="status">
          <h3>
            Pool counters could not be read
            <HelpCard topic="absent-vs-zero" />
          </h3>
          <p>
            The catalogue loaded; the <strong>load</strong> on each backend did
            not: {poolsDetail ?? 'the capacity read did not complete.'}{' '}
            <strong>Those columns are dashes, not zeros.</strong>
          </p>
        </div>
      )}

      <div className="table-wrap">
        <table className="pools">
          <thead>
            <tr>
              <th scope="col">Backend</th>
              <th scope="col" className="n">Runtimes</th>
              {/* "units", never "agents". Admission increments each pool by the
                  resource class's weight, so 8 in use may be four agents of a
                  class that weighs 2. */}
              <th scope="col" className="n">Units in use</th>
              <th scope="col" className="n">Ceiling</th>
              <th scope="col" className="n">Headroom</th>
              <th scope="col">Status</th>
            </tr>
          </thead>
          <tbody>
            {groups.map(([backend, members]) => (
              <BackendRow
                key={backend}
                backend={backend}
                members={members}
                pool={capped === null ? null : (capped.get(backend) ?? null)}
                /* A missing MAP and a missing ENTRY are different facts and get
                   different cells: the first is a failed read, the second is a
                   backend nobody has ever given a ceiling. */
                unread={capped === null}
              />
            ))}
          </tbody>
        </table>
      </div>

      <p className="muted small">
        Grouped by <em>resolved</em> backend · the full pool board is under
        Pools.
      </p>
    </section>
  )
}

function BackendRow({
  backend,
  members,
  pool,
  unread,
}: {
  backend: string
  members: Runtime[]
  pool: Pool | null
  unread: boolean
}) {
  const paused = pool !== null && isPaused(pool)
  // Drift, not fullness: admission cannot put more units in a pool than its
  // ceiling allows, so `active > effective_limit` means a limit was lowered
  // under running work or a slot was never released. Capacity says the same
  // thing about the same pools, and the two must not disagree -- so the same
  // exported predicate decides it in both places.
  const over = pool !== null && overCeiling(pool)
  // A ceiling of zero admits nothing however empty it is. Without this it
  // reads "ok", which is the wrong answer to "can anything start here".
  const shut = pool !== null && !paused && !over && pool.effective_limit === 0
  const full =
    pool !== null && !paused && !over && !shut && pool.active >= pool.effective_limit
  // An unread cell and an unconfigured pool both print a dash, and the two mean
  // different things, so the title says which one this dash is.
  const why = unread
    ? 'The capacity read failed, so this was not measured.'
    : 'No pool of this name is configured, so nothing caps this backend.'
  const dash = <span className="ctl-em" title={why}>—</span>

  return (
    <tr className={over ? 'over' : paused ? 'paused' : full || shut ? 'full' : undefined}>
      <th scope="row" className="pool-name">
        {/* The identifier the API sent, verbatim. A prettified display name
            would be a two-entry table keyed on a frozen enum, and a backend
            this UI has never heard of would render as a blank instead of
            naming itself. */}
        <span className="mono">{backend}</span>
      </th>
      <td className="n">{members.length}</td>
      <td className="n">{pool === null ? dash : pool.active}</td>
      <td className="n">{pool === null ? dash : pool.effective_limit}</td>
      <td className="n">{pool === null ? dash : pool.available}</td>
      <td>
        <span className="tags">
          {unread && <span className="tag unknown" title={why}>not read</span>}
          {!unread && pool === null && (
            <span className="tag ok" title={why}>uncapped</span>
          )}
          {paused && (
            <span className="tag paused" title="An operator paused this pool. It admits nothing until resumed, whatever its headroom says.">
              paused
            </span>
          )}
          {over && (
            <span
              className="tag full"
              title={`${pool?.active} units are held against a ceiling of ${pool?.effective_limit}. Admission cannot produce that, so it is drift: a limit lowered under running work, or a slot never released. 'make pool-check' finds these.`}
            >
              over ceiling
            </span>
          )}
          {shut && (
            <span className="tag capped" title="The ceiling on this backend is zero, so nothing can start here however empty it looks.">
              admits nothing
            </span>
          )}
          {full && <span className="tag capped">full</span>}
          {pool !== null && !paused && !over && !shut && !full && (
            <span className="tag ok">ok</span>
          )}
        </span>
      </td>
    </tr>
  )
}

/* -------------------------------------------------------------------------
 * One runtime
 * ---------------------------------------------------------------------- */

/**
 * A ceiling in seconds, as a person reads it.
 *
 * `humaniseUntil` is reused rather than the arithmetic repeated. Zero and
 * negative are not ceilings this platform can express, so they print as an
 * absent value rather than as "now" — the word that helper returns for a
 * deadline that has passed, which would be a nonsense answer to "how long may
 * this run".
 */
function ceilingLabel(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return '—'
  return humaniseUntil(seconds * 1000)
}

/**
 * True when `value` is the single highest (or lowest) of `all`.
 *
 * A TIE DISTINGUISHES NOTHING, and is reported as nothing rather than as a
 * half-truth: telling a reader a runtime has "the longest ceiling" when two
 * share it sends them to the wrong one. A catalogue of fewer than two entries
 * has no comparisons to make at all.
 */
function soleExtreme(value: number, all: number[], want: 'max' | 'min'): boolean {
  if (!Number.isFinite(value) || all.length < 2) return false
  const best = want === 'max' ? Math.max(...all) : Math.min(...all)
  return value === best && all.filter((n) => n === best).length === 1
}

/**
 * What this runtime specialises in, DERIVED rather than described.
 *
 * The owner asked what each runtime specialises in. Nothing in the response is
 * a description, and writing one per name would be a table of prose keyed on
 * the frozen catalogue — stale the day a profile is added, and invisible when
 * it goes stale. What the response does support is comparison: every line below
 * is this runtime measured against the others in the same payload, so it stays
 * true for a catalogue nobody has told this file about.
 *
 * An empty list is returned as an empty list and said out loud by the caller.
 * "Nothing here separates it from the rest" is a finding; a blank line is not.
 */
function distinguishing(r: Runtime, all: Runtime[]): string[] {
  const facts: string[] = []

  const backends = new Set(all.map((x) => x.resolved_backend))
  if (backends.size > 1) {
    const here = all.filter((x) => x.resolved_backend === r.resolved_backend)
    facts.push(
      here.length === 1
        ? `the only runtime this platform dispatches to ${r.resolved_backend}`
        : `one of ${here.length} routed to ${r.resolved_backend}`,
    )
  }

  const sameImage = all.filter((x) => x.image === r.image)
  if (sameImage.length === 1 && all.length > 1) {
    facts.push(`the only runtime built from ${r.image}`)
  }

  if (r.provider === null) {
    facts.push('spends no provider quota, so it runs for a tenant that has registered no credential at all')
  } else {
    const samePv = all.filter((x) => x.provider === r.provider)
    if (samePv.length === 1 && all.length > 1) {
      facts.push(`the only runtime that spends ${r.provider} quota`)
    }
  }

  if (r.secrets_any_of && r.secrets.length > 1) {
    facts.push(`takes any ONE of its ${r.secrets.length} credentials, never all of them`)
  }

  const timeouts = all.map((x) => x.timeout_seconds)
  if (soleExtreme(r.timeout_seconds, timeouts, 'max')) {
    facts.push(`the longest ceiling in this catalogue, at ${ceilingLabel(r.timeout_seconds)}`)
  } else if (soleExtreme(r.timeout_seconds, timeouts, 'min')) {
    facts.push(`the shortest ceiling in this catalogue, at ${ceilingLabel(r.timeout_seconds)}`)
  }

  const weights = all.map((x) => x.resources.units)
  if (soleExtreme(r.resources.units, weights, 'max')) {
    facts.push(`the heaviest in this catalogue, at ${r.resources.units} units per agent`)
  } else if (soleExtreme(r.resources.units, weights, 'min')) {
    facts.push(`the lightest in this catalogue, at ${r.resources.units} units per agent`)
  }

  return facts
}

function RuntimeCard({ runtime, all }: { runtime: Runtime; all: Runtime[] }) {
  const facts = distinguishing(runtime, all)
  // A profile that let the platform choose is the case this exists for: the
  // declared value and the resolved one then answer different questions, and
  // when they agree there is only one question to answer.
  const platformChose = runtime.backend !== runtime.resolved_backend

  return (
    <section className="section panel">
      <h2>
        {/* `.section > h2` uppercases, and this is an identifier: the string
            here is the exact `runner_profile` a caller sends, so an uppercased
            one would be a name nobody can copy. Overridden locally rather than
            in styles.css, which every other screen shares. */}
        <span className="mono" style={{ textTransform: 'none' }}>{runtime.name}</span>
        <span className="count-chip">{runtime.resolved_backend}</span>
      </h2>

      <dl className="kv">
        <dt>Runs on</dt>
        <dd className="mono">
          {runtime.resolved_backend}
          {platformChose && (
            <span className="client-side" title={`The profile declares ${runtime.backend}; resolve_backend turned it into ${runtime.resolved_backend}.`}>
              {' '}· declared {runtime.backend}, resolved by the platform
            </span>
          )}
        </dd>

        <dt>Image</dt>
        <dd className="mono">{runtime.image}</dd>

        <dt>
          Credential
          <HelpCard topic="credential-names-not-values" />
        </dt>
        <dd>
          <Credential runtime={runtime} />
        </dd>

        <dt>May run for</dt>
        {/* A ceiling, not a measurement. It is what the platform will stop the
            attempt at, and it is the profile's own value -- a submission may
            ask for less and never for more. */}
        <dd>{ceilingLabel(runtime.timeout_seconds)} before the platform stops the attempt</dd>

        <dt>Size</dt>
        <dd>
          <span className="mono">{runtime.resource_class}</span> ·{' '}
          <SizeLine spec={runtime.resources} />
        </dd>

        <dt>
          What sets it apart
          <HelpCard topic="what-sets-it-apart-is-arithmetic" />
        </dt>
        <dd>
          {/* A MEASURED "nothing", not a blank. The comparison ran and found
              no difference; a blank cell here would read as a comparison
              nobody made. */}
          {facts.length === 0 ? (
            <span className="muted">
              Nothing in this response separates it from the rest of the
              catalogue.
            </span>
          ) : (
            facts.join(' · ')
          )}
        </dd>
      </dl>
    </section>
  )
}

/**
 * What a tenant has to have registered before this runtime will run for them.
 *
 * `secrets_any_of` is the field this panel exists to render. A list of two
 * names without it tells a tenant who pays for a subscription that they must
 * also buy metered access — the refusal the flag exists in the frozen catalogue
 * to prevent — so the flag is rendered as WORDS ("any one of"/"all of"), never
 * as a dot, a badge or a tooltip.
 */
function Credential({ runtime }: { runtime: Runtime }) {
  if (runtime.provider === null) {
    return (
      <>
        {/* Not "unknown", and not an em dash: em dash means "not measured", and
            this is measured. The answer is that it needs nothing. */}
        <span className="tag ok">none needed</span>
        <HelpCard topic="runtime-needs-no-provider" />
      </>
    )
  }
  if (runtime.secrets.length === 0) {
    return (
      <>
        <span className="mono">{runtime.provider}</span>{' '}
        {/* THE EM DASH AND THE CLAUSE BOTH STAY. An empty credential cell
            beside a named provider is exactly what a failed read would look
            like, and this is not one. */}
        <span className="muted">— no variable names in the response; not a failed read</span>
        <HelpCard topic="credential-names-not-values" />
      </>
    )
  }
  return (
    <>
      <span className="mono">{runtime.provider}</span> ·{' '}
      {runtime.secrets_any_of ? <strong>any one of</strong> : <strong>all of</strong>}{' '}
      {runtime.secrets.map((s, i) => (
        <span key={s}>
          {i > 0 && ', '}
          <code>{s}</code>
        </span>
      ))}
      <span className="client-side">
        {' '}· names only, never values
      </span>
    </>
  )
}

/* -------------------------------------------------------------------------
 * Sizing
 * ---------------------------------------------------------------------- */

/**
 * One class in one line: what the container gets, and how much of it the
 * workspace may take back.
 *
 * `disk_gib` IS A SLICE OF `memory_gib`, not storage on top of it. The
 * workspace is a memory-backed tmpfs because the Terraform google provider
 * cannot express Cloud Run's disk-backed empty_dir, so an agent that fills its
 * workspace has that much less memory for the process. Printing the two as
 * peers is the misreading this line is written to prevent.
 */
function SizeLine({ spec }: { spec: ResourceClassSpec }) {
  const left = spec.memory_gib - spec.disk_gib
  const coherent = Number.isFinite(left) && left >= 0
  return (
    <>
      {spec.cpu} vCPU · {spec.memory_gib} GiB memory · {spec.units} unit
      {spec.units === 1 ? '' : 's'}
      {' · '}
      {coherent ? (
        <span title="The workspace is a memory-backed tmpfs, so it is carved out of the same memory the process uses.">
          up to {spec.disk_gib} GiB of that memory may go to the workspace, leaving {left} GiB
        </span>
      ) : (
        <span className="ctl-em" title="The workspace figure is larger than the memory it is carved out of, which the platform cannot honour. Reported as served rather than adjusted.">
          workspace {spec.disk_gib} GiB exceeds the {spec.memory_gib} GiB it comes out of
        </span>
      )}
    </>
  )
}

/**
 * Every size this platform offers, and which runtimes reach it.
 *
 * TWO SOURCES, DELIBERATELY. `/v1/resource-classes` is the whole catalogue;
 * `/v1/runtimes` carries only the classes something resolved to. Preferring the
 * first means a class nothing routes to is visible — which is a real finding,
 * since a class no runtime names cannot be asked for by anyone. When that read
 * failed the table is still drawn from the sizes embedded in the runtimes, and
 * it says out loud that it can no longer show an unreachable class rather than
 * quietly showing a shorter list.
 */
function Sizing({
  runtimes,
  classes,
  classesDetail,
}: {
  runtimes: Runtime[]
  classes: ResourceClasses | null
  classesDetail: string | null
}) {
  // KEYED ON ONE FIELD, NOT TWO. `resource_class` and `resources.name` are the
  // same class -- the route resolves the second FROM the first -- and a table
  // that keys its rows on one and its users on the other would print "nothing
  // routes here" beside a class several runtimes use, the moment they ever
  // disagreed. One field, so they cannot.
  const usedBy = new Map<string, string[]>()
  for (const r of runtimes) {
    const list = usedBy.get(r.resource_class) ?? []
    list.push(r.name)
    usedBy.set(r.resource_class, list)
  }

  const fromRoute = classes !== null && Object.keys(classes).length > 0
  const specs: ResourceClassSpec[] = fromRoute
    ? Object.values(classes)
    : // The fallback. Deduplicated on `resource_class` -- the same key `usedBy`
      // above is built with, so a row and its users cannot come apart -- and the
      // size that is paired with it is the one the route resolved from it.
      Array.from(
        new Map(
          runtimes.map((r) => [r.resource_class, { ...r.resources, name: r.resource_class }]),
        ).values(),
      )
  const rows = specs.slice().sort((a, b) => a.units - b.units || a.name.localeCompare(b.name))

  return (
    <section className="section panel">
      <h2>
        Sizing
        <span className="count-chip">
          {rows.length} class{rows.length === 1 ? '' : 'es'}
        </span>
      </h2>

      {!fromRoute && (
        <div className="state partial" role="status">
          <h3>
            The size catalogue could not be read
            <HelpCard topic="catalogue-from-route" />
          </h3>
          <p>
            Sizes below came from the runtimes themselves.{' '}
            <strong>
              A class no runtime resolves to cannot appear in this list.
            </strong>{' '}
            {classesDetail ?? 'The resource-class read did not complete.'}
          </p>
        </div>
      )}

      <div className="table-wrap">
        <table className="pools">
          <thead>
            <tr>
              <th scope="col">Class</th>
              <th scope="col" className="n">vCPU</th>
              <th scope="col" className="n">Memory</th>
              <th scope="col" className="n">Workspace</th>
              <th scope="col" className="n">
                Weight
                <HelpCard topic="units-not-agents" />
              </th>
              <th scope="col">Runtimes that resolve to it</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((spec) => {
              const users = usedBy.get(spec.name) ?? []
              return (
                <tr key={spec.name}>
                  <th scope="row" className="pool-name">
                    <span className="mono">{spec.name}</span>
                  </th>
                  {/* Both the request and the limit: requests == limits
                      platform-wide, so there is no burst headroom above these. */}
                  <td className="n">{spec.cpu}</td>
                  <td className="n">{spec.memory_gib} GiB</td>
                  <td className="n" title="Carved out of the memory beside it, not added to it.">
                    {spec.disk_gib} GiB
                  </td>
                  <td className="n">{spec.units}u</td>
                  <td>
                    {users.length === 0 ? (
                      <span className="muted" title="No runtime in this catalogue resolves to this class, so no caller can reach it. It is configured and unreachable, which is a fact rather than a fault.">
                        nothing routes here
                      </span>
                    ) : (
                      <span className="mono">{users.join(', ')}</span>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

    </section>
  )
}

/**
 * WHAT USED TO BE `Legend()` -- five `<dt>`/`<dd>` pairs, ~230 words, under
 * every load of this screen.
 *
 * The five titles are still on the surface, because they were the index of
 * what this screen's marks mean and deleting the block would have taken the
 * index out with the essay. The paragraphs are at `#help/<id>`, written once
 * and shared with every other screen that used to argue the same point --
 * `workspace-memory` and `requests-are-ceilings` were also spelled out on
 * the agent screen, and `units-not-agents` on four more.
 */
const RUNTIME_TOPICS: readonly TopicId[] = [
  'catalogue-from-route',
  'runner-profile-by-name',
  'not-a-machine-inventory',
  'declared-vs-resolved-backend',
  'workspace-memory',
  'requests-are-ceilings',
  'units-not-agents',
  'credential-names-not-values',
  'what-sets-it-apart-is-arithmetic',
]
