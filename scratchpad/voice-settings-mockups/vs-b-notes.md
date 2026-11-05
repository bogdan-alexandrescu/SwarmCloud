# Direction B: Cards and a timeline (`?vs=b`)

**The idea.** Settings is four cards, one per question: **Engine** (own speech or GPT-Live, who answers and who plans, the fallback, and the OpenAI key's status with its link button), **Machine** (the four single-L4 sizes with their price per hour, the one zone), **Models** (speech-to-text, text-to-speech, voice) and **Behaviour** (idle stop, the read-back verbs). A change, a refusal or a conflict shows as a band on the card it belongs to, so the page never has a global banner. Telemetry is one timeline, top to bottom: a live band (stopped line, start phases, or the running session), a heat strip of the last 30 days, then each session as a row with its latency and GPU sparklines that opens into its detail.

Open it on the fixtures: `/voice/settings?vs=b&vsstate=settings-<state>` and `/voice/telemetry?vs=b&vsstate=telemetry-<state>`, as a platform admin (`swarm_qa_persona=platform-admin` cookie).

## + What it gets right
+ **Each change shows where it lives.** A pending machine size is one band on the Machine card ("At next start g2-standard-4 → g2-standard-8", Undo), said once (vs-b-settings-pending); once a start uses it, the band says Applied (vs-b-settings-applied); a start that found no free L4 says so on the same card (vs-b-settings-stockout). A refusal names its field on its card (vs-b-settings-refused); a conflict names who saved, when and their value, with Use theirs / Save mine over it (vs-b-settings-conflict).
+ **Bogdan's answers are drawn.** Engine shows the small Claude model answering and the Claude planner planning, and no OpenAI on own speech; the OpenAI key sits under the engine switch as GPT-Live's; one zone, shown as a fact, not a picker; text mode as the fallback (vs-b-settings-admin).
+ **Price is next to the choice.** Each machine row carries its price per hour (where the prices come from is in the (i) over the column) and a bar for comparison; the disk's monthly cost and the last start's length are one line under it.
+ **A model fetch is visible where the model is picked** (vs-b-settings-fetching): progress, GB and time on the Models card, and a "fetching" tick beside the field.
+ **Read only is the same page without controls** (vs-b-settings-readonly): values only, the machine as a plain row, one "Read only" mark in the head, viewed as a person who is not a platform admin.
+ **A failed read is never drawn as settings or as an empty timeline** (vs-b-settings-error, vs-b-telemetry-error): one failure line with its status and time, and Retry.
+ **Telemetry reads top to bottom, now → days → sessions.** The live band shows the start's phases against the four-minute limit (vs-b-telemetry-live-starting) and, in a session, per-utterance latency with its p90 rule plus GPU, GPU memory, CPU, memory and disk (vs-b-telemetry-live-session).
+ **A session row answers "how was it" at a glance**: latency and GPU sparklines with p90 and peak, machine minutes, cost; a stock-out and a crash are ticks on the row (vs-b-telemetry-sessions). Opening one shows start phases, p50/p90/max, peaks, machine minutes and cost, crashes (vs-b-telemetry-session-detail).
+ **Days are a strip you can pick**: colour by machine minutes (what cost counts), cost, utterances, p90 or stock-outs; picking a day filters the list and puts the day's totals in its head (vs-b-telemetry-days).
+ **No prose on the surface**: every explanation is an (i) tooltip or a tick's title; the empty telemetry page draws no charts, only "No sessions yet" and Start talking (vs-b-telemetry-empty).

## ! What it costs
! **Settings is long on a phone**: four cards stacked (vs-b-settings-admin-390); the Machine card alone is a screen high.
! **A long history scrolls** (vs-b-telemetry-sessions): over 90 days the strip is three rows and the list is below the fold.
! **The four-minute limit is only a tick** at the end of the start track (vs-b-telemetry-live-starting); its meaning is in the tick's tooltip, not on the surface.
! **Five day metrics on one strip** means the colour's meaning changes with the segmented control; the totals line stays fixed so the numbers do not.
! **Per-card bands can stack** if a card is both pending and in conflict; the mock-ups draw one band per card.
! **Prices other than g2-standard-4's $0.71 are invented** (MOCK-HOOK) until the settings lane reads the Billing Catalog.

All values are invented fixtures (MOCK-HOOK): a session about a workflow page's redesign, thirty days of sessions, the catalogue's four machines.
