import { useEffect, useState } from 'react'
import { loadMe, loadStats } from './api'
import { errorHeading, type ApiError, type Result } from './fetch'
import { HelpCard } from './HelpCard'
import { PageHead, timeAgo } from './Shell'
import { NEVER_WRITTEN, REAL_STATES, pluralise, type Stats } from './types'
import { AGE_TICK_MS, useNow } from './useNow'

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
 * THAT USED TO BE A PARAGRAPH ABOVE THE BUTTON. It is now a figure --
 * `24 count() per run` -- which is the same fact with the arithmetic already
 * done for the reader and, unlike the sentence, it moves when the caller turns
 * out to be an admin. §8.4.1: a well-chosen unit is the explanation. What a
 * run returns is at #help/platform-counts.
 *
 * AND IT SITS IMMEDIATELY BEFORE THE CONTROL IT PRICES (AH-25). The head was a
 * shape of its own -- `.ctl-page-head` with the button pinned right -- and the
 * cost sat in a toolbar row under it, a row away from the press it priced,
 * although this comment said "beside the control". The head is `PageHead` now,
 * the one fourteen `Screen` routes draw, and its line reads like theirs: what
 * was read, how long ago, and the read-now control, with the cost printed
 * right before that control in one span that does not wrap apart.
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
  /**
   * WHO IS ASKING, FROM THE SESSION READ -- the same `/v1/tenants/me` the
   * header's admin badge is drawn from (AH-9, visual QA 2026-09-25).
   *
   * The cost below used to learn it only from the RESULT of a run, so before
   * the first press it was unknown and was priced as a tenant's: an admin was
   * shown `12 count()` for a press that costs 24. The figure exists to say what
   * the button costs, and it was wrong on exactly the press it was there for.
   * `null` is "not known yet, or the read failed" and is priced as such.
   */
  const [sessionAdmin, setSessionAdmin] = useState<boolean | null>(null)
  // The run's age moves on the shared clock, as every age in the frame does
  // (CH-1): an age that was read once and never re-drawn is the one number on
  // the line whose whole job is to grow.
  const now = useNow(AGE_TICK_MS)

  useEffect(() => {
    let live = true
    loadMe().then((r) => {
      if (live && (r.status === 'ok' || r.status === 'stale')) setSessionAdmin(r.data.principal.is_admin)
    })
    return () => {
      live = false
    }
  }, [])

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
  // A RUN'S OWN ANSWER WINS WHEN THERE IS ONE: it is the route that bills, and
  // it says whether it counted the platform. Before any run, the session says.
  const admin = data ? data.platform_tasks_by_state !== undefined : sessionAdmin
  // DERIVED, both halves. "Twelve, or twenty-four as an admin" was a copy of
  // the size of the frozen state enum, written in prose, with nothing checking
  // it -- and the admin doubling was a clause the reader had to apply. Now the
  // number on screen is the number this caller's next press will cost -- and
  // when nobody could say who the caller is, BOTH numbers, because a single one
  // would be a guess about the caller presented as a price.
  const queries =
    admin === null ? `${STATE_COUNT}–${STATE_COUNT * 2}` : String(STATE_COUNT * (admin ? 2 : 1))

  // WHAT WAS READ, AND HOW LONG AGO -- the first half of the head line, as on
  // every Screen route. A run that came back with no counts is a failed query
  // (the panel below says so), so it reads as a failed run here too, and
  // neither kind of failure is counted as a run.
  const failed = run !== null && (run.status === 'error' || run.status === 'empty')
  const provenance = failed ? (
    'last run failed'
  ) : runs === 0 ? (
    'not counted yet'
  ) : (
    <>
      {pluralise(runs, 'run')}
      {data && ` · read ${timeAgo(data.generated_at, now)}`}
    </>
  )

  return (
    <>
      {/* THE ONE PAGE HEAD (AH-25): a title over one line. No `?` on it
          (B7.4): `24 count() per run` IS the cost of pressing the button, as a
          figure with its unit, immediately before the button. */}
      <PageHead title="Platform counts">
        {provenance}
        {' · '}
        {/* ONE SPAN, `white-space: nowrap`: at 390 the line wraps, and the
            price must not end one line with its control starting the next. */}
        <span className="counts-run">
          <span className="counts-cost">{queries} count() per run</span>
          {' · '}
          {/* `.sub button`, the same read-now control as Screen's `refresh`,
              not the boxed `.retry`: pressing it is the same act. */}
          <button type="button" onClick={go} disabled={busy}>
            {busy ? 'Counting…' : run === null ? 'Run the count' : 'Run it again'}
          </button>
        </span>
      </PageHead>

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
          <p>Treat this as a failed query.</p>
        </div>
      )}

      {data && (
        <div className="ctl-cards counts-scopes">
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
          {/* NO `?` (B7.4). The figure's own accessible name beside it is
              `admin-gate-not-failure` spelled out -- absent rather than zero,
              nothing failed -- and the `is-admin` mark is the fourth mark this
              console draws precisely so the case does not need a sentence. */}
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
        <p>Nothing failed.</p>
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
          {/* NO `?` (B7.4). The figure's accessible name above is longer and
              more specific than `withheld-total`: it names HOW MANY of the
              state counts did not come back and WHICH ones, for this response.
              A glyph beside it opened the general version of a sentence the
              figure already carries with the particulars filled in. */}
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
      {/* THIS SCREEN'S ONE `?` (B7.4). It was seven; six of them opened a card
          restating an accessible name already attached to the figure beside
          them, twice over in two cases. This one is different in kind: the line
          under it is a bare list of three state names with no figure, no mark
          and no context, and nothing on this screen says why a state the
          contract defines is absent from a histogram of the contract's states.
          A reader who does not open it is left with an unexplained list, which
          is the only place on this screen where that is true.
          `honesty.counts.test.tsx` pins it to this foot.

          AFTER THE LABEL, NEVER AFTER A VALUE (AH-24). It trailed the list --
          `never written: A · B · C ?` -- where it read as a footnote on the
          last state name. It follows `never written:` now, before the names. */}
      <p className="ctl-card-foot">
        never written:
        <HelpCard topic="states" /> {Array.from(NEVER_WRITTEN).join(' · ')}
      </p>
    </section>
  )
}
