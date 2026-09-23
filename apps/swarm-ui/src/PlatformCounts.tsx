import { useState } from 'react'
import { loadStats } from './api'
import { errorHeading, type ApiError, type Result } from './fetch'
import { HelpCard } from './HelpCard'
import { timeAgo } from './Shell'
import { NEVER_WRITTEN, REAL_STATES, type Stats } from './types'

/**
 * Platform-wide task counts. Admin only, and deliberately behind a button.
 *
 * WHY A BUTTON AND NOT AN AUTO-LOAD. /v1/stats runs one Firestore count()
 * aggregation per TaskState -- twelve for a tenant, twenty-four for an admin,
 * because an admin also gets the platform figures. count() bills per 1000
 * index entries scanned, so the cost of this screen grows with the platform's
 * HISTORY, not with how busy it is. It is the one panel that gets more
 * expensive as the platform gets older, and auto-refreshing it at 5s would be
 * a standing charge nobody chose.
 *
 * THAT USED TO BE A PARAGRAPH ABOVE THE BUTTON. It is now the figure in the
 * toolbar -- `24 count() per run` -- which is the same fact with the arithmetic
 * already done for the reader and, unlike the sentence, it moves when the
 * caller turns out to be an admin. §8.4.1: a well-chosen unit is the
 * explanation. The argument is at #help/api-reads.
 *
 * SCOPE IS NOT DECORATION. `tasks_by_state` is the caller's own tenant;
 * `platform_tasks_by_state` is everyone. They are separate cards with the
 * scope in the title, and never side by side in one row -- an admin reading
 * their own four running tasks as the platform total is a truth bug, not a
 * layout preference.
 */
export function PlatformCountsScreen() {
  const [run, setRun] = useState<Result<Stats> | null>(null)
  const [runs, setRuns] = useState(0)
  const [busy, setBusy] = useState(false)

  const go = () => {
    setBusy(true)
    loadStats().then((r) => {
      setRun(r)
      setBusy(false)
      // Counted only on a read that actually produced counts. Incrementing on
      // every settled promise would let a failure inflate a number the toolbar
      // then presents as work that was billed.
      if (r.status === 'ok' || r.status === 'stale') setRuns((n) => n + 1)
    })
  }

  const data = run && (run.status === 'ok' || run.status === 'stale') ? run.data : null
  const admin = data ? data.platform_tasks_by_state !== undefined : null
  // DERIVED, both halves. "Twelve, or twenty-four as an admin" was a copy of
  // the size of the frozen state enum, written in prose, with nothing checking
  // it -- and the admin doubling was a clause the reader had to apply. Now the
  // number on screen is the number this caller's next press will cost.
  const queries = STATE_COUNT * (admin === true ? 2 : 1)

  return (
    <>
      <div className="ctl-page-head">
        <h1>Platform counts</h1>
        <span className="is-end">
          <button className="retry" onClick={go} disabled={busy}>
            {busy ? 'Counting…' : runs === 0 ? 'Run the count' : 'Run it again'}
          </button>
        </span>
      </div>

      {/* THE ONLY CHROME ABOVE THE CONTENT, AND IT CONTAINS NO SENTENCE.
          The cost of the control, as a figure with its unit, beside the
          control. */}
      <div className="ctl-toolbar">
        <span className="ctl-fact">
          <b>per run</b>
          {queries} count()
        </span>
        <HelpCard topic="api-reads" />
        {runs > 0 && (
          <span className="provenance is-end">
            {runs} run{runs === 1 ? '' : 's'}
            {data && ` · read ${timeAgo(data.generated_at)}`}
          </span>
        )}
      </div>

      {run?.status === 'error' && <Failed error={run.error} />}

      {run?.status === 'empty' && (
        <div className="ctl-empty is-failed">
          <span
            className="ctl-mark is-unread"
            aria-label="The read succeeded and returned no counts at all, which should be impossible. Treat it as a failed query rather than an idle platform."
          >
            not read
          </span>
          <h3>No counts came back</h3>
          <p>
            Treat this as a failed query.
            <HelpCard topic="read-failed" />
          </p>
        </div>
      )}

      {data && (
        <div className="ctl-cards">
          <Scope title="This tenant" subtitle={data.tenant_id} counts={data.tasks_by_state} />
          {data.platform_tasks_by_state !== undefined ? (
            <Scope
              title="Every tenant"
              subtitle="all tenants"
              counts={data.platform_tasks_by_state}
            />
          ) : (
            <AdminGated />
          )}
        </div>
      )}
    </>
  )
}

/**
 * How many `count()` queries one run costs, for one scope.
 *
 * DERIVED, because the alternative is a number in prose that nothing
 * checks. `check-contract-parity.sh` covers shell and jq and does not read
 * TypeScript, so "twelve" here would have outlived the twelfth state.
 */
const STATE_COUNT = REAL_STATES.length + NEVER_WRITTEN.size

/**
 * The platform block a non-admin cannot have.
 *
 * ABSENT, NOT ZERO, AND NOT BROKEN. It keeps the card, the title and the
 * figure slot -- a panel that simply vanished would be indistinguishable from
 * one that was never going to be there -- and the figure slot holds the em
 * dash with the blue `admin only` mark beside it. Blue and solid, never red
 * and never dashed: a non-admin genuinely cannot read /v1/admin/*, and
 * painting that as a failure tells someone their platform is down when it is
 * not.
 */
function AdminGated() {
  return (
    <section className="ctl-card">
      <div className="ctl-card-head">
        <h2 className="ctl-card-title">Every tenant</h2>
        <span className="ctl-card-note">not entitled</span>
      </div>
      <div className="ctl-card-body">
        <p className="counts-total">
          <b
            className="ctl-figure is-absent"
            aria-label="These figures are absent from this response rather than zero. Only an admin may read the platform-wide counts; nothing failed."
          >
            <i className="ctl-em">—</i>
          </b>
          <span className="ctl-mark is-admin">admin only</span>
          <HelpCard topic="admin-gate-not-failure" />
        </p>
      </div>
    </section>
  )
}

function Failed({ error }: { error: ApiError }) {
  if (error.kind === 'admin_required') {
    return (
      <div className="ctl-empty is-admin">
        <span
          className="ctl-mark is-admin"
          aria-label="You are not in an admin group. Nothing failed, and this says nothing about the platform."
        >
          admin only
        </span>
        <h3>Not an admin</h3>
        <p>
          Nothing failed.
          <HelpCard topic="admin-gate-not-failure" />
        </p>
      </div>
    )
  }
  return (
    // NO NUMBER ANYWHERE ON A FAILURE. A zero here is the most reassuring
    // thing on the screen and it would be a guess. The sentence that used to
    // say so -- "this says nothing about how much work the platform is
    // carrying" -- is the mark's accessible name: the mark is the visible,
    // greyscale-safe anchor and the words ride on it.
    <div className="ctl-empty is-failed">
      <span
        className="ctl-mark is-unread"
        aria-label="No counts are shown, because none arrived. This says nothing about how much work the platform is carrying."
      >
        not read
      </span>
      <h3>{errorHeading(error)}</h3>
      <p>
        {error.code ?? 'error'} — {error.message}
        <HelpCard topic="read-failed" />
      </p>
    </div>
  )
}

function Scope({
  title,
  subtitle,
  counts,
}: {
  title: string
  subtitle: string
  counts: Record<string, number>
}) {
  const real = REAL_STATES.map((s) => ({ state: s, n: counts[s] }))
  const missing = real.filter((r) => typeof r.n !== 'number').map((r) => r.state)
  const max = Math.max(1, ...real.map((r) => (typeof r.n === 'number' ? r.n : 0)))

  // Only summed when every state arrived. A total over a partial response is
  // a wrong number wearing the clothes of a right one.
  const total = missing.length === 0 ? real.reduce((a, r) => a + (r.n as number), 0) : null
  const arrived = REAL_STATES.length - missing.length

  return (
    <section className="ctl-card counts-card">
      <div className="ctl-card-head">
        <h2 className="ctl-card-title">{title}</h2>
        {/* The qualifier slot carries the COVERAGE when the response was
            partial, because that is the fact that changes how every figure
            below should be read, and the scope otherwise. */}
        <span className="ctl-card-note">
          {total === null ? `${arrived} of ${REAL_STATES.length} states` : subtitle}
        </span>
      </div>

      <div className="ctl-card-body">
        {/* THE TOTAL'S SLOT IS ALWAYS DRAWN. A card that simply omitted the
            total when it could not compute one would look like a card that
            never had a total. The hole is drawn as a hole: the em dash at the
            figure step's absent treatment, with the `partial` mark -- dashed
            on one side only, the side the missing part would have been on --
            beside it. The reader never has to notice something is missing;
            the missing thing is on screen. */}
        <p className="counts-total">
          <b
            className={`ctl-figure${total === null ? ' is-absent' : ''}`}
            aria-label={
              total === null
                ? `No total is shown. ${missing.length} of the ${REAL_STATES.length} state counts did not come back (${missing.join(', ')}), and a sum over a partial response is a wrong number wearing the clothes of a right one.`
                : `${total} task documents across the ${REAL_STATES.length} states that are written.`
            }
          >
            {total === null ? <i className="ctl-em">—</i> : total}
            {total !== null && <span className="ctl-figure-unit">tasks</span>}
          </b>
          {total === null && <span className="ctl-mark is-partial">partial</span>}
          <HelpCard topic="withheld-total" />
        </p>

        {real.map(({ state, n }) => (
          <div className="split-row" key={state}>
            <span className="sr-name">{state}</span>
            {/* A STATE THE RESPONSE DID NOT CARRY IS NOT A ZERO (§B20). This
                drew a zero-width fill on a plain track for it, which is the one
                mark the axis now reserves for a MEASURED zero -- so an absent
                count would have been promoted to a measurement by the fix.
                Hatched instead, with no axis, matching the em dash beside it. */}
            <span className={`sr-bar${typeof n === 'number' ? '' : ' is-unknown'}`}>
              <i style={{ width: `${((typeof n === 'number' ? n : 0) / max) * 100}%` }} />
            </span>
            <span className="sr-n">
              {typeof n === 'number' ? (
                n
              ) : (
                <i
                  className="ctl-em"
                  aria-label={`The ${state} count did not come back. That is an absence, not a zero.`}
                >
                  —
                </i>
              )}
            </span>
          </div>
        ))}
      </div>

      {/* The states that can never be written, named rather than explained.
          A bucket that can only ever read zero teaches "nothing is wrong"
          rather than "this cannot happen", so they are not drawn above -- and
          naming them here is what stops their absence from the histogram
          looking like an oversight. Derived from the contract sets, so the
          list cannot drift if a state is added. */}
      <p className="ctl-card-foot">
        never written: {Array.from(NEVER_WRITTEN).join(' · ')}
        <HelpCard topic="states" />
      </p>
    </section>
  )
}
