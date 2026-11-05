# sc-b: direction B, "Action bar and stat tiles": notes (after fix-b)

Mock-ups only: no commit and no pull request. Live at `/workflows/<id>?sc=b` on the fixture dev server. `&scx=running|finished|failed|parked|attempts` pins a fixture, `&sca=<n>` pins the pager, `&scd=1` opens Details. Every fact on the card is an invented fixture (src/StepCardB.tsx, MOCK-HOOK), now chosen to agree with the graph's own nodes.

+ All frames: with ?sc=b the card is the step's whole summary. The agent block that used to sit under it (state again, title again, attempt line, 'account not read', › links, tabs, Outcome prose) is not drawn; Agent opens all of it.
+ sc-b-running, sc-b-running-1280: elapsed keeps pace with the node (16d 16h). Attempt reads n of the allowed N, the wording the app already uses ("attempt 1 of 3"); its tooltip says so.
+ sc-b-failed: Retry is on the card's bar. The error is drawn once, on the card; the Needs-you row for the same step drops its copy while the card is open.
+ sc-b-parked: one line, 'Waits on scan-terraform'; the head's ↑ is its action.
+ sc-b-attempts: segment widths follow duration; the times under the bar are the selected attempt's.
+ sc-b-finished-390: the bar fits one row; the sheet's back control is a button.
+ All frames: the Needs-you box's quiet line is 'Nothing found'; the sentence naming what was not known is its tooltip (the chips already say 'not known').
! sc-b-running, sc-b-failed: Stop and Retry are inert in the mock. The real ones are StepCardExtras' Stop with its confirmation and NeedsYou's StepRetryButton.
! All frames: hiding the agent block also hides what it carried for some steps (a review verdict, the merge checklist, an await-input form). The mock does not draw those; shipping this needs a home for them on the card or behind Agent.
! sc-b-running-1280: rendered as a 1280x800 frame inside the desktop browser, because the live browser has no 1280x800 preset.
