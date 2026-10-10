# SwarmCloud console mock-ups and the owner's picks

These are the approved design mock-ups for the console, kept here so that a
lane building a screen can read the frames it is building. Every page is
standalone HTML: open it in a browser. All data on them is **example data**;
the user is "Operator" and hosts are `swarm.example.com`.

Each page has an inventory measured from the code at the time it was drawn,
variants A/B/C with Idea / Pros / Risks, and a recommendation. **Build the
picked variant only.** The other variants are kept because the reasons they
lost are the record of the decision.

## Rules that override the mock-ups

These were decided after batch 1 and 2 were drawn, and win wherever a frame
disagrees:

* **Type:** 12px floor, no all-caps, no letter-spacing. The batch 1-2 frames
  still draw small caps eyebrows; do not copy them.
* **Theme:** a light / dark / system toggle in the panel's user footer.
* **Honesty:** an unknown figure is a dash with its reason, never 0
  (docs/web-ui/redesign-v2.md, "Honesty rules").

## Batch 1-2 (picked 2026-10-01; the console is built on these)

| Page | Pick |
|---|---|
| brand.html | Palette A "Open sky" (accent #0369a1 light / #7cc8f8 dark), mark M1 Hive; spine logo 44px Hive in a white→#7dd3fc gradient, no tile |
| navigation.html | V2 Sky spine: 84px spine of sections + 236px panel of the section's pages; Submit is the sky button at the top of the spine; phone header takes the spine colour |
| overview.html | O1 Lead and ledger |
| agents.html | V1 compact list + wide detail, collapse toggle, divider snapping 64px / 380px / 50%, width remembered per device |
| workflows.html | V2 full-width pages (/workflows list + /workflows/<id>), Recent (5) switcher in the panel |
| capacity.html | Ceilings table by family with "Needs a look" first; Accounts as a split view; polling Pools/Holders 30s, Accounts 60s, paused in hidden tabs |
| admin-help.html | Pool limits = L2 side editor with impact and history, typed confirm for 0 or a cut of half or more; Help = H1 one page per group with search |
| submit.html | /submit chooser page (N opens it); one-page task form; workflow form stacked top to bottom with on_step_failure and priority in it |

## Batch 3 (picked 2026-10-02)

| Page | Pick |
|---|---|
| components.html | **A as built**: 32px rows, 8px controls, 12px cards, pill chips; control border token at 3:1 (#7d8ea3 light / #5f7087 dark); radius scale 4/8/9/12/14/999; table heads 12px sentence case; toasts only acknowledge the viewer's own action (failures stay as banners); bars 6px; hatching means only "not measured". One canonical set replaces the 12 button / 11 chip / 7 bar styles in the live app |
| states.html | **C three tiers**: whole-app states take over the content area with the nav still usable; page states are one banner over dimmed older data; region states show in place. Plus: typed confirm on Cancel workflow while a step runs; a "tab was hidden, reading now" line; an offline banner |
| timeline.html | **A swimlanes**: one lane per agent grouped under its workflow; bars are attempts holding capacity, hatched bands are parks (no capacity), a cut bar is a fenced generation. Two pages: /timeline Lanes (default) and /timeline/outcomes (today's ledger, unchanged). Reads /v1/attempts and events; no new route |
| intake-tenants.html | **1A**: a third card on /submit opens /submit/issue, with the issue previewed before submit (title, body, labels), runner, plan approval, auto-merge off by default and disabled until #295, fix rounds (3, range 1-5); a closed issue warns with "Plan it anyway"; submitting lands on a new Work › Runs page at /runs/<id>. **Tenant switcher 2A** on top of #501: collapsed-spine tenant tile, phone header chip, a switch-back toast, and an unsent form is KEPT with its button naming the new tenant |
| agent-detail-2.html | **A in the split detail**: a Children tab on a parent and a "child of …" link on a child; a verdict card on a review step (findings by severity, "Not graded" while the verdict has no severity field); the merge step's ordered checklist card; the Code card opens with one sentence on what happened, then the base-pin line, then the per-file diff; FAILED published_nothing and PUBLISH_REFUSED drawn |
| viewers.html | **The log dock is SUPERSEDED (owner decision 2026-10-04): logs are a tab.** The owner does not want the log component overlapping the agent details, so the bottom dock -- its strip, resize handle, `Open full` overlay and bottom sheet -- is gone. A full-height **Logs** tab sits beside Details / Attempts / Artifacts / Checkpoints / Children at `/agents/<tab>/<id>/logs` and fills the whole detail area under the header: the four streams as a segmented control (Transcript / Agent stdout / Agent stderr / Runner), attempt picker by generation, search, Follow (live agents only), Wrap, jump to error, masked count, the notice when output is missing between two reads, and the stream metadata (gs path, size) folded by default; one row of controls, overflow into More. A running agent opens on Logs, a finished one on Details, whose one-line last log line opens the tab. A phone has the same tab, full screen. Pick A's viewers for text, JSON, markdown, image, binary and checkpoint contents stand |
| wide-workflows.html | **A, extends what is live**: stage rows top to bottom, a stage never wraps; a wide stage's band gains a state-mix bar, failed → running → parked steps as chips, "holds N slots · M waiting hold none", edges bundled one lane per target and lit on hover/select, 1:1 children aligned under their parents; a band count opens the Table filtered to that stage and state. A step skipped by its verdict gate (`result_summary.verdict_gate.agent_ran: false`) gets its own look |

The live app was audited against the batch 1-2 picks on 2026-10-02: #503.

## Agent Details tab, v3 (picked 2026-10-04)

| Page | Pick |
|---|---|
| agent-details-v3.html | **A as drawn**: a 2-line header (state pill, title, 'elapsed · attempt n of N', Copy link / Stop / close; one sans meta line with profile · class · workflow link · account, the task id behind copy); a **Now** card (current phase, last log line, latest checkpoint, Open logs); ONE stat strip (Elapsed · Attempt · Peak memory with % of limit · CPU peak/limit · Cost, unknown values small and muted 'at exit'); Progress (compact phases + last 5 events) and Resources (thin bars with % of limit, charts behind 'details') side by side from a 640px pane; Input folded (3-line prompt preview, metadata behind a disclosure); Dispatch as plain-language chips in the header meta; one '?' per card instead of the help-link block; an Outcome card (finished) or a failure card (failed) leads; tokens under Cost; checkpoints in the Now card; lease/attempt ids on the Attempts tab; mono only for ids and values; ONE scroll container. Replaces the Details layout of agent-detail-2.html. Logs stay a full-height tab (owner, 2026-10-04). |

## Repositories, repository index and git tokens (picked 2026-10-05)

From `repositories.html` (lanes RI0, #573, and RI0b, #578) and the designs in
[`docs/repo-index.md`](../../repo-index.md) and [`docs/git-tokens.md`](../../git-tokens.md).

**The owner's picks:**

| Screen | Pick |
|---|---|
| 2 · Repositories list | **B, cards**: one card per repository with the index at a glance (freshness against the head, last indexed, schedule, tests mapped). This overrides the page's recommendation, A |
| 3 · Register repository | **C, pick from the token**: list what the tenant's git token can read, pick one, then set the schedule and on-change trigger. Overrides A |
| 4 · Repository detail | **A, tabs**: Overview, Test map, Hot-spots, Index runs, Settings, Used by |
| 5 · The index inside an issue-run plan | **B, a Context card** above the plan: the index used, its freshness, the modules and impact it found. Overrides A with C's per-step tests |

The owner said "for second pass mock-ups go with your own recommendations"
(2026-10-05), so **the operator picked these**. Each stays open to the owner's
override:

| Screen or decision | Pick | Why |
|---|---|---|
| 8 · Graph explorer | **A**, a force-directed canvas with a side inspector | The page's recommendation. It is the only variant that shows the module, call and test graphs on one surface |
| 9 · Commit / PR impact | **A**, four columns (diff → changed symbols → callers → tests) | The page's recommendation. Each column answers one question in order |
| 10 · Selected-tests gate = merge policy | **C, P3**: the selected tests gate the merge, and a change touching build or test config, shared fixtures, or symbols with low-confidence edges falls back to the full suite | P1 lets a missed edge merge a regression with no full run before merge. P2 keeps the full suite on every PR, so selection buys only a faster red. P3 is fast on the common case and safe where the graph is weakest. The page made no recommendation, because this is a policy decision |
| Execution mode (repo-index.md §4.4) | **X2**: SwarmCloud passes the selected test list to the repository's own CI (`workflow_dispatch` inputs), which posts `swarmcloud/selected-tests` | No frozen-contract change (X1 needs request C), and it runs in each repository's own CI environment, which is multi-repo from the start. X1 stays available per repository later |
| 11 · Repository Settings | **A**, cards | The page's recommendation |
| 12 · Git tokens | **B**, cards plus a command line. **No console paste box** in phase 1 | A forge token enters only through `scripts/create-secrets.sh --stdin` (CLAUDE.md, owner rule 2026-09-25). A paste box is a new path for a token value through the API, which is the owner's to open, not the operator's. B shows the exact command per slot |
| 13 · Permission matrix | **A**, a grid of tokens × repositories, one cell per capability | The page's recommendation |
| Token resolution order (git-tokens.md §3.1) | **R2**: repository token, then tenant token. A user's token is used for attribution only | A task never fails because the dispatching user's token lacks a right that the repository or tenant token has. R1 can refuse work that R2 would do |
| User-token isolation (§2) | **U1**: a user token is a tenant secret attributed to a person | Simplest for phase 1 and enough under R2, where a user token never clones or pushes. U2 (personal tenants) and U3 (a reader identity per user) stay open if user tokens ever push |
| Secret creation (§2) | **S1**: Terraform declares each slot, and values come only from `create-secrets.sh --stdin` | The doc's recommendation. It adds no platform privilege, and the registry does not depend on self-service |

## Onboarding: connect GitHub, enable orgs, choose repositories (picked 2026-10-07)

From `onboarding.html` and the design in [`docs/onboarding.md`](../../onboarding.md)
(#780). The owner's decision D10 on #780, 2026-10-07: chooser A, and the
recommended variant of every other screen. Built by lane OB8 as
`apps/swarm-ui/src/Onboarding.tsx` (Work › Setup, `/setup`) and
`apps/swarm-ui/src/Access.tsx` (Work › Access, `/access`).

| Screen | Pick |
|---|---|
| 1 · Entry and checklist | **A**: a checklist card on Overview until every step is done, the six step ids the API serves, the failing step's §2.3 copy inline. Hide keeps the steps; Work › Setup still shows them. Hide posts `POST /v1/onboarding/dismiss` (built, #780 step 3), so it is kept per person on the server, in every browser, and read back as `dismissed` from `GET /v1/onboarding`; Work › Setup's Show on Overview posts `{dismissed: false}` |
| 2 · Connect GitHub | **A**: one Connect GitHub button for the App (built by OB3 as `GitHubConnect.tsx`) |
| 3 · Org and repository chooser | **A**: owners on the left (the installations, plus orgs with none), the selected owner's repositories on the right, paged and searched server-side, each Not chosen, Read or Write with push ability shown before Write; "Not listed? type owner/repo" carries the recovery copy |
| 4 · Verification results | **A**: a grid, failures first, the fix inline; ok / missing / unknown as the permission matrix draws them. Reads only by default (D6); a write grant offers the opt-in **Push test**, which asks first, creates and deletes `swarmcloud/onboarding-check-<nonce>`, draws ok / missing / unknown, and names a branch its delete left behind. A read grant is never offered one (D9) |
| 5 · Access | **A**, Work › Access, for each person, **plus B**, the admin's Members view. The page states D9 (SwarmCloud enforces a read grant) and D8 (no workflows write). An owner enabled by a fallback token says "via token"; Request install on an org opens the App's install page and records `POST /v1/access/orgs/{owner}/install-request`, and the row draws ORG_APPROVAL_PENDING's §2.3 copy, with Re-check and Withdraw request, until GitHub lists the installation |
| D5 · Fallback token in the console | **Admin settings › GitHub fallback token** (the owner's note on D5, "lets also allow it to be done via the UI admin settings page"), beside `uv run sc setup token --owner <org>`. An owner field and a password input; it posts once to `POST /v1/onboarding/github/token` and stores the **signed-in person's own** token for that owner (the route takes the person from the identity, never the body). The input is cleared whatever the answer, the value is never React state, storage or text, and a refusal draws the server's §2.3 copy. Built in `apps/swarm-ui/src/AdminSettings.tsx` `FallbackTokenCard` |

## Schedules and the Approvals inbox (placement picked 2026-10-08)

Mock-up page: `schedules.html`, drawn by design lane SCHED0 (#892); the design is
[docs/schedules.md](../../schedules.md). The owner answered the design's eleven
questions on 2026-10-08. Only the placement is a screen pick; the other screens
on the page are still recommendations and **have not been picked**.

| Screen or decision | Pick | Why |
|---|---|---|
| 2 · Placement | **P3**, a new spine section, **Automate**: Overview · Work · Automate · Capacity · Admin, holding Schedules and the Approvals inbox (SD1) | **Not** the recommended P1 (Work › Schedules). The owner chose a section of its own for the two pages. `App.tsx` `SECTIONS` gains an `automate` section, and the issue forms' "Where" list follows it |

## Diff viewer (picked 2026-10-08)

From `diff-viewer.html` (lane DIFF0) and [`docs/design/diff-viewer.md`](../../design/diff-viewer.md) section 3.

| Decision | Pick |
|---|---|
| Variant | **2 + 5, then 3**: the Changes tab on agents, workflows and issue runs; the workflow and run tab holds the files x steps matrix; findings beside lines later |
| Entry points | **All at once, in one lane** (not agent first, then workflow) |
| Syntax highlighting | **Yes**, about +6 kB gz, lazy with the Changes tab, React text nodes only (no `innerHTML`) |
| Phone | **One file at a time** with a picker and an All files sheet |

## Repository graph overlays (drawn 2026-10-09, not picked yet)

Mock-up page: `graph-overlays.html`, drawn by design lane KG8-MOCK for row KG8 of
[`docs/design/knowledge-graph.md`](../../design/knowledge-graph.md) §6. The design
is [`docs/design/graph-overlays.md`](../../design/graph-overlays.md). It has four
variants of the community layer and the issue and lane overlays, each in
Structure and Network: 1 Paint, 2 Regions, 3 Lens and 4 Matrix. **No variant
has been picked.** KG8 waits for the owner's answers to that note's §5. The
page recommends **2, Regions, with 4's per-community cards on the phone**.
