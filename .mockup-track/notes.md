# Direction "track": the activity timeline

**The idea.** Talk becomes two columns: the conversation on the left (voice orb, captions, one working card), and on the right an **Activity** timeline that grows as the ask is worked. All the rows share one time axis. The top lane is the **voice session** itself: solid while it talks, striped while it is paused. Under it, each hand-off is a row: who took it, what it is doing now, its own elapsed time, and a bar from when it started to now. Agents sit indented under the planner that called them. Each row is a button that opens that agent's or workflow's console page. While the voice is paused, a slim band pins to the top of Talk with *Paused · time · $ saved · (i) · "resume" · Resume*, and the timeline keeps moving under it. On a phone it is one column: the band stays pinned, the conversation keeps its newest lines in a short scroller, and the timeline follows under it.

Built at `?voiceactivity=track&vstate=<state>` (MOCK-HOOK). The new files are `apps/swarm-ui/src/voice/TrackMock.tsx` and `src/styles/talk-track.css`, and there is one marked line in each of `voice/Section.tsx` and `voice/Projects.tsx`. Everything comes from invented fixture data: the server side of pause and of the roster is not built.

## What it gets right

+ **Time is the organising idea.** One axis answers "how long has this taken, and who is slow" at a glance. In track-handed the three bars start in a staggered row at 0:13–0:15, just after the planner's bar ends. That shows the hand-off itself, without a word.
+ **The pause shows its own value.** The session lane's striped stretch is the pause, sitting next to the agents' solid bars, and "$0.38 saved" is printed on it (track-resumed, track-ended). The person can see that the voice was off while the work went on.
+ **Nesting matches who called whom.** The agents and the workflow sit under the Planner with a tree line (track-handed). A deeper plan nests the same way.
+ **One working card, not a stream.** The conversation stays a conversation. Collapsed, the card is one line (track-working-collapsed). Opened, it is a dated log, newest at the bottom (track-working-open). The next reply replaces it (track-resumed).
+ **Failure is local.** The failed agent's row turns red, with its reason behind an (i) and a Retry button. Its slot on the Stage is held open with a second Retry, so the person sees which version is missing (track-failed-agent).
+ **Rows are buttons to real pages** (`/agents/running/<id>`, `/workflows/<id>`), never arrow links. No prose sits on the surface: the definitions of the pause, the "resume" word and the saving are all in tooltips.
+ **Resume both ways.** There is a button, and the "resume" chip shows the spoken word the browser's own voice detection listens for (track-paused, track-paused-390).
+ **Projects carries the outcome.** A Work column ("3 versions delivered", "1 agent working") sits next to Resume (track-projects).

## What it costs

! **Width.** The timeline takes about 400 px of the desktop's width for as long as the session runs. When the Stage is up, the Stage spans the full width and the orb shrinks to a small ring beside the captions (track-resumed). With the timeline, there is less room for the conversation than today's single column gives it.
! **On a phone the roster comes after the conversation.** To see the agents in track-handed-390 and track-paused-390 the person scrolls past the talk. The orb is dropped there, and the conversation is capped to its newest lines in a short scroller.
! **The axis rescales as time passes** (0:30 → 1:30 → 3:00), so the bars shrink while the person watches. Fine for minutes. A run of an hour would need a log scale or a "last 5 min" window.
! **The reason tooltip sits over the conversation.** In track-failed-agent it opens to the left of the timeline, level with its row, so neither the failed row nor the next one is hidden; while open it covers the end of the newest captions.
! **Two clocks.** The band's "Paused 0:55" (how long the voice has been off) sits close to the card's "1:36" (time since the ask). Both are right, but they read as a mismatch at first glance (track-paused).
! **The saving is an estimate** at an invented rate ($0.18 a minute). The real figure needs the ledger to price a pause.

## Not built (server side, for a later step)

The pause/resume itself, the roster events (planner → agents → workflows with progress), the "resume" keyword listener, Retry, and the saving computed from the ledger. The mock draws all of these from fixture data only.
