def edit(f, pairs):
    s=open(f).read()
    for a,b in pairs:
        assert a in s, (f,a[:90])
        s=s.replace(a,b,1)
    open(f,'w').write(s)

edit('TimelineLanes.tsx',[
("import { PageHead, timeAgo } from './Shell'","import { CountNote, PageHead, RefreshControl, useClaimPageAge, useIdleStop, usePoll } from './Shell'"),
("export function TimelineLanesScreen({","""/**
 * How often both Timeline pages (Lanes and Outcomes) re-read while open:
 * every 60 seconds (#117, owner ruling 2026-10-07). Slower than the capacity
 * screens' 30 because a timeline is read for its shape over hours, and one
 * minute is a sliver of the narrowest span it offers. It polls through
 * `usePoll` (Shell.tsx): nothing while the tab is hidden, and nothing after
 * `IDLE_STOP_MS` without input, behind the head's `Paused · resume`.
 */
export const TIMELINE_POLL_MS = 60_000

export function TimelineLanesScreen({"""),
("""  const refresh = () => {
    setAnchor(Date.now())
    setNonce((n) => n + 1)
  }
""","""  const refresh = () => {
    setAnchor(Date.now())
    setNonce((n) => n + 1)
  }
  // THE CADENCE (#117) and THE ONE AGE (#98): a re-read every
  // `TIMELINE_POLL_MS` re-anchors the span at the read, as a refresh does,
  // and the head's refresh control carries the age, so the frame's head
  // prints none.
  const { idle, resume } = useIdleStop(true)
  usePoll(TIMELINE_POLL_MS, refresh, idle)
  useClaimPageAge(true)
"""),
])
s=open('TimelineLanes.tsx').read()
a=s.index("      <PageHead title=\"Timeline\">\n        {rangeWords(win.since, win.until)}")
b=s.index("      <TimelinePages at=\"lanes\" />\n",a)+len("      <TimelinePages at=\"lanes\" />\n")
s=s[:a]+"""      {/* TITLE LEFT, ACTIONS RIGHT (#138): the refresh, with its ticking
          age and the cadence, is the head's one action; the span and the
          lane count are the note over the first card. */}
      <PageHead title="Timeline">
        <RefreshControl
          readAt={shown === null ? null : shown.fetchedAt}
          now={now}
          cadence={{ base: TIMELINE_POLL_MS, wait: TIMELINE_POLL_MS }}
          reading={att.status === 'loading'}
          idle={idle}
          onRefresh={refresh}
          onResume={() => {
            resume()
            refresh()
          }}
        />
      </PageHead>

      <TimelinePages at="lanes" />

      <CountNote>
        {rangeWords(win.since, win.until)} · {shown === null ? 'reading…' : `${allLanes.length} ${allLanes.length === 1 ? 'lane' : 'lanes'}`}
      </CountNote>
"""+s[b:]
open('TimelineLanes.tsx','w').write(s)

edit('Activity.tsx',[
("import { Id, PageHead, Screen, timeAgo } from './Shell'","import { CountNote, Id, PageHead, RefreshControl, Screen, timeAgo, useClaimPageAge, useIdleStop, usePoll } from './Shell'"),
("import { TimelinePages } from './TimelineLanes'","import { TIMELINE_POLL_MS, TimelinePages } from './TimelineLanes'"),
("""  const refresh = () => setNonce((n) => n + 1)
""","""  const refresh = () => setNonce((n) => n + 1)
  // THE CADENCE (#117: both Timeline pages every `TIMELINE_POLL_MS`) and THE
  // ONE AGE (#98), on the head's refresh control.
  const { idle, resume } = useIdleStop(true)
  usePoll(TIMELINE_POLL_MS, refresh, idle)
  useClaimPageAge(true)
"""),
])
s=open('Activity.tsx').read()
a=s.index("      <PageHead title=\"Timeline\">\n        {data === null ? (")
b=s.index("      <TimelinePages at=\"outcomes\" />\n",a)+len("      <TimelinePages at=\"outcomes\" />\n")
s=s[:a]+"""      {/* TITLE LEFT, ACTIONS RIGHT (#138): the refresh, with its ticking
          age and the cadence; the range and the cache are the note over the
          first card. The age is the payload's (`generated_at`), which a
          cached answer carries from when it was counted. */}
      <PageHead title="Timeline">
        <RefreshControl
          readAt={data === null ? null : Date.parse(data.generated_at)}
          now={now}
          cadence={{ base: TIMELINE_POLL_MS, wait: TIMELINE_POLL_MS }}
          reading={pending}
          idle={idle}
          onRefresh={refresh}
          onResume={() => {
            resume()
            refresh()
          }}
        />
      </PageHead>

      {/* Outcomes is the Timeline's second page now (timeline.html pick A); Lanes is /timeline. */}
      <TimelinePages at="outcomes" />

      <CountNote>
        {data === null ? null : (
          <>
            {rangeWords(data)}
            {data.cached && ` · from the ${OUTCOMES_CACHE_S} s cache`}
          </>
        )}
      </CountNote>
"""+s[b:]
open('Activity.tsx','w').write(s)
