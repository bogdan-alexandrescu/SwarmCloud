# fix-place: the 25 review findings and what changed

The 19 frames were re-rendered in light and dark. Four extra frames (20-23) were added for finding 24. Every image was checked for text under 12px and for sideways scroll: none found. Two reviewers then read every image in both themes, and the last fixes were re-rendered and checked by eye.

| # | Finding | What changed |
|---|---|---|
| 1-3 | The Overview frames opened on Home's personal band (HTML keeps no scroll position) | At the Overview address, PlaceHome folds the personal band to its count chips, so the deployment's numbers and tabs come first. Each frame is rendered at its screen's own address (/overview, /admin/people, /config/workspace, /admin/workspaces), not /. overview-open opens the Capacity tab with the pool at its ceiling, and its holders and waiters are in view. |
| 4 | Setup card drawn for Bogdan, naming alice@example.com | Home draws no setup card. A workspace's setup is its page's Setup tab (frame 22). A new person's next step is Home's only content (frame 5). The onboarding fixture's sign-in email follows the persona (api.ts hook). |
| 5 | Failure counts contradicted each other (8, 5, 0, 14 d) | Fixture clock set to now. Today's stats give 5 failed and dead-lettered. The lifecycle band, Needs a look, the Failures tab and the left menu's Failures link all read 5. The left menu takes the tab's own count (OverviewScreen gains extraCounts, and PlaceHome publishes pl-failures). |
| 6 | Browser hand-off drawn twice | Shown once, in Waiting on you. Overview's own hand-off card is hidden on the Now tab. |
| 7 | One agent title in four states | Fixture tasks have unique titles. An agent in Waiting on you is not listed again under Your work. Frame 3 shows the answered agent as ready (answered just now), at the top of Your work. |
| 8 | Runtimes 14d 0h | Tasks carry started and completed times relative to the fixture clock (now). Running now and Your work agree. |
| 9 | Two pools at the ceiling, one shown as a raw code | Only the global pool is full. Pools are named in words, and the code is kept only in a tooltip. |
| 10 | Your work rows cut off at 1280 and on desktop | Your work rows wrap. The failure reason and its age stay readable. |
| 11 | The member's profile said Workspace admin, there was a bell badge, and alice@ appeared | The role line says Member. The notification fixture is empty for the member, so there is no badge. The setup email is fixed (see 4). |
| 12 | The new person's panel had jumps to tabs that are not drawn | A person with no workspace gets only the For you jump (jumps.ts). |
| 13 | Scoped page showed "0 wait for you" and dropped the workspace admin's join requests | The scoped page has no workspace-request count. It has a Waiting for you card for Ana's own join requests, read from the notification feed, with an empty state that says so. Requests to join are no longer called a platform admin's. |
| 14 | The left menu said Platform people on the scoped page, and Last active was a bare dash | The left menu, the phone title and the browser tab all say "People in your workspaces". Every dash carries its reason (not reported or never signed in). |
| 15 | 10px default-role line, 10.83px times | Type is 12px or more everywhere (CSS). |
| 16 | 62 against 60, badges, request order | "62 = 60 signed in + 2 with a role, not signed in yet" is shown. The badges match the 3 requests. Requests have real times and are listed oldest first, in the same order on Home. |
| 17 | Member frame: tiny text, People tab against the caption, two Settings places | The text is 12px. The caption now reads "who holds a role" under the People tab. Config has one row, This workspace, and the panel says that people, roles, settings and setup are its tabs. |
| 18 | Workspaces: progress label at 10px, next step said twice | The label is 12px and the next step is said once per row. |
| 19 | Engineering shown as set up in one place and 4 of 7 in another | The engineering setup fixture has 4 of 7 done, with Enable orgs failed, in both places. |
| 20 | Settings: role and default drawn twice, a "People and roles" link, paragraphs | Each is drawn once per page. The link says Roles tab. The lead paragraphs of Code intelligence, Visual QA and Per-agent budget sit behind "What this does" (LeadFold). Code intelligence's "Never sent" list is folded under its count. |
| 21 | Phone pages wider than 390 | The header's icon button fits. scrollWidth is 390 on every phone frame. |
| 22 | Phone title "People and roles" against the "Platform people" heading | The phone title names the page's heading (App.tsx). |
| 23 | Phone: the row's age on its own line | The age sits at the top right of each row. |
| 24 | No frame of the workspace page as its admin uses it | Added frames 20 (People tab: set a role, return to the default, join link with 2 let in, members' GitHub, why the group's people are not listed), 21 (Roles tab: what each role may do, Set default), 22 (Setup tab) and 23 (Add a new workspace form). |
| 25 | notes.md: 32 items not drawn, real cuts unnamed, frame numbers off by one | AP-24, CP-35, CP-52, AP-41/42, AP-38, NAV-03/04 and NAV-10/11 are now drawn and mapped to their frames. The 21 items still not drawn are each named in "What it costs the owner". Frame numbers match frames.json. OV-03, OV-06 and AP-09 are rewritten. A "Where the cost shows" section is added. |
