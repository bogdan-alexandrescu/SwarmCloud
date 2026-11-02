import { loadCapacity } from './api'
import { CapSeg, NoPools, PoolsSkeleton, keepNoPools } from './Capacity'
import type { TopicId } from './help'
import { POOLS_POLL_MS, capacityPoll } from './capacityPoll'
import { ProfileMatrix } from './ProfileMatrix'
import { Screen, timeAgo } from './Shell'
import { AGE_TICK_MS, useNow } from './useNow'
import type { Capacity } from './types'

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
 *    them, never a sum; the matrix outlines the binding cell, which makes that
 *    minimum visible rather than asserted.
 * 3. The catalogue is frozen contract data that can GAIN entries, so everything
 *    below renders whatever arrived: no list of five, no per-name special case,
 *    no client-side weight table.
 */
export function ProfilesScreen() {
  return (
    <Screen
      /* "POOLS", BECAUSE THIS IS POOLS' SECOND VIEW (capacity.html C1, frame
         3, owner's pick 2026-10-01; the #503 audit). Ceilings and By runner
         profile are one page, headed "Pools", with the in-page strip below
         naming the view -- the panel lists By runner profile under Pools for
         the same reason. It was headed "By runner profile", as a page of its
         own. test_nav_headings_agree.py holds a nested view's heading to its
         parent tab's label and its strip to its own label.
         Every count here is still the CALLING tenant's: built from
         `pool_names_for(tenant_id=ctx.tenant_id)` unconditionally, admin
         included (service.py:313-329), which the summary says in words.
         The route is still `#capacity/profiles`: `runner_profile` is the
         contract's field name and invariant 10 is why this screen exists, so
         the address keeps the contract's noun. */
      title="Pools"
      /* THE SCREEN'S ONE `?` (CP-5's second half, #85): Pools' glyph. The
         banner it replaces said the conjunction once for every card, and the
         glyph does too -- after the title, AH-24's slot for a property of the
         whole screen -- because a `?` in each card head would be one per
         profile saying one thing.
         IT OPENS THE SCREEN'S TOPICS (QA1-FIX-24, capacity-13, part of
         #2596), as Ceilings' `?` has since VQ-033 (BGD 2026-10-26: "Move the
         topics behind the page's one ?"). By runner profile still ended in
         the `Reading this screen:` row of underlined monospace links; the
         row is gone and its four topics, `pools-all-at-once` among them, are
         behind this one glyph. See PROFILE_TOPICS. */
      help={PROFILE_TOPICS}
      load={loadProfilesView}
      // Part of Pools, so Pools' cadence (#117): every 30s, paused while the
      // tab is hidden (`Screen`).
      pollMs={capacityPoll(POOLS_POLL_MS)}
      skeleton={<PoolsSkeleton view="profiles" />}
      /* THE READ IS SAID ABOVE THE MATRIX (visual QA V197, part of #1427).
         Above 900px the matrix is its own vertical scroller, for its sticky
         head (styles/capacity.css), and its box looks finished at its bottom
         edge: the provenance line under it -- how many profiles, the whole
         catalogue, when the counts were read -- went unread below a box
         holding most of the page. It is the count note now, over the first
         card, where every screen's count is (#138). */
      summary={(d) => d.pools === 'some' && <CatalogueCount capacity={d.board} />}
      /* AN EMPTY POOL LIST IS POOLS' ONE EMPTY STATE (V199): `NoPools`, worded
         as Ceilings words it and under the same view strip. A response with
         pools and no profiles is not this; Catalogue below renders that. */
    >
      {(d, reading) => (
        <>
          <CapSeg view="profiles" />
          {d.pools === 'none' ? <NoPools reading={reading} /> : <Catalogue capacity={d.board} />}
        </>
      )}
    </Screen>
  )
}

function Catalogue({ capacity }: { capacity: Capacity }) {
  const entries = Object.entries(capacity.runner_profiles).sort(([a], [b]) => a.localeCompare(b))

  // A read that succeeded with an empty catalogue. Not `NoPools`, which means
  // "no pools", and not a failure — so nothing here is red.
  if (entries.length === 0) {
    return (
      <section className="section panel">
        <h2>The catalogue came back with no entries</h2>
        <p className="muted">
          The capacity read succeeded and returned {capacity.pools.length} pools, so
          this is not a failed read — the runner catalogue itself arrived
          empty, and nothing can be submitted until a profile is registered.
        </p>
      </section>
    )
  }

  return (
    <>
      {/* THE MATRIX IS THE SCREEN (capacity.html §B, decided 2026-10-01,
          #124): every profile against every pool family, the binding cell
          outlined. The per-profile cards that sat under it as each row's
          detail were removed with the rebrand (owner's decision, 2026-10-01).
          The conjunction -- a task clears every pool at once, so the figure is
          a minimum and never a sum -- is `#help/pools-all-at-once`, in the
          topic index the `?` after this screen's title opens. */}
      <ProfileMatrix capacity={capacity} />
    </>
  )
}

const loadProfilesView = keepNoPools(() => loadCapacity())

/**
 * The count note (#138): `36 profiles · 2 backends · the whole catalogue ·
 * counters last reported 2m ago, one pool at a time`.
 *
 * §8.4(4): PROVENANCE, ONCE, FOR THE WHOLE PAGE. Every figure on it is worked
 * out from pool counts read at one instant, and the instant is the server's
 * `generated_at`. `the whole catalogue`: the response carries every profile,
 * not a page of them.
 */
function CatalogueCount({ capacity }: { capacity: Capacity }) {
  const profiles = Object.values(capacity.runner_profiles)
  const backends = new Set(profiles.map((p) => p.backend)).size
  return (
    <>
      {profiles.length} profiles · {backends} backend{backends === 1 ? '' : 's'} · the whole catalogue ·{' '}
      <CountsAge generatedAt={capacity.generated_at} />
    </>
  )
}

/**
 * `counters last reported 2m ago, one pool at a time`, on the age tick, so it
 * moves while the page is open. The `<time>` carries the server's own instant.
 *
 * REPORTED, NOT READ (visual QA pass 2, cap2-12, 2026-10-29): 'counts read
 * 10d ago' sat beside the refresh chip's 'read 0 s ago', two reads of one page
 * ten days apart. They are two instants: the chip is when this page last asked,
 * and `generated_at` is when the server's counters were last written. This one
 * says it is the counters' report time; the chip keeps the read.
 */
function CountsAge({ generatedAt }: { generatedAt: string }) {
  const now = useNow(AGE_TICK_MS)
  return (
    <>
      counters last reported{' '}
      <time
        dateTime={generatedAt}
        title={`${generatedAt}: when the server last reported these counters. The refresh control says when this page last read them.`}
      >
        {timeAgo(generatedAt, now)}
      </time>
      , one pool at a time
    </>
  )
}

/**
 * The two `<dt>`/`<dd>` pairs this screen used to end on, as links -- and then
 * as a footer index, until QA1-FIX-24 (capacity-13, part of #2596) moved it
 * behind the title's `?` (`help` above), as VQ-033 did Ceilings'.
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
