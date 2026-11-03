# Desk: "A personal Home and a people page across workspaces"

## The idea

Two pages and one people page. **Home** is a new page at the console's root and the first entry of the left menu: it belongs to the signed-in person and opens on **Needs you** (their owner questions, answered in place; approvals waiting on them; the workspace and Claude account requests they may decide; agents waiting for a person at their browser; their own failed runs), then **Your work** (what they started, with live browser thumbnails, and what of theirs waits and why), **Recently finished** and **Start**; a person with no workspace sees the setup checklist first. **Overview** stays the deployment's page, numbers first: a row of figure tiles (holding capacity, waiting, finished today, pools at ceiling) replaces the lifecycle band and opens a pool in place, then today's regions made compact; the setup card and "Waiting on you" move to Home, where they are personal. **People and roles** becomes one page across workspaces: requests on top, a grid of people by workspace with a scope switch (This workspace · Workspaces I administer · Whole deployment, each offered only to a role that may read it) and one drawer per person for every change. **Workspace Settings** becomes one page per workspace, its sections listed on the left under a workspace switcher that carries each workspace's setup progress, furthest behind first, and replaces Admin › Workspaces you admin.

Where Home sits and why it passes App.tsx's navigation comment: Home is the rail's first entry, above Overview. The comment's test is that a section answers its own QUESTION, not that it re-labels a subsystem; Automate was admitted on that test. Home answers "what waits on me, and what of mine is moving", which Overview answered only by mixing a personal setup card and a personal approvals card into the deployment's page. Overview keeps its own route and its own reads; no screen is merged.

## + What it gets right

+ What waits on the signed-in person is one card with a count per kind and an action per row: Send answer, Approve (one click), Deny… (asks a reason), Lend… (asks), Take control, Open (frames 1, 2, 3, 17).
+ Answering in place shows its effect: the question leaves, the count drops 8 → 7 and the agent leads Your work, "moving again" (frame 3).
+ Overview reads in figures before sentences: four tiles, each with its breakdown; the full pool opens in place with what holds it and what waits on it (frames 6, 7).
+ One answer to "who is in this workspace and what may they do" and to "who is waiting for me to approve them", for every role, at one address; the scope switch is the only difference a role sees (frames 9, 12, 13).
+ A platform admin of six workspaces picks one from a switcher sorted furthest behind first; there is no second page to keep in step (frame 15).
+ The rules hold: a value not read is a dash with its reason; no group roster or member count ("everyone else holds the default"); a role given before someone joins says "not in group yet"; Lend and Reclaim never together; the workspace admin's scoped read draws no requests, no Manage, no Admins list (frame 12); Add a new workspace appears only for the platform admin and posts to the existing new-workspace route (frame 15, absent in 14).

## ! What it costs the owner

! One more rail entry (Home), on every screen: the navigation comment counts the rail's interactive elements, and this adds one (every frame's rail).
! Overview loses its setup card and its "Waiting on you" card; a person who lands on Overview by an old link sees neither there (frame 6 against today's Overview).
! The lifecycle band's state words become tile breakdowns; the per-state cells are one click further for whoever read them there (frame 6).
! Config › People and roles and Admin › People and roles become one page; Admin › People and roles' address redirects to it with the whole-deployment scope, and the four existing address constants in paths.ts change (frames 9, 12, 13).
! Admin › Workspaces you admin is retired as a page; its job is the Settings switcher (frame 15).
! With the grid widened to one column, Workspaces, Admins, Recent admin actions and What each role permits sit under the grid, below the fold at 1440x900 (frame 9); after a change, Recent admin actions is lifted above the grid so the event is seen (frame 11).
! On a phone the grid scrolls sideways inside its card (frame 19). Every phone frame measures 392 px of page width at 390; today's console measures the same (the rail's jump strip), so it is not this direction's, and it is not fixed here.
! Shared pieces changed for the mock-up only (each line marked MOCK-HOOK): Spine.tsx draws Home as the rail's first entry, lights the section each desk screen sits in, names the screen in the phone header and, for the new person (`homewho=new`), shows their name and "No workspace yet" in the foot. The rail's workspace tile still shows the member persona's personal workspace in frame 5: the fixtures have no persona without a workspace, so a build would need one.

## Routes the server would need

- **Your work, "started by me"**: Home filters the newest tasks read to the caller's own; a build needs `GET /v1/tasks?submitted_by=me` (or the existing list filtered server-side by the caller), else the list is "of the N newest read", as the frames say.
- **Live browser thumbnails**: the last frame of an agent's live browser, `GET /v1/tasks/{id}/browser/still` (a still image, the caller's own agents only). Today the console reads the live session only on the agent's page.
- **Pool opened, what holds it**: `/v1/admin/leases` served per pool (`?pool=<name>`) with each lease's pools named; the fixtures' leases name no pool, so frame 7 counts them up to the pool's own held figure.
- **Setup progress per workspace in the switcher**: Workspaces you admin reads each workspace's setup today; the switcher keeps that read (`/v1/tenants/mine` plus each workspace's onboarding summary). No new route if that read is kept.
- Everything else reads routes served today: `/v1/admin/people` (requests, grid, audit), `/v1/admin/workspaces/roles`, `/v1/workspace/roles`, `/v1/approvals`, the owner-question answer route, `/v1/onboarding`, `/v1/capacity`, `/v1/stats`, `/v1/tasks`.

## Where to see it

Every screen is at `?homeset=desk&homescreen=<home|overview|people|person|settings|workspaces>&homestate=<rest|answer|answered|open|changed>` on the fixture build; `homewho=new` is the person with no workspace; the persona is the `swarm_qa_persona` cookie.

## Map of every features.md item

| ID | feature | where it lives in the desk direction |
|---|---|---|
| OV-01 | Page title "Overview" with the section-help ? link | Overview › head (unchanged) |
| OV-02 | Refresh control: age of newest read, Paused | Overview › head (unchanged) |
| OV-03 | "On this page" jump-count strip above the lead | Overview › head (unchanged) |
| OV-04 | Workspace-scope note "workspace <tenant>" / "workspace —" | Overview › head (unchanged) |
| OV-05 | Read tally ("N/8 reads") with tone dot | Overview › head (unchanged) |
| OV-06 | Setup checklist card ("Set up SwarmCloud"), entry point | Home › Set up your workspace |
| OV-07 | Setup card header actions: step-count chip, Hide button | Home › Set up your workspace |
| OV-08 | Setup card step-progress bar (one segment per step) | Home › Set up your workspace |
| OV-09 | Setup checklist steps list Sign in, Request your | Home › Set up your workspace |
| OV-10 | Setup card footer note explaining Hide vs. Workspace | Home › Set up your workspace |
| OV-11 | "Waiting on you" card entry point Automate approvals | Home › Needs you |
| OV-12 | "Waiting on you" header with live count badge | Home › Needs you |
| OV-13 | "Waiting on you" list rows: kind label, linked | Home › Needs you |
| OV-14 | "Waiting on you" footer note oldest N | Home › Needs you |
| OV-15 | "Needs a look" section heading with aggregate count/accessible | Overview › Needs a look |
| OV-16 | "Needs a look" blind-checks badge ("N blind") | Overview › Needs a look |
| OV-17 | "Needs a look" partial mark (pill) when some | Overview › Needs a look |
| OV-18 | "Needs a look" reading state: skeleton check cards | Overview › Needs a look |
| OV-19 | "Needs a look" problem cards, one per derived | Overview › Needs a look |
| OV-20 | "Needs a look" check card: mark, title, id-line | Overview › Needs a look |
| OV-21 | "Needs a look" all-clear absence state "Nothing wrong" | Overview › Needs a look |
| OV-22 | Hand-off cards: one card per agent waiting | Overview › Needs a look (acted on from Home › Needs you) |
| OV-23 | Hand-off card "who is waiting" line step/agent kind | Overview › Needs a look (acted on from Home › Needs you) |
| OV-24 | Hand-off card "Take control" button (held/hold-phase only) | Overview › Needs a look (acted on from Home › Needs you) |
| OV-25 | Hand-off card "I'm ready" action (parked phase) | Overview › Needs a look (acted on from Home › Needs you) |
| OV-26 | Hand-off card "Open →" link to the agent | Overview › Needs a look (acted on from Home › Needs you) |
| OV-27 | Hand-off "+N more waiting" link to Agents › | Overview › Needs a look (acted on from Home › Needs you) |
| OV-28 | Lifecycle band region Waiting / Holding capacity | Overview › figure tiles |
| OV-29 | Lifecycle band "Waiting" cell, broken down by QUEUED/READY/PARKED | Overview › figure tiles |
| OV-30 | Lifecycle band "Holding capacity" cell, broken down | Overview › figure tiles |
| OV-31 | Lifecycle band "Finished today" cell, broken down | Overview › figure tiles |
| OV-32 | Lifecycle band per-cell stale/age footer counted N min | Overview › figure tiles |
| OV-33 | Lifecycle band per-cell page-coverage footer of the N | Overview › figure tiles |
| OV-34 | "Running now" card header, note ("N listed"), All | Overview › Running now |
| OV-35 | "Running now" card loading skeleton | Overview › Running now |
| OV-36 | "Running now" card error/blind absence ("Agent list unread") | Overview › Running now |
| OV-37 | "Running now" card real-zero absence ("No agent exists") | Overview › Running now |
| OV-38 | "Running now" card "Nothing running" absence, cross-checked against | Overview › Running now |
| OV-39 | "Running now" table: State / Agent·profile / Runtime | Overview › Running now |
| OV-40 | "Running now" row: state mark, agent/workflow title link | Overview › Running now |
| OV-41 | "Running now" row Runtime cell elapsed / "leased" | Overview › Running now |
| OV-42 | "Running now" row Cost so far cell figure | Overview › Running now |
| OV-43 | "Running now" phone list (mark, name, elapsed) | Overview › Running now |
| OV-44 | "Running now" card footer row count vs. stats | Overview › Running now |
| OV-45 | "Waiting, and why" card header, note total waiting | Overview › Waiting and why |
| OV-46 | "Waiting, and why" grouped list rows state mark | Overview › Waiting and why |
| OV-47 | "Waiting, and why" absence states unread list | Overview › Waiting and why |
| OV-48 | "Recent token cost" card header, note last N | Overview › Recent token cost |
| OV-49 | "Recent token cost" loading skeleton | Overview › Recent token cost |
| OV-50 | "Recent token cost" error absence task-read failed | Overview › Recent token cost |
| OV-51 | "Recent token cost" zero absences "No agent exists" | Overview › Recent token cost |
| OV-52 | "Recent token cost" big figure dollar sum | Overview › Recent token cost |
| OV-53 | "Recent token cost" four-token-count list (input/output/cache read/cache write) | Overview › Recent token cost |
| OV-54 | "Recent token cost" provenance footer sample coverage, unmeasured | Overview › Recent token cost |
| OV-55 | "Headroom" card header, "Pools →" link, ? help | Overview › Headroom |
| OV-56 | Headroom "Can start now, by runner profile" sub-heading | Overview › Headroom |
| OV-57 | Headroom profile tile: disabled state ("disabled", reason) | Overview › Headroom |
| OV-58 | Headroom profile tile: figure +N / 0 | Overview › Headroom |
| OV-59 | Headroom card capacity-read absence/empty states "Pool state unread" | Overview › Headroom |
| OV-60 | Headroom card footer pool count read, whose workspace | Overview › Headroom |
| OV-61 | Headroom "Subscription accounts" sub-heading and one-line account summary | Overview › Headroom |
| OV-62 | Account line: usable/total figure, best-account % used + | Overview › Headroom |
| OV-63 | Account line "N projected (...) not counted" | Overview › Headroom |
| OV-64 | Account line "N needs sign-in" warning chip | Overview › Headroom |
| OV-65 | Account line "Accounts →" link | Overview › Headroom |
| OV-66 | Account line absence states "Account pool unread" | Overview › Headroom |
| OV-67 | "Recent failures" card header, note (failures summary string) | Overview › Recent failures |
| OV-68 | "Recent failures" table mark, agent/workflow name+id, why, ended-ago | Overview › Recent failures |
| OV-69 | "Recent failures" "N cancelled" folded disclosure | Overview › Recent failures |
| OV-70 | "Recent failures" absence states unread / partial-vs-zero | Overview › Recent failures |
| OV-71 | "Pools" card header, note pool count · busiest | Overview › Pools |
| OV-72 | "Pools" full-width row of per-pool usage bars name | Overview › Pools |
| OV-73 | "Pools" card absence states "Pool state unread" | Overview › Pools |
| OV-74 | Session-expired full-page empty state "Session expired", Reload | Overview › head (unchanged) (session-expired covers whole page) |
| CP-01 | Tab strip switching between "Roles" and "Members' GitHub" | People › This workspace scope (today's Config page, embedded) |
| CP-02 | "Roles" tab option | People › This workspace scope (today's Config page, embedded) |
| CP-03 | "Members' GitHub" tab option | People › This workspace scope (today's Config page, embedded) |
| CP-04 | Page lead: — this workspace's roles could not | People › This workspace scope (today's Config page, embedded) |
| CP-05 | Page lead absent entirely for a personal workspace | People › This workspace scope (today's Config page, embedded) |
| CP-06 | Page lead "Read only for you": names | People › This workspace scope (today's Config page, embedded) |
| CP-07 | Page lead Step 4 · People invited | People › This workspace scope (today's Config page, embedded) |
| CP-08 | Page lead "Needs an admin": N person/people are | People › This workspace scope (today's Config page, embedded) |
| CP-09 | Page lead Nobody else holds a role | People › This workspace scope (today's Config page, embedded) |
| CP-10 | Page lead "Step 4 · done": N other | People › This workspace scope (today's Config page, embedded) |
| CP-11 | Card "Roles in this workspace" | People › This workspace scope (today's Config page, embedded) |
| CP-12 | Chip badge "read only for members" | People › This workspace scope (today's Config page, embedded) |
| CP-13 | Refresh control on the roster card | People › This workspace scope (today's Config page, embedded) |
| CP-14 | Loading/error/empty region for GET /v1/workspace/roles with "Try again" | People › This workspace scope (today's Config page, embedded) |
| CP-15 | Personal-workspace message block You are this workspace's admin | People › This workspace scope (today's Config page, embedded) |
| CP-16 | Button-link "Open Workspace Settings" | People › This workspace scope (today's Config page, embedded) |
| CP-17 | Button-link "Add a new workspace" | People › This workspace scope (today's Config page, embedded) |
| CP-18 | "Your role" paragraph You hold {role} in {name} | People › This workspace scope (today's Config page, embedded) |
| CP-19 | Platform-admin reach notice You are not in {name}'s | People › This workspace scope (today's Config page, embedded) |
| CP-20 | "Default role" paragraph who holds the default, migration | People › This workspace scope (today's Config page, embedded) |
| CP-21 | Roster table "Who holds which role in …" | People › This workspace scope (today's Config page, embedded) |
| CP-22 | Roster column: Person (email, "you" marker) | People › This workspace scope (today's Config page, embedded) |
| CP-23 | Roster column: Role | People › This workspace scope (today's Config page, embedded) |
| CP-24 | Roster column: Given by (incl. "(platform admin)" suffix) | People › This workspace scope (today's Config page, embedded) |
| CP-25 | Roster column: When relative time, title = exact | People › This workspace scope (today's Config page, embedded) |
| CP-26 | Roster column: Reason | People › This workspace scope (today's Config page, embedded) |
| CP-27 | Roster column: Actions — "Return to default" button | People › This workspace scope (today's Config page, embedded) |
| CP-28 | Synthetic "you" row when caller holds no override | People › This workspace scope (today's Config page, embedded) |
| CP-29 | Write-result note beside the roster | People › This workspace scope (today's Config page, embedded) |
| CP-30 | Hint Only this workspace's admins change roles. You | People › This workspace scope (today's Config page, embedded) |
| CP-31 | "What each role permits" definition list | People › This workspace scope (today's Config page, embedded) |
| CP-32 | "Set the default" form: role select + Set | People › This workspace scope (today's Config page, embedded) |
| CP-33 | Write-result note beside the default form | People › This workspace scope (today's Config page, embedded) |
| CP-34 | Card "Give a person their own role" | People › This workspace scope (today's Config page, embedded) |
| CP-35 | Info paragraph: Google owns who | People › This workspace scope (today's Config page, embedded) |
| CP-36 | Email input field (required, type=email, maxLength 254) | People › This workspace scope (today's Config page, embedded) |
| CP-37 | Role segmented control (viewer/member/admin) | People › This workspace scope (today's Config page, embedded) |
| CP-38 | Reason input field (optional, maxLength 280) | People › This workspace scope (today's Config page, embedded) |
| CP-39 | "Set role" submit button (busy/disabled) | People › This workspace scope (today's Config page, embedded) |
| CP-40 | Write-result note beside the person form | People › This workspace scope (today's Config page, embedded) |
| CP-41 | Card "Role events" | People › This workspace scope (today's Config page, embedded) |
| CP-42 | Role/platform-admin event list, one sentence per event; empty-state | People › This workspace scope (today's Config page, embedded) |
| CP-43 | Card "Join link" | People › This workspace scope (today's Config page, embedded) |
| CP-44 | Join-link status paragraph on/off, who turned it | People › This workspace scope (today's Config page, embedded) |
| CP-45 | Hint Nobody gets in by holding the link | People › This workspace scope (today's Config page, embedded) |
| CP-46 | One-time reveal banner + read-only link field + | People › This workspace scope (today's Config page, embedded) |
| CP-47 | Write-result note after Turn on/off/rotate | People › This workspace scope (today's Config page, embedded) |
| CP-48 | "Make a new link" button (rotate) | People › This workspace scope (today's Config page, embedded) |
| CP-49 | "Turn the link off" button (danger) | People › This workspace scope (today's Config page, embedded) |
| CP-50 | "Turn the link on" button (primary) | People › This workspace scope (today's Config page, embedded) |
| CP-51 | Card "Joined by request" | People › This workspace scope (today's Config page, embedded) |
| CP-52 | Notice: Approving a request does not let anyone | People › This workspace scope (today's Config page, embedded) |
| CP-53 | Write-result note ("membership by request is ended…") | People › This workspace scope (today's Config page, embedded) |
| CP-54 | Empty-state Nobody holds a membership of {name} | People › This workspace scope (today's Config page, embedded) |
| CP-55 | Table columns: Person, Let in by, When, Access | People › This workspace scope (today's Config page, embedded) |
| CP-56 | "Remove" button per row | People › This workspace scope (today's Config page, embedded) |
| CP-57 | Card "Members' GitHub connections" (second tab's content) | People › This workspace scope (today's Config page, embedded) |
| CP-58 | Admin-gate panel (FailedPanel) drawn from a 403/admin_required refusal | People › This workspace scope (today's Config page, embedded) |
| CP-59 | Refresh control on the Members card | People › This workspace scope (today's Config page, embedded) |
| CP-60 | Loading/error region for GET /v1/access/members with retry | People › This workspace scope (today's Config page, embedded) |
| CP-61 | Empty-state "No member has started" / Nobody has | People › This workspace scope (today's Config page, embedded) |
| CP-62 | Lead paragraph describing the table | People › This workspace scope (today's Config page, embedded) |
| CP-63 | Column headers: Member, Connection, Owners, Repositories hidden | People › This workspace scope (today's Config page, embedded) |
| CP-64 | Per-member row: name, GitHub handle, connection mark, owners | People › This workspace scope (today's Config page, embedded) |
| CP-65 | Hint Each member connects as themselves. An admin | People › This workspace scope (today's Config page, embedded) |
| WS-01 | Page header "Workspace Settings" with a refresh control | Settings › page head |
| WS-02 | Config tabs: Settings / Workspace setup / Add | Settings › left column section list |
| WS-03 | Lead banner "Setup's next step" + Open Workspace | Settings › Settings section (today's part) |
| WS-04 | Lead banner once complete: "Nothing here needs you" | Settings › Settings section (today's part) |
| WS-05 | "This workspace" card: Name / Id / Kind | Settings › Settings section (today's part) |
| WS-06 | "This workspace" card: Default role row + People | Settings › Settings section (today's part) |
| WS-07 | "Rename" button | Settings › Settings section (today's part) |
| WS-08 | Rename form: "New name" input, hint, Save/Cancel, refusal | Settings › Settings section (today's part) |
| WS-09 | "Code intelligence" card lead paragraph + refresh | Settings › Settings section (today's part) |
| WS-10 | "Send code to Jev" switch + "Never sent" | Settings › Settings section (today's part) |
| WS-11 | "Visual QA" card lead paragraph + refresh | Settings › Settings section (today's part) |
| WS-12 | QA flows-per-step read-only figure | Settings › Settings section (today's part) |
| WS-13 | QA flows-per-step form number input, bounds hint, validation | Settings › Settings section (today's part) |
| WS-14 | "Per-agent budget" card lead paragraph + refresh | Settings › Settings section (today's part) |
| WS-15 | Budget read-only view Budget, Platform ceiling, Set | Settings › Settings section (today's part) |
| WS-16 | Budget form: Off/Dollars/Tokens radiogroup, amount input, hint, invalid/over-max/over-ceiling | Settings › Settings section (today's part) |
| WSU-01 | Page header "Workspace Settings" with refresh control profile | Settings › page head |
| WSU-02 | Config tabs (same as WS-02) | Settings › left column section list |
| WSU-03 | Lead banner "Step N of M · next" | Settings › Setup section (today's checklist) |
| WSU-04 | Lead banner once complete: "Setup is complete" | Settings › Setup section (today's checklist) |
| WSU-05 | Intro paragraph connect GitHub / install App | Settings › Setup section (today's checklist) |
| WSU-06 | Checklist card loading/error/empty states The checklist answered | Settings › Setup section (today's checklist) |
| WSU-07 | Checklist card header: title, "Show on Overview" button | Settings › Setup section (today's checklist) |
| WSU-08 | Count-summary sentence + step bar | Settings › Setup section (today's checklist) |
| WSU-09 | Checklist rows: title, state word, "next" chip, evidence | Settings › Setup section (today's checklist) |
| WSU-10 | Step issue/error copy message(s), with "Open on GitHub" | Settings › Setup section (today's checklist) |
| WSU-11 | "Last probe did not complete…" note on Connect | Settings › Setup section (today's checklist) |
| WSU-12 | "Connecting authorised the App…" help note on Install | Settings › Setup section (today's checklist) |
| WSU-13 | Per-step action buttons: Connect GitHub/Reconnect, Re-check, Install | Settings › Setup section (today's checklist) |
| WSU-14 | Workspace step, "none" state: helper + Request my | Settings › Setup section (today's checklist) |
| WSU-15 | Workspace step, "requested" state: request line, elapsed time | Settings › Setup section (today's checklist) |
| WSU-16 | Workspace step, approved/applying/needs_owner state: ApprovedWaiting/"Setting up…" + elapsed | Settings › Setup section (today's checklist) |
| WSU-17 | Workspace step, "ready" state: "Workspace ready (id)" | Settings › Setup section (today's checklist) |
| WSU-18 | Workspace step, "denied" state: reason quote + Request | Settings › Setup section (today's checklist) |
| WSU-19 | Workspace step, "failed" state: Setting up your workspace | Settings › Setup section (today's checklist) |
| WSU-20 | Workspace step: refusal message on request failure + | Settings › Setup section (today's checklist) |
| WSU-21 | Claude-account step: counts line, helper text, "Add key" | Settings › Setup section (today's checklist) |
| WSU-22 | "After the checklist" card: Secrets / People invited | Settings › Setup section (today's checklist) |
| WSU-23 | "On Overview" card status paragraph (complete/hidden/shown/default) | Settings › Setup section (today's checklist) |
| WSU-24 | "On Overview" card "Show on Overview" button + | Settings › Setup section (today's checklist) |
| WSU-25 | GitHub section header: "Checked x ago" + ↻ | Settings › Setup section (today's checklist) |
| WSU-26 | GitHub section loading/error states | Settings › Setup section (today's checklist) |
| WSU-27 | GitHub section lead paragraph GitHub does not let | Settings › Setup section (today's checklist) |
| WSU-28 | App permissions card: Details/Hide toggle, count chip, summary | Settings › Setup section (today's checklist) |
| WSU-29 | App permissions card "Add to App" fix link | Settings › Setup section (today's checklist) |
| WSU-30 | App permissions card hint A platform admin adds | Settings › Setup section (today's checklist) |
| WSU-31 | App permissions card "not read"/The App was not | Settings › Setup section (today's checklist) |
| WSU-32 | Org permissions cards (one per installation): toggle, count | Settings › Setup section (today's checklist) |
| WSU-33 | Org permissions card "Review on GitHub" fix link | Settings › Setup section (today's checklist) |
| WSU-34 | "No connected org" empty state | Settings › Setup section (today's checklist) |
| NW-01 | Page header + Config tabs page itself platform-admin | Settings › Add a new workspace section (platform admin) |
| NW-02 | "Reading whether you are a platform admin…" loading | Settings › Add a new workspace section (platform admin) |
| NW-03 | "Not available to you" / Adding a workspace | Settings › Add a new workspace section (platform admin) |
| NW-04 | "Add a new workspace" lead banner explaining | Settings › Add a new workspace section (platform admin) |
| NW-05 | Workspace-kind tabs: Personal / Group / Service | Settings › Add a new workspace section (platform admin) |
| NW-06 | Address field (email input; label/placeholder vary by kind) | Settings › Add a new workspace section (platform admin) |
| NW-07 | Display name (optional) field | Settings › Add a new workspace section (platform admin) |
| NW-08 | First admin's email address (optional) field + hint | Settings › Add a new workspace section (platform admin) |
| NW-09 | "What this gets" paragraph, text differs per kind | Settings › Add a new workspace section (platform admin) |
| NW-10 | Refusal error message (code + sentence) | Settings › Add a new workspace section (platform admin) |
| NW-11 | "Create workspace"/"Creating…" submit button | Settings › Add a new workspace section (platform admin) |
| NW-12 | First-admin outcome message granted & member / granted | Settings › Add a new workspace section (platform admin) |
| NW-13 | Progress: record id + name/principal | Settings › Add a new workspace section (platform admin) |
| NW-14 | Progress, "requested" state line + held/migration | Settings › Add a new workspace section (platform admin) |
| NW-15 | Progress, approved/applying state | Settings › Add a new workspace section (platform admin) |
| NW-16 | Progress, "needs_owner" state | Settings › Add a new workspace section (platform admin) |
| NW-17 | Progress, "ready" state ("Workspace ready") | Settings › Add a new workspace section (platform admin) |
| NW-18 | Progress, "denied" state (reason quote) | Settings › Add a new workspace section (platform admin) |
| NW-19 | Progress, "failed" state "Setting up stopped" + failure | Settings › Add a new workspace section (platform admin) |
| NW-20 | Progress, unknown-state Dash fallback | Settings › Add a new workspace section (platform admin) |
| NW-21 | WorkspaceTimeline: ordered job-step list with tone, label, state | Settings › Add a new workspace section (platform admin) |
| NW-22 | "Add another" button | Settings › Add a new workspace section (platform admin) |
| NW-23 | "What creating it does" card: ordered list | Settings › Add a new workspace section (platform admin) |
| NW-24 | "What creating it does" card explanatory paragraph POST | Settings › Add a new workspace section (platform admin) |
| NW-25 | Filed <id>. Admin › Tenants lists it under | Settings › Add a new workspace section (platform admin) |
| AP-01 | Page title "People and roles" | People › Whole deployment scope |
| AP-02 | Subtitle (platform-wide wording vs scoped wording) | People › Whole deployment scope |
| AP-03 | Refresh control beside the title | People › Whole deployment scope |
| AP-04 | Admin-gate region 403 on GET /v1/admin/people draws | People › Whole deployment scope |
| AP-05 | Full-platform lead: "Nobody is waiting for an admin" | People › Whole deployment scope |
| AP-06 | Full-platform lead: N requests are waiting | People › Whole deployment scope |
| AP-07 | Full-platform lead: unread state who is waiting | People › Whole deployment scope |
| AP-08 | Workspace-admin lead ("Your workspaces"): Nobody is waiting | People › Workspaces I administer scope |
| AP-09 | Workspace-admin lead: N people are waiting to join | People › Workspaces I administer scope |
| AP-10 | Workspace-admin lead: unread state | People › Workspaces I administer scope |
| AP-11 | "Workspace building is off" banner | People › Whole deployment scope |
| AP-12 | "The requests to join could not be read" | People › Whole deployment scope (shared with Workspaces I administer scope) |
| AP-13 | "Waiting for you" card: amber, one row | People › Whole deployment scope |
| AP-14 | Waiting-card empty line "Nothing else is waiting." | People › Whole deployment scope |
| AP-15 | Workspace-request row: avatar, name, "asked <time>", workspace id | People › Whole deployment scope |
| AP-16 | Approve control (one click) + busy state + | People › Whole deployment scope |
| AP-17 | Deny control: inline reason textarea (required, max 500) | People › Whole deployment scope |
| AP-18 | Loan-request row: avatar, name, Borrow a Claude account | People › Whole deployment scope |
| AP-19 | Lend control: account picker/confirm dialog | People › Whole deployment scope |
| AP-20 | Decline control: optional reason input, Cancel/Decline | People › Whole deployment scope |
| AP-21 | Join-request row: avatar, requester name, to join <workspace> | People › Whole deployment scope (shared with Workspaces I administer scope) |
| AP-22 | Join Approve control: role select + Approve button | People › Whole deployment scope (shared with Workspaces I administer scope) |
| AP-23 | Join Deny control: required reason textarea, Cancel/Deny | People › Whole deployment scope (shared with Workspaces I administer scope) |
| AP-24 | Workspace-admin "Waiting for you" card (join requests only) | People › Workspaces I administer scope |
| AP-25 | "The workspaces' roles could not be read" banner | People › Whole deployment scope |
| AP-26 | "People and their roles" card head: Workspace filter | People › Whole deployment scope |
| AP-27 | Workspace filter (Segmented): "Every workspace" + one | People › Whole deployment scope |
| AP-28 | Count chip "N of M people in X" | People › Whole deployment scope |
| AP-29 | "Grant admin" button opening GrantDialog | People › Whole deployment scope |
| AP-30 | "Find a person" search box + match-count | People › Whole deployment scope (shared with Workspaces I administer scope) |
| AP-31 | "No 'joined by request' mark…" notice | People › Whole deployment scope (shared with Workspaces I administer scope) |
| AP-32 | Empty state no one has signed | People › Whole deployment scope |
| AP-33 | Nobody has been seen in X yet | People › Workspaces I administer scope |
| AP-34 | NobodyShown filtered-empty message | People › Whole deployment scope (shared with Workspaces I administer scope) |
| AP-35 | Full table columns: Person, Platform role, per-workspace role | People › Whole deployment scope |
| AP-36 | Scoped table columns: Person, one per administered workspace | People › Workspaces I administer scope |
| AP-37 | Person cell: avatar, name/email, GitHub mark, legacy platform | People › Whole deployment scope (shared with Workspaces I administer scope) |
| AP-38 | Platform role cell | People › Whole deployment scope |
| AP-39 | Role cell (grid): role word, admin badge, none/requested/dash | People › Whole deployment scope (shared with Workspaces I administer scope) |
| AP-40 | Own-workspace cell: state tone+word, workspace id, failure | People › Whole deployment scope (shared with Workspaces I administer scope) |
| AP-41 | Claude-account cell: source summary, multi-loan disclosure, borrow/decline chips | People › Whole deployment scope |
| AP-42 | Ceiling cell: numeric value or dash | People › Whole deployment scope |
| AP-43 | Last-active cell | People › Whole deployment scope (shared with Workspaces I administer scope) |
| AP-44 | "Manage" button opening the person's drawer | People › Whole deployment scope |
| AP-45 | Manage drawer: Facts section | People › Whole deployment scope |
| AP-46 | Manage drawer: Ceiling section stepper, numeric field, Save | People › Whole deployment scope |
| AP-47 | Manage drawer: Claude account section | People › Whole deployment scope |
| AP-48 | Manage drawer: Admin switch (on/off, confirm dialog) | People › Whole deployment scope |
| AP-49 | Manage drawer: "Roles in workspaces" section | People › Whole deployment scope |
| AP-50 | Drawer chrome: focus trap, Escape/scrim/Close dismissal | People › Whole deployment scope |
| AP-51 | "Workspace roles" card: per-workspace default role, named admins | People › Whole deployment scope |
| AP-52 | Workspace-roles card list states (loading/error/empty) | People › Whole deployment scope |
| AP-53 | "Admins" side card: admin list, inline "Grant admin" | People › Whole deployment scope |
| AP-54 | "Recent admin actions" side card folded, N | People › Whole deployment scope |
| AP-55 | Audit body: empty state, housekeeping fold/unfold, per-run list | People › Whole deployment scope |
| AP-56 | Outcome/error line after any write (SaidLine) | People › Whole deployment scope (shared with Workspaces I administer scope) |
| WYA-01 | Page title "Workspaces you admin" | Settings › workspace switcher |
| WYA-02 | Refresh control beside the title | Settings › workspace switcher |
| WYA-03 | Subtitle describing the page | Settings › workspace switcher |
| WYA-04 | Error/retry region for GET /v1/tenants/mine | Settings › workspace switcher |
| WYA-05 | Empty state "You administer no group workspace" | Settings › workspace switcher |
| WYA-06 | "Behind" lead card naming the workspace furthest behind | Settings › workspace switcher |
| WYA-07 | List of administered workspaces display name + tenant | Settings › workspace switcher |
| WYA-08 | "admin as a platform admin" marker | Settings › workspace switcher |
| WYA-09 | Per-workspace links: Workspace Settings, MCP servers, People | Settings › workspace switcher |
| WYA-10 | Per-workspace setup progress strip (segments/count/next-step), "Reading setup…"/Setup not | Settings › workspace switcher |
| WYA-11 | Setup-progress "next" click opens that step's Config page | Settings › workspace switcher |
| WYA-12 | Design note: personal workspaces never listed; platform admin's | Settings › workspace switcher |
| NAV-01 | Overview's "On this page" panel lists 8 jump | Spine › Overview panel (jump links, reordered for new regions) |
| NAV-02 | 4 of the 8 Overview jump links Needs | Spine › Overview panel (jump links' live count marks) |
| NAV-03 | Config section's panel lists 6 rows in fixed | Spine › Config panel |
| NAV-04 | Each Config row (except Workspace Settings) draws | Spine › Config panel |
| NAV-05 | Admin section's panel lists up to 9 rows | Spine › Admin panel (Workspaces you admin row removed; People and roles redirects scope=whole) |
| NAV-06 | Admin panel drawn in two groups People | Spine › Admin panel |
| NAV-07 | Admin rows carry status badges from reads already | Spine › Admin panel |
| NAV-08 | "In Automate" group inside the Admin panel links | Spine › Admin panel (In Automate group) |
| NAV-09 | Capacity › Accounts' "Provider quota" child row carries | Spine › Capacity panel (unchanged) |
| NAV-10 | SetupStrip renders across the top of every Config | Spine › Config pages (strip; unchanged, stays) |
| NAV-11 | Compact SetupMeter segmented bar, Setup N of M | Spine › Config panel (SetupMeter; unchanged) |
| HOME-01 | The root address / resolves to address overview/now | Spine › root route (now targets Home, not Overview) |


## Non-ID section: "Home / App.tsx navigation test"

This section's one feature row (HOME-01) is in the table above. The rest of the section is prose plus a "Path constants found" list (7 bullets, no IDs): it documents that the root address '/' falls back to Overview today, and names the FIXED path constants (`overview/now`, `config/people`, `config/workspace` + its `setup`/`new` query parts, `admin/people`, `admin/workspaces`) plus the heading/label-parity test that reads `App.tsx`'s `SECTIONS`/`SectionBody` source directly. In the desk direction this moves: root '/' now targets the new Home page instead of falling back to Overview (Spine › root route); `config/people` and `admin/people` both resolve to the single combined People and roles page (`admin/people` redirecting with `scope=whole`); `config/workspace` gains the workspace-switcher left column but keeps its `setup`/`new` query parts as the Setup and Add-a-new-workspace sections; and `admin/workspaces` (Workspaces you admin) is retired as a page, replaced by the Settings workspace switcher. The heading/label-parity test and its parser-guard test keep applying to whatever `SECTIONS`/`SectionBody` becomes, including the new Home entry.

## Non-ID section: "Reads per page"

This section lists the network calls (loadX/GET, setX/PUT, etc.) made by each of today's six pages (Overview, Config People and roles, Workspace Settings, Workspace setup, Add a new workspace, Admin People and roles) and Workspaces you admin; none of its 7 bullets carry a feature ID. The direction redistributes *where* each read is consumed, not the calls themselves: Overview's loadOnboarding/dismissOnboarding and loadApprovals move to Home (setup card and Waiting-on-you card); Overview's loadCapacity/loadTasks/loadLeases/loadProviders/loadAccountPool/loadWorkflows/loadStats/loadSpend/loadTasksInState/loadLiveSession/loadTask stay behind Overview's figure tiles and regions; Config People and roles' and Admin People and roles' reads both now feed the one combined People and roles page (scoped by the Segmented control); Workspace Settings', Workspace setup's and Add a new workspace's reads all feed the one per-workspace Settings page (sections in the left column); and Workspaces you admin's single read (loadMyTenants) feeds the Settings workspace switcher instead of its own page.
