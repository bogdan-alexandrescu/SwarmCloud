# Direction D — fixes for the 18 review findings (step fix-d)

Mock-ups only: no commit of product code is meant to ship, no pull request. Every frame was re-rendered
after the last change (18 frames: the 12 asked for plus 6 that show what findings 9 and 11 asked for), light in
`frames/`, dark in `dark/`, each looked at in both themes.

| # | Finding | What changed | Checked in |
|---|---|---|---|
| 1 | Phone: "your turn" chip clipped on "Upgrade booking API client" | The name cell is a flex row: the step/title shrinks with an ellipsis, the marks (your turn, question, silent) never shrink, so the mark stays inside the cell. Probe at 390px: mark x 253–316 inside the cell 169–322. | D·9 |
| 2 | Dark frames show the theme switch on "Light" | Frames are rendered through the console's own theme setting (`swarm.theme`), not by forcing `data-theme`, so the switch reads Dark on every dark frame. | all dark/ |
| 3 | Address strip "https://example.com" disagrees with the still | Each fixture browser carries its page's address; the strip reads the still's own address (staging.northwind-crm.example.com). | D·2, D·5 |
| 4 | All-caps "LIVE" badge on the Browser tab | The tab badge is the lowercase "live" mark used everywhere else. | D·2b |
| 5 | Page-level scrollbar (scrollHeight 894 vs 871) | The two panes are sized to the page scroller; probe on agents, agent, workflows, workflow rest/watch/needs: scrollHeight 871 / clientHeight 871. | D·1–D·7 |
| 6 | Phone top bar says "Agents" on a workflow; "click one to watch it in the inspector" | Workflow routes title "Workflows"; the strip says "pick one to watch it" (a sheet on the phone, the inspector on desktop). | D·9b |
| 7 | review-invoices drawn running after build-invoices failed | Fixture: review-invoices waits (parked on its failed parent), no agent row, no still. The workflow now reads ▶9 running, ◷25 waiting, ✓5 done, ✗1 failed, 9 browsers; captions and notes say nine. | D·4–D·9b, D·4c |
| 8 | Owner, Attempt, PR columns never visible | At rest (browser wall docked) the table keeps Owner, Running/Waiting on/Ended, Attempt, Cost and PR; they fold only while one agent is open beside it (listed in notes.md costs). | D·1, D·1b, D·1c |
| 9 | No frame draws Waiting or Recent; no "silent" mark; no cancelled note | New frames D·1b (Waiting, grouped by why it waits, "in line 25") and D·1c (Recent, failed and cancelled rows). One live agent is past its heartbeat grace and carries the red "silent" mark (D·1). The cancelled row reads "· by request" with "cancelled by request" as its tooltip, so the step's own name keeps its width. | D·1, D·1b, D·1c |
| 10 | No Jev/Playwright badges; no hand-off "I'm ready", heartbeat age, Children | The mock live session reports its tool, so the strip draws "Auto: Playwright", "Direct", "Jev". Header shows "heartbeat 24s ago"; Children is a tab with its count; new frame D·2c shows the hand-off banner with "I'm ready". | D·2, D·2c, D·5 |
| 11 | Only the Graph view drawn | New frames D·4b Table, D·4c Steps over time, D·4d Changes, each beside the inspector. | D·4b–D·4d |
| 12 | Merge strip ellipsised, the pull requests cut off | The strip wraps; "after #2401 #2402" never shrinks; the reason moves to its own line. | D·4–D·9b |
| 13 | With an agent or watch wall open, Needs you collapses to a bare count | The bar keeps every counted chip and wraps to a second line when narrow (Failed, Question, Merge waiting, Your turn in a browser, Approvals). | D·2, D·2b, D·6 |
| 14 | Needs-you chips and "Browsers only" in mono, bordered pills | Chips are sans, no border, no radius: a coloured dot, the word, the number (design-system §6.6); "Browsers only" is a plain checkbox. Glyph form only when the bar is narrow, the word kept as label and tooltip. | all |
| 15 | Step inspector puts Changes under "More" at 1440 | Overview, Browser, Logs, Changes and Attempts stay tabs at 1440; only the rest go under More. | D·5 |
| 16 | Workflows list: "$1.00 so far" on every row; running rows' bar has no running segment | Fixture: each small workflow has its own spend; finished ones show the amount, running ones "$0.55+" ("+" = still adding up, said in the tooltip); running workflows have a running step, so the bar draws its running segment. | D·3 |
| 17 | Watch: "live" badge covers the large still's address; build-contacts drawn twice | The large view has its own address line ("live" + address) above the still; the wall lists the other eight, not the large one again. | D·6 |
| 18 | "0 err ln" in the log bar | In the inspector the log bar keeps the words ("No error matched") and wraps instead of switching to the short labels. | D·2b |

Beyond the findings: nothing. The cancelled-note change in 9 was the only change this session made after the
earlier session's fixes; everything else above was re-checked in code, by probe and in the re-rendered frames.
