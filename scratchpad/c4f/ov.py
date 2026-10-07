f='Overview.tsx'
s=open(f).read()
def rep(a,b):
    global s
    assert a in s, a[:90]
    s=s.replace(a,b,1)
rep("import { PageHead, timeAgo } from './Shell'","import { AGED_AFTER_MS, CountNote, PageHead, RefreshControl, staleFoot, timeAgo, useClaimPageAge, useIdleStop, usePoll } from './Shell'\nimport { AGE_TICK_MS, useNow as useAgeClock } from './useNow'")
rep("""  // NEITHER TIMER RUNS WHILE THE TAB IS HIDDEN (#168, docs/web-ui §2.5).
  usePoll(POLL_MS, () => setLive((n) => n + 1))
  usePoll(STATS_POLL_MS, () => setCounted((n) => n + 1))
""","""  // NEITHER TIMER RUNS WHILE THE TAB IS HIDDEN (#168, docs/web-ui §2.5), AND
  // NEITHER RUNS AFTER FIFTEEN MINUTES WITHOUT INPUT (#117): the head's
  // control says `Paused · resume`, and resuming reads everything at once.
  const { idle, resume } = useIdleStop(true)
  usePoll(OVERVIEW_POLL_MS, () => setLive((n) => n + 1), idle)
  usePoll(STATS_POLL_MS, () => setCounted((n) => n + 1), idle)
  const resumePoll = useCallback(() => {
    resume()
    refresh()
  }, [resume, refresh])
  // ONE AGE ON THIS SCREEN (#98): the head's refresh control carries it, so
  // the frame's head prints none.
  useClaimPageAge(true)
  const clock = useAgeClock(AGE_TICK_MS)
""")
a=s.index("      {/* TITLE, TENANT, PROVENANCE ON ONE LINE (O1 `.phead`).")
b=s.index("      </PageHead>\n",a)+len("      </PageHead>\n")
s=s[:a]+"""      {/* TITLE LEFT, ACTIONS RIGHT, NOTHING UNDER IT (#138, owner ruling
          2026-10-07): the title and the section's `?`, and on the right the
          quiet refresh carrying this screen's one ticking age and its
          cadence (#98, #117). */}
      <PageHead title="Overview">
        <RefreshControl
          readAt={newest}
          now={clock}
          cadence={{ base: OVERVIEW_POLL_MS, wait: OVERVIEW_POLL_MS }}
          stale={newest !== null && clock - newest > AGED_AFTER_MS}
          reading={pending > 0}
          idle={idle}
          onRefresh={refresh}
          onResume={resumePoll}
        />
      </PageHead>

      {/* THE COUNT, AS THE NOTE OVER THE FIRST CARD (#138). The scope is not
          decoration: `/v1/stats` and `/v1/tasks` are tenant-scoped while the
          `global` pool is platform-wide. Still reading is not missing (OV-9):
          the dash is for a read that landed without a tenant.

          THE TALLY SAYS HOW MUCH OF THIS PAGE IS REAL: `6/8` means two of
          the reads behind the cards below did not land, and every em dash
          further down is one of those two. Its dot is the severity, its
          accessible name the sentence. */}
      <CountNote>
        {tasks.status === 'loading' ? (
          <span className="ov-reading">tenant reading…</span>
        ) : tenant === null ? (
          <>
            tenant <i className="ctl-em" title="The task read carried no tenant id.">&mdash;</i>
          </>
        ) : (
          `tenant ${tenant}`
        )}
        {' · '}
        <span className="ov-tally" aria-label={readTally(reads.length, landed, pending, refused, broken)}>
          <ToneMark tone={tallyTone(pending, refused, broken)} />
          <span className="ov-num">
            {landed}/{reads.length}
          </span>{' '}
          reads
          <HelpCard topic="absent-vs-zero" />
        </span>
      </CountNote>
"""+s[b:]
rep("""  const tenant = dataOf(tasks)?.tenant_id ?? null""","""  // The newest reading behind this screen, for the head's one age (#98).
  const newest = reads.reduce<number | null>((m, r) => {
    const at = ageOf(r)
    return at === null ? m : m === null ? at : Math.max(m, at)
  }, null)
  const tenant = dataOf(tasks)?.tenant_id ?? null""")
rep("""/**
 * How often the cheap reads re-run.
 *
 * NAMED, because the spend panel has to say that it does NOT move on this
 * cadence, and the only honest way to say that is to render the figure the
 * timer actually uses.
 */
const POLL_MS = 20_000""","""/**
 * How often the cheap reads re-run: every 20 seconds, unchanged by the
 * 2026-10-07 cadence ruling (#117, "Overview stays 20000").
 *
 * NAMED, because the spend panel has to say that it does NOT move on this
 * cadence, and the only honest way to say that is to render the figure the
 * timer actually uses.
 */
export const OVERVIEW_POLL_MS = 20_000""")
a=s.index("/** `document.hidden`, false where there is no document (tests/run.mjs). */\nfunction tabHidden()")
b=s.index("// ---------------------------------------------------------------------------\n// Read plumbing",a)
s=s[:a]+s[b:]
rep("""/** "read 40s ago", or nothing at all when there is no reading to date. */
function footFor(r: Result<unknown>, verb: string): string | undefined {
  const at = ageOf(r)
  return at === null ? undefined : `${verb} ${timeAgo(at)}`
}""","""/**
 * A card foot's freshness clause: `absent` when there is no reading to date,
 * `from 6 min ago` when the reading is stale (a failed refresh, or older than
 * `AGED_AFTER_MS`), and nothing at all while it is fresh -- the head's
 * refresh control carries a fresh age (#98, owner ruling 2026-10-07).
 */
function footFor(r: Result<unknown>, absent: string): string | null {
  const at = ageOf(r)
  return at === null ? absent : staleFoot(at, Date.now(), r.status === 'stale')
}""")
rep("""          <span>{footFor(state, 'read') ?? 'not read'}</span>""","""          {footFor(state, 'not read') !== null && <span>{footFor(state, 'not read')}</span>}""")
rep("""          <span>{footFor(state, 'summed') ?? 'not summed'}</span>""","""          {footFor(state, 'not summed') !== null && <span>{footFor(state, 'not summed')}</span>}""")
rep("""  const countedAge = countedAt === null ? null : timeAgo(countedAt)""","""  // Said only when stale (#98): a fresh count's age is the head's.
  const countedAge = countedAt === null ? null : staleFoot(countedAt, Date.now(), stats.status === 'stale')""")
rep("""          {reading.length > 0
            ? `${reading.length} of ${checks.length} checks still reading`
            : `${found} ${found === 1 ? 'check' : 'checks'} of ${checks.length} · derived on this read`}""","""          {/* THE LEAD FRACTION, LABELLED (#98, owner ruling 2026-10-07):
              `checks 8/8` is how many of the checks ran, so it cannot be
              read as the reads tally or as a count of problems. */}
          {reading.length > 0
            ? `${reading.length} of ${checks.length} checks still reading`
            : `checks ${ran}/${checks.length} · ${found} found something`}""")
open(f,'w').write(s)
