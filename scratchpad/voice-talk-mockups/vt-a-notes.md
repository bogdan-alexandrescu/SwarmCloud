# Voice it › Talk on own speech — direction A: the Stage (`?vt=a`)

**The idea.** The page is a stage: whatever the session is talking about (the UI mock-up, the
architecture diagram, the code slice, the failed step's log) fills it, so you and the agent are both
looking at the thing itself. Everything else sits at the edges. The visuals are a row of tabs along
the top, each with its pin count. The budget line is at the top right. The captions are a thin strip
under the visual (three lines, newest last, the words being heard shown with a caret). Proposal and
read-back cards slide in at the right edge and slide out once they are answered. The voice is a ring
around Start/Stop at the bottom centre, with the status word or two beside it. Note pins sit on the
visual, and a note opens from its pin. The page carries no prose. The engine name ("own speech",
"GPT-Live", "this browser") is a dotted tick, and its explanation is in a tooltip, as are the budget's
breakdown and the read-back clock's rules.

Built in the real console (`apps/swarm-ui`, fixtures, every line MOCK-HOOK) at
`/voice/talk?vt=a&s=<scene>`. The scenes are rest, starting, starting-fetch, hearing, thinking,
speaking, visual-mockup, visual-code, note-adding, readback-pending, readback-confirmed,
readback-refused, readback-expired, reconnecting, engine-down, no-l4, budget-80, budget-cap and
gptlive. A region
dragged on the visual is real: the note-adding frame was shot after an actual mouse drag. Go, Not that,
Approve and Not now really make their card leave. The pins open and close, and the visual tabs switch
what is on the stage.

## + What it gets right

+ **The visual is the conversation.** On a 1440 screen the mock-up, code or log is drawn at almost
  full width (vt-a-visual-mockup, vt-a-visual-code), so a pin or a pointed-at line can be read across
  the room. This is the direction to choose if visuals are the point of Talk.
+ **The agent's pointing reads at a glance.** In vt-a-visual-code line 42 is banded in the accent
  while the caption says "Line 42: …". In vt-a-speaking the diagram node breathes in a ring. Pointing,
  caption and ring all use the one accent colour.
+ **Answering a read-back takes nothing from the visual.** In vt-a-readback-pending the card
  ("Heard / Will cancel / run nightly-e2e #482 / Go / Not that") takes the right edge and the log moves
  left without being covered. Its 45-second clock is a small ring, and the expiry rule is in that
  ring's tooltip. In vt-a-readback-confirmed a spoken "go ahead" ticks it green and quotes the words
  heard, with no click.
+ **The machine's start is honest and brief.** vt-a-starting lists only the phases that happened,
  each finished one with a tick and its time (`starting machine 0:41`, `loading models 0:12`), beside a
  spinning ring; vt-a-starting-fetch is a start after a change (`updating speech server 1:12`,
  `fetching model qwen3-asr-0.6b`). The engine is named to the right of the phases, behind a rule, so
  it never reads as one more phase. Stop is live from the first second.
+ **The fallback is one line and the right button.** vt-a-no-l4 is the stock-out at Start: nothing
  spent, nothing shown, nothing said, one line "No L4 free in us-central1-a." with [Text] (it only
  dismisses: the session is already in text mode). Text mode draws no voice ring: a field, Send and
  Stop take its place, the status reads "text mode" and the engine tick "this browser".
  vt-a-engine-down is the same bar mid-session, after the speech server did not come back.
+ **Every state is a word or two.** thinking (a quiet pulse), reconnecting (the ring spins in the
  warning colour, Mute and Push to talk held faint), engine down, and budget cap (the session ended,
  the ring back to Start, the reason in the status word's tooltip).
+ **A read-back's four ends are all drawn.** Confirmed ticks green; refused marks "Not that" and quotes
  the "no" before it leaves; expired greys to 0s and stays for the record.
+ **GPT-Live is the same page.** vt-a-gptlive differs only in the engine tick and has no machine
  phases, so changing engine in Settings changes nothing the person has to learn again.
+ **The budget stays out of the way until it matters.** It is a tiny line at the top right, and at
  80% it turns to the warning colour and grows (vt-a-budget-80).

## ! What it costs

! **Rest and starting are a big empty stage** (vt-a-rest, vt-a-starting). With no visual and no
  prose, three quarters of the page is sky until the first visual arrives. That follows the rule
  ("empty sections are not drawn") but can look unfinished. The other direction may use that space
  better.
! **The ring is small** (112 px), so the voice has less presence than in today's centred Presence
  ring. At a distance the status word does more of the work than the trace.
! **Captions are three lines.** Anything older scrolls out of the strip (vt-a-speaking). A long
  answer can push the start of the agent's own sentence out of view while it is still speaking.
! **Cards narrow the visual.** With a card on screen (vt-a-speaking, vt-a-readback-*), the visual
  shrinks to about 70% of the width, so the diagram and the log are drawn with larger type to keep
  every label at 12 px or more there. Two cards at once would stack in a 320 px column.
! **Phone** (vt-a-hearing-390): the visual never drops below the 12 px text floor, so on a phone it
  is drawn at that scale in a square window that pans (both scrollbars show), opened at the start of
  the part being talked about. The pins are a pan away; the tab still counts them. Captions get the
  rest of the height, which suits hearing but not studying a whole visual at once.
! **Notes are separate from the transcript.** A spoken note shows in the captions and lands on its
  pin (vt-a-note-adding), but there is no list of all the notes on a visual, only pins and a count on
  each tab.
! **Mock-only shortcut:** the `?vt=a` hook skips the Voice it open/closed gate and the page head, so
  the stage fills the content area. A real build would keep the gate.
