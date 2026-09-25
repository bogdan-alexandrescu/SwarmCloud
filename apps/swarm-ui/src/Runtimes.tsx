import { useId } from 'react'
import { loadRuntimeTopology, type ResourceClasses, type RuntimeTopology } from './api'
import { isPaused } from './fetch'
import type { TopicId } from './help'
import { HelpCard, HelpLinks } from './HelpCard'
import { Id, Screen } from './Shell'
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
 * each backend's pool is.
 *
 * THE RULE THIS FILE IS MOST AT RISK OF BREAKING. Not one figure here may be
 * hand-written. Resource-class weights, sizes, profile names, backend
 * identifiers and timeouts all live in the frozen catalogue;
 * `check-contract-parity.sh` holds the shell and jq restatements of it to the
 * Python and does not read TypeScript, so a copy in this file would drift the
 * first time a class is resized and nothing would notice. `RESOURCE_UNITS` in
 * types.ts is that mistake already made once and recorded in
 * docs/contract-change-requests.md. Everything below is therefore derived from
 * the response -- including the "what sets it apart" facts, which are
 * COMPARISONS ACROSS THE RESPONSE rather than prose about names this file
 * recognises. A catalogue that gains an entry gains a correct row here without
 * anyone editing this file.
 *
 * ---------------------------------------------------------------------------
 * B4.4: THE TWO LEADS AND THE DEFINITION LIST.
 *
 * This screen opened on a sentence, closed on a provenance sentence, and put
 * every per-runtime fact in a `<dl>` whose `<dd>`s were clauses. ~120 words.
 * Where each one went:
 *
 *   "a caller picks a runtime by NAME and supplies nothing else" -- this is
 *   invariant 10 and it is the reason the screen exists, so it did not get
 *   deleted. It is `#help/runner-profile-by-name`, in the footer index below
 *   and on the submit form's first step, where a caller meets the constraint.
 *   It had the `?` beside the catalogue's own heading until B7.4, which gave
 *   that glyph to `catalogue-from-route` instead: with one `?` to spend on
 *   this screen, what the list IS beats what a caller may pass to it, because
 *   the second is stated by the submit form at the moment it matters. It is an
 *   argument, not a datum, and §8.4(5) is where arguments live.
 *
 *   "up to N GiB of that memory may go to the workspace, leaving M GiB" --
 *   the WORKSPACE COLUMN IS NAMED `Workspace (of memory)`. §8.4(3). The clause
 *   existed to stop a reader adding the two figures together; a column name
 *   that says "of" does that without a verb, and does it for every row at once
 *   rather than once per runtime card.
 *
 *   "X before the platform stops the attempt" -- the fact key is `MAX RUN`.
 *   §8.4(1): a well-chosen label is the explanation.
 *
 *   "the only runtime this platform dispatches to cloudrun" etc. -- these are
 *   `.ctl-facts` entries: a two-or-three-character mono key and a one-or-two
 *   word value. The COMPARISON is unchanged and still derived from the
 *   response; only its phrasing is gone. The topic
 *   `what-sets-it-apart-is-arithmetic` says what the comparison is.
 *
 *   "Those columns are dashes, not zeros" -- `.ctl-mark.is-unread` on the
 *   card, and an em dash with no digit in every affected cell. The mark is on
 *   the card whose figures are missing; the sentence was in a banner above it
 *   and could be scrolled away from them.
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
        body: (
          <>
            <span className="ctl-mark is-zero">real zero</span> nothing can be submitted until
            one is registered
          </>
        ),
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
      <Backends runtimes={runtimes} pools={data.pools} poolsDetail={data.poolsDetail} />
      <Sizing
        runtimes={runtimes}
        classes={data.classes}
        classesDetail={data.classesDetail}
      />

      <div className="ctl-toolbar">
        {/* THIS SCREEN'S ONE ALWAYS-PRESENT `?` (B7.4).
            It was ten: invariant 10 here, the resolved-backend rule on the
            Backends heading, the unit on a column whose own header already
            said `(units)`, the workspace basis on a column that already said
            `(of memory)`, and five more. Every one of them is a standing
            argument about the catalogue as a whole rather than about the thing
            it was pinned to, so all ten are the footer index below and this is
            the one that stays: what this list IS and where it comes from, which
            is the question the other nine are downstream of.
            It sits on the eyebrow because the eyebrow renders on every path
            through this screen -- a rationed `?` is only learnable if it is in
            the same place every time, and a glyph that appears only when a read
            fails is one nobody has learned to look for. */}
        <span className="ctl-eyebrow rt-eyebrow">
          The catalogue
          <HelpCard topic="catalogue-from-route" />
        </span>
        <span className="ctl-card-note is-end">
          {runtimes.length} of {runtimes.length} · whole catalogue
        </span>
      </div>

      <div className="ctl-cards">
        {runtimes.map((r) => (
          <RuntimeCard key={r.name} runtime={r} all={runtimes} />
        ))}
      </div>

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
 * somewhere a task can run -- grouping on it would invent a backend no
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
    /* §B6.1: THE PANEL IS THE TABLE, AND THE TITLE IS ON THE PAGE.
       This was `.ctl-card > .ctl-card-head + .ctl-card-body.is-flush >
       .ctl-table`, which drew two concentric boxes around one table and put
       the heading inside the outer one — so a panel title on this screen sat
       at a different level from the panel title on `pools/quota` and
       `admin/limits`, which already put theirs on the page. Three of the five
       reference consoles put a table's heading on the page and draw exactly
       one box, around the data. That construction is now the only one these
       seven screens use. The provenance line that was a `.ctl-card-foot` is a
       `<caption>`, which is the same slot inside the one box that is left. */
    <section className="section rt-backends">
      <div className="ctl-toolbar">
        {/* THE BASIS MOVED INTO THE COLUMN NAME, and the `?` went with the
            paragraph. `Backend (resolved)` is what the glyph here was holding:
            these rows group on `resolved_backend` and never on the declared
            one. In the heading it was a claim about the whole card; in the
            column name it cannot be scrolled away from the rows it governs and
            cannot be read as qualifying the next column along.
            `#help/declared-vs-resolved-backend` is in the footer index. */}
        <h2 className="ctl-card-title">Backends</h2>
        {/* Trap E: a number may only sit beside a number of the same scope, so
            the scope is declared rather than left to be inferred. Both numbers
            here are platform-wide and cannot be otherwise -- the catalogue route
            reads no tenant document, and a backend pool has no tenant in its
            name. Nothing on this row is "yours"; per-tenant headroom is the
            Profile headroom pane, one tab along under Capacity. That pane used
            to be called "Runner profiles" and used to live in a different
            section from this screen; both changed together, because a tab
            named "Runner profiles" beside one named "Runtimes" is how the two
            scopes get read as one. */}
        <span className="ctl-card-note is-end">
          {groups.length} target{groups.length === 1 ? '' : 's'} · platform-wide
        </span>
      </div>

      {/* THE LOAD COLUMNS ARE UNREAD, AND THE MARK SAYS SO ON THE CARD THAT
          CARRIES THEM. Not a banner above the table: a banner can be scrolled
          away from the figures it qualifies, and a reader who lands on row
          nine has no qualifier at all. Every affected cell is also an em dash
          with no digit in it, which is the fact the mark indexes. */}
      {capped === null && (
        <div className="rt-unread">
          {/* AH-24: NEVER AFTER A VALUE. The glyph trailed the server's own
              words, inside a one-line detail that clips with an ellipsis, so
              it was after a value and cut off with it when the message ran
              long. The row has no label of its own -- its first item is the
              mark -- so the glyph leads the row. */}
          <HelpCard topic="absent-vs-zero" />
          <span className="ctl-mark is-unread">not read</span>
          <span className="rt-unread-detail">
            {poolsDetail ?? 'the capacity read did not complete'}
          </span>
        </div>
      )}

      <div className="ctl-table is-stacked">
        <table role="table">
            <thead role="rowgroup">
              <tr role="row">
                <th role="columnheader" scope="col">Backend (resolved)</th>
                <th role="columnheader" scope="col" className="is-num">Runtimes</th>
                {/* "units", never "agents". Admission increments each pool by the
                    resource class's weight, so 8 in use may be four agents of a
                    class that weighs 2. THE `?` THAT SAID SO IS GONE (B7.4):
                    `(units)` is already the entire claim, in the only place it
                    cannot be separated from the figures, and a glyph beside it
                    could only repeat the word above it. */}
                <th role="columnheader" scope="col" className="is-num">In use (units)</th>
                <th role="columnheader" scope="col" className="is-num">Ceiling</th>
                <th role="columnheader" scope="col" className="is-num">Headroom</th>
                <th role="columnheader" scope="col">Status</th>
              </tr>
            </thead>
            <tbody role="rowgroup">
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
          <caption>by resolved backend · full pool board under Pools</caption>
        </table>
      </div>
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
  // different things, so the accessible name says which one this dash is.
  const why = unread
    ? 'The capacity read failed, so this was not measured.'
    : 'No pool of this name is configured, so nothing caps this backend.'
  const dash = (
    <span className="ctl-em" aria-label={why}>
      —
    </span>
  )

  return (
    <tr role="row" className={over ? 'is-bad over' : paused ? 'is-paused paused' : full || shut ? 'is-warn full' : undefined}>
      <th role="rowheader" scope="row">
        {/* The identifier the API sent, verbatim. A prettified display name
            would be a two-entry table keyed on a frozen enum, and a backend
            this UI has never heard of would render as a blank instead of
            naming itself. */}
        <span className="mono">{backend}</span>
      </th>
      {/* `data-label` is the column name, repeated nowhere else and rendered
          only by §B6.3's `::before` below 900px. It adds no DOM and no text
          node, so the prose budgets measure the same screen they always did. */}
      <td role="cell" data-label="Runtimes" className="is-num">{members.length}</td>
      <td role="cell" data-label="In use (units)" className="is-num">{pool === null ? dash : pool.active}</td>
      <td role="cell" data-label="Ceiling" className="is-num">{pool === null ? dash : pool.effective_limit}</td>
      <td role="cell" data-label="Headroom" className="is-num">{pool === null ? dash : pool.available}</td>
      <td role="cell" data-label="Status">
        <span className="rt-marks">
          {unread && <span className="ctl-mark is-unread">not read</span>}
          {!unread && pool === null && (
            <span className="ctl-chip is-info" title={why}>
              <i aria-hidden="true" />
              uncapped
            </span>
          )}
          {paused && (
            <span className="ctl-chip is-paused" title="An operator paused this pool. It admits nothing until resumed, whatever its headroom says.">
              <i aria-hidden="true" />
              paused
            </span>
          )}
          {over && (
            <span
              className="ctl-chip is-bad"
              title={`${pool?.active} units are held against a ceiling of ${pool?.effective_limit}. Admission cannot produce that, so it is drift: a limit lowered under running work, or a slot never released. 'make pool-check' finds these.`}
            >
              <i aria-hidden="true" />
              over ceiling
            </span>
          )}
          {shut && (
            <span className="ctl-chip is-warn" title="The ceiling on this backend is zero, so nothing can start here however empty it looks.">
              <i aria-hidden="true" />
              admits nothing
            </span>
          )}
          {full && (
            <span className="ctl-chip is-warn">
              <i aria-hidden="true" />
              full
            </span>
          )}
          {pool !== null && !paused && !over && !shut && !full && (
            <span className="ctl-chip is-ok">
              <i aria-hidden="true" />
              ok
            </span>
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
 * absent value rather than as "now" -- the word that helper returns for a
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

/** One derived comparison: a short mono key and a one-or-two word value. */
interface Distinction {
  key: string
  value: string
}

/**
 * What this runtime specialises in, DERIVED rather than described.
 *
 * The owner asked what each runtime specialises in. Nothing in the response is
 * a description, and writing one per name would be a table of prose keyed on
 * the frozen catalogue -- stale the day a profile is added, and invisible when
 * it goes stale. What the response does support is comparison: every entry
 * below is this runtime measured against the others in the same payload, so it
 * stays true for a catalogue nobody has told this file about.
 *
 * B4.4: THE COMPARISON IS UNCHANGED; ITS PHRASING IS GONE. These were full
 * clauses -- "the only runtime this platform dispatches to cloudrun" -- joined
 * with middots into a paragraph-length cell. They are now a key and a value,
 * which is what they always were: the verb carried nothing the key does not.
 * `#help/what-sets-it-apart-is-arithmetic` states what the arithmetic is.
 *
 * An empty list is returned as an empty list and marked by the caller. "The
 * comparison ran and found no difference" is a finding; a blank cell is not.
 *
 * ONLY WHAT NOTHING ELSE SHARES, UNDER THE CARD'S OWN KEYS (CP-16, visual QA
 * 2026-09-25). This used to push `target 1 of 4` and `quota none spent` for
 * facts three other runtimes had too -- a distinction nothing distinguishes --
 * and to restate the card's rows under keys that clashed with the card's own
 * (`ceiling` beside `max run`, `creds` beside `cred`), so one fact read as two.
 * Every entry is now `sole`, `longest`/`shortest` or `heaviest`/`lightest`
 * against the rest of the payload, and its key is the key of the row above it
 * that it is about. A runtime that shares everything gets the real-zero mark,
 * which is what that finding already looked like.
 */
function distinguishing(r: Runtime, all: Runtime[]): Distinction[] {
  const facts: Distinction[] = []
  // A catalogue of one has nothing to be told apart from.
  if (all.length < 2) return facts

  /** True when no other runtime in the payload has the same value. */
  const sole = (of: (x: Runtime) => string | null): boolean =>
    all.filter((x) => of(x) === of(r)).length === 1

  if (sole((x) => x.resolved_backend)) facts.push({ key: 'runs on', value: 'sole' })
  if (sole((x) => x.image)) facts.push({ key: 'image', value: 'sole' })

  // THE CREDENTIAL AS ONE FACT: which provider, and which secrets under which
  // rule -- the whole of what the `cred` row says. A runtime that needs none is
  // told apart only when it is the only one that needs none; while two need
  // none, "none spent" describes both and distinguishes neither.
  const credential = (x: Runtime): string =>
    x.provider === null ? '' : `${x.provider}|${x.secrets_any_of ? 'any' : 'all'}|${[...x.secrets].sort().join(',')}`
  if (sole(credential)) facts.push({ key: 'cred', value: 'sole' })

  const timeouts = all.map((x) => x.timeout_seconds)
  if (soleExtreme(r.timeout_seconds, timeouts, 'max')) {
    facts.push({ key: 'max run', value: 'longest' })
  } else if (soleExtreme(r.timeout_seconds, timeouts, 'min')) {
    facts.push({ key: 'max run', value: 'shortest' })
  }

  const weights = all.map((x) => x.resources.units)
  if (soleExtreme(r.resources.units, weights, 'max')) {
    facts.push({ key: 'weight', value: 'heaviest' })
  } else if (soleExtreme(r.resources.units, weights, 'min')) {
    facts.push({ key: 'weight', value: 'lightest' })
  }

  return facts
}

function RuntimeCard({ runtime, all }: { runtime: Runtime; all: Runtime[] }) {
  const facts = distinguishing(runtime, all)
  // A profile that let the platform choose is the case this exists for: the
  // declared value and the resolved one then answer different questions, and
  // when they agree there is only one question to answer.
  const platformChose = runtime.backend !== runtime.resolved_backend
  const spec = runtime.resources
  const left = spec.memory_gib - spec.disk_gib
  const coherent = Number.isFinite(left) && left >= 0
  const off = runtime.available === false
  const reasonId = useId()

  return (
    <section className="ctl-card">
      <div className="ctl-card-head">
        {/* `<Id>` because the string here is the exact `runner_profile` a
            caller sends: an uppercased one would be a name nobody can copy. */}
        <h2 className="ctl-card-title">
          <Id>{runtime.name}</Id>
        </h2>
        {/* A DISABLED PROFILE IS MARKED WHERE IT IS READ, not only where it is
            refused. The catalogue serves it because an existing task that names
            it still has to render; a reader scanning this list needs to know at
            a glance that it cannot be dispatched, or the entry reads as an
            option. `is-bad` stays: the one profile disabled today was switched
            off over a credential its provider refused, which is a failure and
            not a choice. (No profile is named here: this file may hold no
            name from the frozen catalogue, comments included --
            test_runtimes_screen.py reads it whole.) */}
        {off ? (
          <span className="ctl-chip is-bad" aria-describedby={runtime.disabled_reason ? reasonId : undefined}>
            <i aria-hidden="true" />
            disabled
          </span>
        ) : (
          <span className="ctl-card-note">{runtime.resolved_backend}</span>
        )}
      </div>

      <div className="ctl-card-body">
        {/* THE REASON IS ON THE CARD, NOT IN AN ATTRIBUTE (CP-17, visual QA
            2026-09-25). It was the chip's `aria-label` and nothing else: a
            sighted reader got `disabled` and no way to learn why, which sends
            them looking for a setting -- and the reason is the only part of
            this they can act on (it names the profile to use instead). The
            chip points at it, so
            a screen reader still hears it with the mark. */}
        {off && runtime.disabled_reason && (
          <p className="muted" id={reasonId}>
            {runtime.disabled_reason}
          </p>
        )}
        {/* §B6.4: `is-rows`. Thirteen pairs in a wrapping flex strip rendered
            as a justified blob -- `max run 5m size demo-small cpu 3 vCPU` on
            one line -- with every key starting wherever the previous value
            ended. The pairs are unchanged; what they gain is a key COLUMN, so
            finding `mem` is a glance down an edge rather than reading a line. */}
        <ul className="ctl-facts is-rows">
          <li className="ctl-fact">
            <b>runs on</b>
            <span className="mono">{runtime.resolved_backend}</span>
            {platformChose && (
              <span
                className="rt-declared"
                title={`The profile declares ${runtime.backend}; resolve_backend turned it into ${runtime.resolved_backend}.`}
              >
                ← {runtime.backend}
              </span>
            )}
          </li>
          <li className="ctl-fact rt-fact-wide">
            <b>image</b>
            <span className="mono rt-image">{runtime.image}</span>
          </li>
          <li className="ctl-fact">
            {/* §8.4(1): THE LABEL IS THE EXPLANATION. "before the platform
                stops the attempt" is what `MAX RUN` means; the clause added a
                verb and no information. */}
            <b>max run</b>
            {ceilingLabel(runtime.timeout_seconds)}
          </li>
          <li className="ctl-fact">
            <b>size</b>
            <span className="mono">{runtime.resource_class}</span>
          </li>
          <li className="ctl-fact">
            <b>cpu</b>
            {spec.cpu} vCPU
          </li>
          <li className="ctl-fact">
            <b>mem</b>
            {spec.memory_gib} GiB
          </li>
          <li className={`ctl-fact${coherent ? '' : ' is-absent'}`}>
            {/* THE WORKSPACE IS A SLICE OF THE MEMORY BESIDE IT, not storage
                on top of it -- the workspace is a memory-backed tmpfs because
                the Terraform google provider cannot express Cloud Run's
                disk-backed empty_dir. `of mem` is what stops the two being
                added together, in two characters rather than a clause. */}
            <b>ws of mem</b>
            {spec.disk_gib} GiB
          </li>
          <li className="ctl-fact">
            <b>weight</b>
            {spec.units}u
          </li>
          <li className="ctl-fact rt-fact-wide">
            {/* `cred` NAMES WHAT IT SHOWS, which is why the `?` here could go
                (B7.4). `<Credential>` below renders secret NAMES and never a
                value -- it cannot render a value, because the route does not
                serve one -- so the topic was explaining a guarantee the reader
                can see being kept. It is one line in the footer index now.
                If a `?` is ever put back on a key like this one, it belongs
                INSIDE the `<b>`, not after the value: `spaceprobe.ts:545`
                short-circuits at the first element carrying direct text, so a
                glyph placed after a text node in the same element is never
                measured, while one placed as a bare sibling of element-only
                children is. That convention is load-bearing; see the note at
                the foot of this file. */}
            <b>cred</b>
            <Credential runtime={runtime} />
          </li>
        </ul>

        <div className="rt-apart">
          {/* "Sets it apart FROM THE REST OF THE CATALOGUE" is what the `?`
              here said -- that this block is arithmetic over the other rows and
              not an editorial judgement about this runtime. The eyebrow sits
              inside a card headed by that runtime's name, one scroll under a
              list of every other one, so the comparison it is making is on the
              screen; the topic is in the footer index. */}
          <span className="ctl-eyebrow">Sets it apart</span>
          {facts.length === 0 ? (
            /* A MEASURED "nothing", not a blank. The comparison ran and found
               no difference; a blank here would read as a comparison nobody
               made. */
            <span className="ctl-mark is-zero">real zero</span>
          ) : (
            <ul className="ctl-facts is-rows rt-apart-facts">
              {facts.map((f) => (
                <li className="ctl-fact" key={f.key}>
                  <b>{f.key}</b>
                  {f.value}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </section>
  )
}

/**
 * What a tenant has to have registered before this runtime will run for them.
 *
 * `secrets_any_of` is the field this panel exists to render. A list of two
 * names without it tells a tenant who pays for a subscription that they must
 * also buy metered access -- the refusal the flag exists in the frozen
 * catalogue to prevent -- so the flag is rendered as WORDS ("any one of"/"all
 * of"), never as a dot, a badge or a tooltip. That is unchanged by B4.4: the
 * flag is the datum, not an explanation of one, and §8.5(1) keeps a name on
 * the surface.
 *
 * What DID go is the trailing "· names only, never values". It is a promise
 * about this client rather than a fact about this runtime, it was repeated on
 * every card, and it is `#help/credential-names-not-values` -- linked from the
 * column and from the footer.
 */
function Credential({ runtime }: { runtime: Runtime }) {
  if (runtime.provider === null) {
    return (
      /* Not "unknown", and not an em dash: em dash means "not measured", and
         this is measured. The answer is that it needs nothing. */
      <span className="rt-cred">
        <span className="ctl-chip is-ok">
          <i aria-hidden="true" />
          none needed
        </span>
      </span>
    )
  }
  if (runtime.secrets.length === 0) {
    return (
      <span className="rt-cred">
        <span className="mono">{runtime.provider}</span>{' '}
        {/* THE MARK STAYS. An empty credential cell beside a named provider is
            exactly what a failed read would look like, and this is not one --
            so it is marked as a measurement rather than left blank. */}
        <span className="ctl-mark is-zero">real zero</span>
      </span>
    )
  }
  return (
    /* B4.6: ONE WRAPPER, AND IT FIXES A REAL LAYOUT DEFECT.
       `.ctl-fact` is `display: inline-flex`, so every child this component
       returned became a FLEX ITEM of the fact: the provider span, the ` · `,
       the `any one of`, and each secret. Flex items shrink independently, so
       at the catalogue's card width the provider broke mid-token
       (`demo-` / `vendor`) and the flag broke mid-phrase (`any` on one line,
       `of` alone on the next, with `one` squeezed out of view). Both were
       visible on the shipped Runtimes screen at 1440x900 and neither was a
       gate failure -- `spacing.test.tsx` is jsdom and has no layout engine
       (§0.3), so nothing in the repository could see it.

       The wrapper makes the whole credential ONE flex item whose contents lay
       out as ordinary text, which is what they are: a sentence fragment naming
       a provider and its secrets. `secrets_any_of` stays WORDS, never a badge
       -- the flag is the datum (§8.5.1) and this changes nothing about it. */
    <span className="rt-cred">
      <span className="mono">{runtime.provider}</span> ·{' '}
      {runtime.secrets_any_of ? <strong>any one of</strong> : <strong>all of</strong>}{' '}
      {runtime.secrets.map((s, i) => (
        <span key={s}>
          {i > 0 && ', '}
          <code>{s}</code>
        </span>
      ))}
    </span>
  )
}

/* -------------------------------------------------------------------------
 * Sizing
 * ---------------------------------------------------------------------- */

/**
 * Every size this platform offers, and which runtimes reach it.
 *
 * TWO SOURCES, DELIBERATELY. `/v1/resource-classes` is the whole catalogue;
 * `/v1/runtimes` carries only the classes something resolved to. Preferring the
 * first means a class nothing routes to is visible -- which is a real finding,
 * since a class no runtime names cannot be asked for by anyone. When that read
 * failed the table is still drawn from the sizes embedded in the runtimes, and
 * the card is marked `partial` rather than quietly showing a shorter list.
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
    /* §B6.1, as Backends above: title on the page, one box around the data. */
    <section className="section rt-sizing">
      <div className="ctl-toolbar">
        {/* NO `?`: the mark and the note beside this heading already say where
            this table came from. `partial` plus `· from runtimes` IS
            `catalogue-from-route` -- that the classes were derived from the
            runtime list because the catalogue read did not land -- and it says
            it as an encoding rather than as a sentence behind a glyph. The one
            `?` this screen keeps is on the catalogue eyebrow above. */}
        <h2 className="ctl-card-title">Sizing</h2>
        {/* A PARTIAL LIST IS NOT A LIST. The catalogue read failed, so a class
            no runtime resolves to cannot appear here at all -- the note is the
            coverage qualifier and the mark is the kind of absence. */}
        {!fromRoute && <span className="ctl-mark is-partial">partial</span>}
        <span className="ctl-card-note is-end">
          {rows.length} class{rows.length === 1 ? '' : 'es'}
          {!fromRoute && ' · from runtimes'}
        </span>
      </div>

      <div className="ctl-table is-stacked">
        <table role="table">
            <thead role="rowgroup">
              <tr role="row">
                <th role="columnheader" scope="col">Class</th>
                {/* Both the request and the limit: requests == limits
                    platform-wide, so there is no burst headroom above these. */}
                <th role="columnheader" scope="col" className="is-num">vCPU</th>
                <th role="columnheader" scope="col" className="is-num">Memory</th>
                {/* §8.4(3). "Carved out of the memory beside it, not added to
                    it" was a tooltip on every cell; `(of memory)` is the same
                    claim, visible, once, attached to the column. */}
                <th role="columnheader" scope="col" className="is-num">Workspace (of memory)</th>
                {/* `(units)`, because a bare "Weight" is a number with no
                    dimension and the `?` that supplied the dimension is gone
                    (B7.4). The cells read `2u`, so the unit is on the figure
                    too; what the header adds is that the unit is what the POOLS
                    count, so two agents of different classes can cost the same
                    and two of the same class can cost double one of another.
                    NO CLASS IS NAMED IN THIS COMMENT, and that is not style:
                    `tests/unit/control_plane/test_runtimes_screen.py` greps this
                    file for the frozen catalogue's entries and fails on one,
                    because every value on this screen has to come from the
                    response or a resized class renders its old figure forever.
                    `#help/units-not-agents` is in the footer index. */}
                <th role="columnheader" scope="col" className="is-num">Weight (units)</th>
                <th role="columnheader" scope="col">Resolves from</th>
              </tr>
            </thead>
            <tbody role="rowgroup">
              {rows.map((spec) => {
                const users = usedBy.get(spec.name) ?? []
                return (
                  <tr role="row" key={spec.name}>
                    <th role="rowheader" scope="row">
                      <span className="mono">{spec.name}</span>
                    </th>
                    <td role="cell" data-label="vCPU" className="is-num">{spec.cpu}</td>
                    <td role="cell" data-label="Memory" className="is-num">{spec.memory_gib} GiB</td>
                    <td role="cell" data-label="Workspace (of memory)" className="is-num">{spec.disk_gib} GiB</td>
                    <td role="cell" data-label="Weight (units)" className="is-num">{spec.units}u</td>
                    <td role="cell" data-label="Resolves from">
                      {users.length === 0 ? (
                        /* CONFIGURED AND UNREACHABLE is a fact, not a fault:
                           no runtime resolves to this class, so no caller can
                           reach it. `--info` is this sheet's tone for a fact
                           that is not a verdict. */
                        <span
                          className="ctl-chip is-info"
                          title="No runtime in this catalogue resolves to this class, so no caller can reach it."
                        >
                          <i aria-hidden="true" />
                          unreachable
                        </span>
                      ) : (
                        <span className="mono">{users.join(', ')}</span>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          {!fromRoute && (
            <caption>{classesDetail ?? 'the resource-class read did not complete'}</caption>
          )}
        </table>
      </div>
    </section>
  )
}

/**
 * WHAT USED TO BE `Legend()` -- five `<dt>`/`<dd>` pairs, ~230 words, under
 * every load of this screen.
 *
 * AND, SINCE B7.4, WHERE THIS SCREEN'S OTHER NINE `?` WENT. It carried ten help
 * glyphs, the most of any screen in the console, and nine of them named a
 * standing argument about the catalogue rather than about the cell they were
 * pinned to -- so a reader met the same nine arguments again on every visit,
 * each one a hover away from a different figure. As links they are one row, in
 * one place, in the reader's own reading order, and `#help/<id>` is the same
 * destination the glyph opened. Three of the nine were pure repetition: the
 * column head already said `(units)`, `(of memory)` and `ws of mem`.
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
  'runtime-needs-no-provider',
  'what-sets-it-apart-is-arithmetic',
]
