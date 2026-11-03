# One Home, and a page per workspace (direction "place")

## The idea

One landing page and workspaces as places. **Home** is Overview made personal: a band on top for the signed-in person (what waits on them, each acted on in place: answer a question, approve a plan, approve or deny a request, lend an account; their own running work; Start an agent), the setup card under it while their setup is not complete, and the deployment below in five tabs (Now, Waiting, Failures, Spend, Capacity) in place of one long column. `/overview` opens the same page at its deployment part, and the left menu keeps one item, **Home**, whose panel jumps to For you and the five tabs. **Workspaces** are the way into administration: a list (every group workspace for a platform admin, the ones they administer for a workspace admin, their own for a member), furthest behind on setup first, each with its progress and what waits in it; one workspace's page has tabs People, Roles, Settings and Setup. What is deployment-wide (requests, every person who signed in, platform admins, recent admin actions, Claude account loans) is one page, **Platform people**.

Why it passes the navigation comment's test (App.tsx): the menu does not grow. Overview's item becomes Home (same address, same place); Config › People and roles and Config › Workspace Settings fold into the workspace page; Admin › People and roles becomes Platform people and Admin › Workspaces you admin becomes Workspaces. Admin's other pages (Pool limits, Runner sizes, Tenants, Platform counts, Platform secrets, Setup, Health) stay where they are, under Admin, unchanged; the Capacity tab links to Pool limits and Runner sizes, a workspace's Setup tab links to the Config pages for GitHub access, Repositories, Secrets and MCP servers.

## + What it gets right

+ What waits on you is the first thing on the page, counted in chips (7 wait on you: 2 questions, 1 approval, 3 requests, 1 at a browser) and acted on in place (frames 1-3).
+ Every question has one place: "who is waiting for me" is Home's band, "who is in this workspace and what may they do" is that workspace's People and Roles tabs, "what is this workspace set to" its Settings tab, "who is anyone, platform-wide" is Platform people.
+ A platform admin of six workspaces picks one from a list led by the one furthest behind (frame 15); the grid on Platform people shows 60 people against 6 workspaces at once (frame 9).
+ Each role sees only what it may do: the workspace admin's Platform people is the scoped read with no requests, no Manage and no Admins (frame 12); the member's workspace page is read only (frame 13).
+ The deployment reads in numbers and marks: Overview's own regions, unchanged, arranged in tabs so the first screen holds the person's work and the deployment's counts.
+ Honesty rules kept as today: a value nothing reported is a dash with its reason; totals off a page say "of the N newest read"; no group roster or member count; a role given before someone joined says it applies when they join.

## ! What it costs the owner

! Overview's long column becomes tabs: a region is one click away rather than one scroll away; Recent failures sits behind the Failures tab (frames 6, 7).
! Two Config pages (People and roles, Workspace Settings) and the Config setup strip move onto the workspace page: a person who knew Config › People and roles now goes Workspaces › engineering › People (frames 13, 14). The Config panel's rows for GitHub access, Repositories, Secrets and MCP servers stay Config pages, reached from the workspace's Setup tab.
! The Platform people grid is leaner than today's: the platform role is a tag in the person's cell, and the Claude account and ceiling move into Manage (frame 9, 10).
! The Members' GitHub view becomes a fold inside the People tab rather than a second tab.
! Not drawn in this mock (would be built as today): the read states (refresh, loading, error, 403, empty), each write's result note except the role change's, the join link's off and one-time reveal states, and the other items marked "! NOT DRAWN" in the map below.
! The member persona's left menu (the console's own Spine) still shows Config for their personal workspace while the main pane shows engineering, the group workspace they hold a default role in (frame 13): in a build, the workspace switcher would follow the workspace page.
! Frame 3: the answered question's agent is shown back in line ("ready, starts when a pool has room") rather than running, because the pool it needs is at its ceiling in the same fixtures; that is the honest state.
! Frame 5 (the new person): the fixture build answers as one of three personas, none of them a person without a workspace, so the console's own chrome (the workspace switcher and the profile at the bottom of the menu) still shows the member, Kenji; the page itself is Hana Okafor's, drawn from the scaffold's new-person fixture (`&who=new`).
! Frames were laid out in an iframe of exactly each frame's size (the live browser's device switch was unavailable at render time); the media queries see the frame's own width.

## Routes it would need

None new. Home reads what Overview, Automate's approvals, the owner questions and Admin › People already read; the workspace page reads what Config › People and roles and Workspace Settings read; Platform people reads Admin › People and roles' route. The Home band's "yours running" and "yours failed" counts are the existing runs read filtered to the caller (counted off the page of the newest, and said so).

## Shared pieces changed (each line marked MOCK-HOOK)

- `HomeRedraw.tsx`: registers PLACE_SCREENS.
- `fixtures.ts`: exports the roster, overrides and people docs; adds Mei Chen as a signed-in person (the one whose role changes); the 60-person total is unchanged.
- `Spine.tsx`: the one item labelled Home, its panel's jumps (jumps.ts) with Overview's counts on Now, Waiting and Failures; Platform people and Workspaces labels.
- `App.tsx`: the section and its phone title read Home.

## Map: every features.md item to where it lives

Lines marked "! NOT DRAWN" are costs the mock does not draw; read states are kept as today.

## Overview (OV)

- OV-01 Page title + `?` link → Home › deployment header, shown on every tab (not hidden by any `data-tab` rule) — Overview.tsx:OverviewScreen (PageHead), embedded unchanged by PlaceHome.tsx
- OV-02 Refresh control → same, always visible — Overview.tsx:OverviewScreen (RefreshControl), unchanged
- OV-03 "On this page" jump-count strip → redrawn as the 6-item place jump chip row (phone `sk-otp` / desktop panel) via jumps.ts:PLACE_JUMPS + Spine.tsx:OVERVIEW_JUMPS, but its live counts are broken for place (see id-mismatch note above) — placed, counts not functional
- OV-04 Workspace-scope note → Home › deployment header, always visible — Overview.tsx:CountNote (ov-scope), unchanged
- OV-05 Read tally → same — Overview.tsx:CountNote (ov-tally), unchanged
- OV-06 Setup checklist card entry → Home, under the personal band: Onboarding.tsx:SetupCard, unchanged (PlaceHome.tsx `.pl-setup`); drawn only while setup is not complete, so it is absent from the platform admin frames
- OV-07 Setup card header actions → inside SetupCard on Home, as OV-06
- OV-08 Setup card step-progress bar → inside SetupCard on Home, as OV-06
- OV-09 Setup checklist steps list → inside SetupCard on Home, as OV-06
- OV-10 Setup card footer note → inside SetupCard on Home, as OV-06
- OV-11 "Waiting on you" (Automate approvals) card entry → placed, reimplemented: PlaceHome.tsx merges approvals (`loadApprovals`) into its own personal-band "Waiting on you" Card alongside questions/handoffs/requests (PlaceHome.tsx:WaitRow, kind `'approval'`), not Schedules.tsx:WaitingOnYouCard (not imported)
- OV-12 Header live-count badge → placed, relocated: the count shows as a separate pill in the `pl-counts` strip above the card (PlaceHome.tsx `Count` component), not on the card's own header
- OV-13 List rows (kind, linked summary, age) → placed — PlaceHome.tsx:WaitRow, approval case (kind label, `<a href="/automate/approvals">{summary}</a>`, `ago(requested_at)`)
- OV-14 Footer note ("oldest N of M…") → ! NOT DRAWN IN THIS MOCK (a cost, see notes above). PlaceHome.tsx renders the full `shown` array with no `.slice`/truncation at all, so there is nothing to caption
- OV-15 "Needs a look" heading → Home › deployment, Now tab only (`#ov-needs` hidden on every other tab by `.pl-dep:not([data-tab='now']) #ov-needs{display:none}`) — Overview.tsx:NeedsALook, unchanged
- OV-16 Blind-checks badge → Now tab only, same card
- OV-17 Partial mark → Now tab only, same card
- OV-18 Reading-state skeletons → Now tab only, same card
- OV-19 Problem cards → Now tab only — Overview.tsx:CheckCard / checks.ts:deriveChecks
- OV-20 Check card detail → Now tab only, same
- OV-21 All-clear absence state → Now tab only, same
- OV-22 Hand-off cards → Now tab only (same `#ov-needs` container as NeedsALook) — Overview.tsx:HandoffCards / Handoff.tsx:HandoffBanner
- OV-23 Hand-off "who is waiting" line → Now tab only, same
- OV-24 "Take control" button → Now tab only, same
- OV-25 "I'm ready" action → Now tab only, same
- OV-26 "Open →" link → Now tab only, same
- OV-27 "+N more waiting" link → Now tab only, same
- OV-28 Lifecycle band region → Home › deployment, every tab (`.pl-dep .ov-page > .ov-life{order:1}`, no `data-tab` hide rule targets it) — OverviewRegions.tsx:LifecycleBand, unchanged
- OV-29 Lifecycle "Waiting" cell → every tab, same
- OV-30 Lifecycle "Holding capacity" cell → every tab, same
- OV-31 Lifecycle "Finished today" cell → every tab, same
- OV-32 Lifecycle stale/age footer → every tab, same
- OV-33 Lifecycle page-coverage footer → every tab, same
- OV-34 "Running now" header → Home › deployment, Now tab only (full width: `.pl-dep[data-tab='now'] #ov-running{grid-column:1/-1}`; hidden on every other tab) — Overview.tsx:RunningCard, unchanged
- OV-35 Running-now loading skeleton → Now tab only, same
- OV-36 Running-now error/blind absence → Now tab only, same
- OV-37 Running-now real-zero absence → Now tab only, same
- OV-38 "Nothing running" absence → Now tab only, same
- OV-39 Running-now table → Now tab only — Overview.tsx:RunningCard/RunningRow
- OV-40 Running-now row → Now tab only, same
- OV-41 Runtime cell → Now tab only — Overview.tsx:Runtime
- OV-42 Cost-so-far cell → Now tab only — Overview.tsx:CostSoFar
- OV-43 Phone list → Now tab only — Overview.tsx:PhoneRunRow
- OV-44 Running-now footer → Now tab only, same
- OV-45 "Waiting, and why" header → Home › deployment, Waiting tab only (full width) — Overview.tsx:OverviewScreen (CardHead), unchanged
- OV-46 Waiting grouped rows → Waiting tab only — OverviewRegions.tsx:WaitingWhy
- OV-47 Waiting absence states → Waiting tab only, same
- OV-48 "Recent token cost" header → Home › deployment, Spend tab only (full width) — Overview.tsx:OverviewScreen (CardHead, explain="token-cost"), unchanged
- OV-49 Spend loading skeleton → Spend tab only — Overview.tsx:SpendBody
- OV-50 Spend error absence → Spend tab only, same
- OV-51 Spend zero absences → Spend tab only, same
- OV-52 Spend big figure → Spend tab only, same
- OV-53 Token-count list → Spend tab only — Overview.tsx:TokenMix
- OV-54 Spend provenance footer → Spend tab only, same
- OV-55 "Headroom" header → Home › deployment, Capacity tab only (full width, shown with Pools) — Overview.tsx:OverviewScreen (CardHead, explain="pools-all-at-once"), unchanged
- OV-56 Headroom per-profile tiles → Capacity tab only — Overview.tsx:HeadroomBody/CapacityStacks/ProfileTile
- OV-57 Profile tile disabled state → Capacity tab only, same
- OV-58 Profile tile figure/binds line → Capacity tab only, same
- OV-59 Headroom absence states → Capacity tab only — Overview.tsx:CapacityStacks
- OV-60 Headroom footer → Capacity tab only, same
- OV-61 "Subscription accounts" sub-heading → Capacity tab only — Overview.tsx:HeadroomBody/AccountLine
- OV-62 Account line figure → Capacity tab only, same
- OV-63 "N projected" note → Capacity tab only, same
- OV-64 "N needs sign-in" chip → Capacity tab only, same
- OV-65 "Accounts →" link → Capacity tab only, same
- OV-66 Account line absences → Capacity tab only, same
- OV-67 "Recent failures" header → Home › deployment, Failures tab only (full width) — Overview.tsx:OverviewScreen (CardHead), unchanged
- OV-68 Recent-failures table → Failures tab only — OverviewRegions.tsx:RecentFailures
- OV-69 "N cancelled" fold → Failures tab only, same
- OV-70 Recent-failures absences → Failures tab only, same
- OV-71 "Pools" header → Home › deployment, Capacity tab only (full width, alongside Headroom; not in any of Now/Waiting/Failures/Spend's hide lists is false — it IS hidden there, only shown on Capacity) — Overview.tsx:OverviewScreen (CardHead)/poolsNote, unchanged
- OV-72 Per-pool usage bars → Capacity tab only — Overview.tsx:PoolsBody/PoolRow
- OV-73 Pools absence states → Capacity tab only — Overview.tsx:PoolsBody
- OV-74 Session-expired empty state → placed unchanged, replaces the whole embedded page regardless of tab — Overview.tsx:OverviewScreen

## Config › People and roles (CP)

Reimplemented (not `WorkspacePeopleScreen`) inside PlacePeople.tsx's `PlaceWorkspace`, tabs People
(`WsPeople`) and Roles (`WsRoles`), reached as Home's Workspaces list → one workspace's page.

- CP-01 Tab strip (Roles / Members' GitHub, `?view=members`) → ! NOT DRAWN IN THIS MOCK (a cost, see notes above). Roles is now its own top-level tab; Members' GitHub is a `<details>` fold inside the People tab, not a second view of a tab
- CP-02 "Roles" tab option → placed — PlacePeople.tsx:PlaceWorkspace (`WS_TABS`), WsRoles
- CP-03 "Members' GitHub" tab option → ! NOT DRAWN IN THIS MOCK (a cost, see notes above) as a tab; its content exists only as the fold noted at CP-57
- CP-04 Page lead for unreadable roles → kept as today (a read state: refresh, loading, error, 403 or empty); the place pages would wrap the same reads in the same states; not drawn in a frame (fixtures always answer). ! not shown
- CP-05 Page lead absent for personal workspace → placed, reshaped: WsPeople shows a full Card ("{name} is this workspace's fixed admin") instead of leaving the lead blank
- CP-06 Page lead "Read only for you" naming admins → placed, relocated to a side Card — PlacePeople.tsx:WsPeople "Who to ask"
- CP-07 Page lead "Step 4 · People invited — {reason}" → ! NOT DRAWN IN THIS MOCK (a cost, see notes above)
- CP-08 Page lead "Needs an admin" → ! NOT DRAWN IN THIS MOCK (a cost, see notes above) (no "N waiting" callout inside a single workspace's People tab; waiting-for-admin only appears on Platform people)
- CP-09 "Nobody else holds a role… Give a person a role" prompt + scroll/focus → ! NOT DRAWN IN THIS MOCK (a cost, see notes above) as a conditional prompt; the form itself (CP-34) is always shown regardless of holder count
- CP-10 Page lead "Step 4 · done" → ! NOT DRAWN IN THIS MOCK (a cost, see notes above)
- CP-11 Card "Roles in this workspace" → placed, renamed "Who holds a role here" — PlacePeople.tsx:WsPeople
- CP-12 "read only for members" chip → placed, same card (`action={canManage?undefined:<span className="pl-tag">read only</span>}`)
- CP-13 Refresh control on roster card → kept as today (a read state: refresh, loading, error, 403 or empty); the place pages would wrap the same reads in the same states; not drawn in a frame (fixtures always answer). ! not shown
- CP-14 Loading/error/empty + "Try again" → kept as today (a read state: refresh, loading, error, 403 or empty); the place pages would wrap the same reads in the same states; not drawn in a frame (fixtures always answer). ! not shown
- CP-15 Personal-workspace message block → placed, same Card as CP-05
- CP-16 "Open Workspace Settings" button-link → ! NOT DRAWN IN THIS MOCK (a cost, see notes above) on this card
- CP-17 "Add a new workspace" button-link → ! NOT DRAWN IN THIS MOCK (a cost, see notes above) on this card; a platform admin instead reaches it from the Workspaces list page (see NW block)
- CP-18 "Your role" paragraph → placed, reshaped into the synthetic "you" table row — PlacePeople.tsx:WsPeople (`{!mine && <tr className="is-you">…}`)
- CP-19 Platform-admin reach notice → ! NOT DRAWN IN THIS MOCK (a cost, see notes above)
- CP-20 "Default role" paragraph → placed, moved to the Roles tab's own "Default role" card — PlacePeople.tsx:WsRoles
- CP-21 Roster table → placed — PlacePeople.tsx:WsPeople, `<table className="pl-tbl">`
- CP-22 Column: Person → placed
- CP-23 Column: Role → placed
- CP-24 Column: Given by → placed
- CP-25 Column: When → placed, merged into the "Given by" cell (`{short(granted_by)} · {ago(granted_at)}`), not a separate column
- CP-26 Column: Reason → placed ("Why" column)
- CP-27 Column: Actions ("Return to default") → placed — role `<select>` + "Return to default" button, canManage only
- CP-28 Synthetic "you" row → placed (see CP-18)
- CP-29 Write-result note beside roster → kept: a write's result is a status line where the control was (`.pl-said`, shown in frame 11 for the role change). ! not drawn for this control
- CP-30 Hint "Only admins change roles…" → placed, in the "Who to ask" side card (CP-06)
- CP-31 "What each role permits" list → placed, renamed "What each role may do" — PlacePeople.tsx:WsRoles
- CP-32 "Set the default" form → placed, same card
- CP-33 Write-result note beside default form → kept: a write's result is a status line where the control was (`.pl-said`, shown in frame 11 for the role change). ! not drawn for this control
- CP-34 Card "Give a person their own role" → placed — PlacePeople.tsx:WsPeople, canManage
- CP-35 "Google owns who is in the workspace…" paragraph → ! NOT DRAWN IN THIS MOCK (a cost, see notes above)
- CP-36 Email input → placed, same form
- CP-37 Role segmented control → placed, same form
- CP-38 Reason input → placed, same form
- CP-39 "Set role" submit button → placed, same form
- CP-40 Write-result note beside person form → kept: a write's result is a status line where the control was (`.pl-said`, shown in frame 11 for the role change). ! not drawn for this control
- CP-41 Card "Role events" → placed — PlacePeople.tsx:WsRoles
- CP-42 Event list / empty state → placed ("No role has been changed here.")
- CP-43 Card "Join link" → placed, PlacePeople.tsx:WsPeople side column, canManage
- CP-44 Join-link status paragraph → placed, same card
- CP-45 Hint about requests waiting in Platform people → placed, folded into the same paragraph as CP-44
- CP-46 One-time reveal banner + link field + Copy → ! NOT DRAWN IN THIS MOCK (a cost, see notes above) (the mock's link is always just shown "on", never revealed)
- CP-47 Write-result note after link actions → kept: a write's result is a status line where the control was (`.pl-said`, shown in frame 11 for the role change). ! not drawn for this control
- CP-48 "Make a new link" button → placed
- CP-49 "Turn the link off" button → placed
- CP-50 "Turn the link on" button / off state → ! NOT DRAWN IN THIS MOCK (a cost, see notes above) (the mock only has an "on" state; the off branch is never drawn)
- CP-51 Card "Joined by request" → placed, renamed "Let in by request" — PlacePeople.tsx:WsPeople
- CP-52 "Approving does not let anyone in yet…" notice → ! NOT DRAWN IN THIS MOCK (a cost, see notes above)
- CP-53 Write-result note ("membership ended") → kept: a write's result is a status line where the control was (`.pl-said`, shown in frame 11 for the role change). ! not drawn for this control
- CP-54 Empty-state "Nobody holds a membership…" → placed, same wording
- CP-55 Table columns (Person/Let in by/When/Access/Actions) → placed, reshaped into a `<ul>` list (Person + merged "let in by … · when" + Remove); no separate "Access" column
- CP-56 "Remove" button per row → placed
- CP-57 Card "Members' GitHub connections" → placed, reshaped as a `<details className="pl-fold">` with 3 hardcoded rows, inside the People tab's side column — PlacePeople.tsx:WsPeople; not platform-admin gated (gated by `canManage`, which also includes workspace admins)
- CP-58 Admin-gate (403) panel → kept as today (a read state: refresh, loading, error, 403 or empty); the place pages would wrap the same reads in the same states; not drawn in a frame (fixtures always answer). ! not shown
- CP-59 Refresh control on Members card → kept as today (a read state: refresh, loading, error, 403 or empty); the place pages would wrap the same reads in the same states; not drawn in a frame (fixtures always answer). ! not shown
- CP-60 Loading/error region for the read → kept as today (a read state: refresh, loading, error, 403 or empty); the place pages would wrap the same reads in the same states; not drawn in a frame (fixtures always answer). ! not shown
- CP-61 Empty-state "No member has started" → kept as today (a read state: refresh, loading, error, 403 or empty); the place pages would wrap the same reads in the same states; not drawn in a frame (fixtures always answer). ! not shown
- CP-62 Lead paragraph describing the table → ! NOT DRAWN IN THIS MOCK (a cost, see notes above)
- CP-63 Column headers (Member/Connection/Owners/Repositories) → ! NOT DRAWN IN THIS MOCK (a cost, see notes above) (it is a plain list now, not a table with these headers)
- CP-64 Per-member row → placed, partial: name/GitHub handle/repo count/connection mark present; no "owners" info — PlacePeople.tsx:WsPeople fold
- CP-65 "Each member connects as themselves…" hint → placed, same fold, reworded slightly ("never a token")

## Workspace Settings (WS)

All embedded unchanged via `WorkspaceSettingsScreen(part="settings")` inside PlaceWorkspace's Settings
tab (open to every role, not just canManage) — PlacePeople.tsx:PlaceWorkspace.

- WS-01 Page header + refresh → placed, partial: the refresh control (`UrRefresh`) stays (`.pl-embed .c-phead` is not hidden), but the literal "Workspace Settings" `<h1>` is hidden (`.pl-embed .c-phead h1{display:none}`), superseded by Place's own workspace-name heading
- WS-02 Config tabs (Settings / Workspace setup / Add a new workspace) → ! NOT DRAWN IN THIS MOCK (a cost, see notes above). `.pl-embed .cfg-parts{display:none}` hides this strip entirely; Place's own People/Roles/Settings/Setup tab strip is a different, higher-level nav that does not include "Add a new workspace" as an option
- WS-03 "This workspace" card → placed unchanged — WorkspaceSettings.tsx:ThisWorkspace
- WS-04 Lead banner variants → placed unchanged — WorkspaceSettings.tsx:StripLead
- WS-05 Card body (Name/Id/Kind/Role) → placed unchanged
- WS-06 Default role row → placed unchanged
- WS-07 "Rename" button → placed unchanged
- WS-08 Rename form → placed unchanged — WorkspaceSettings.tsx:RenameForm
- WS-09 "Code intelligence" card lead → placed unchanged — CodeIntelligence.tsx:CodeIntelligenceCard
- WS-10 "Send code to Jev" switch → placed unchanged — CodeIntelligence.tsx:JevSwitch
- WS-11 "Visual QA" card lead → placed unchanged — WorkspaceSettings.tsx:QaSettingsCard
- WS-12 QA flows-per-step read-only figure → placed unchanged
- WS-13 QA flows-per-step form → placed unchanged — WorkspaceSettings.tsx:QaFlowsPerStep
- WS-14 "Per-agent budget" card lead → placed unchanged — WorkspaceSettings.tsx:WorkspaceBudgetCard
- WS-15 Budget read-only view → placed unchanged — WorkspaceSettings.tsx:BudgetReadOnly
- WS-16 Budget form → placed unchanged — WorkspaceSettings.tsx:BudgetForm

## Workspace setup (WSU)

All embedded unchanged via `WorkspaceSettingsScreen(part="setup")` inside PlaceWorkspace's Setup tab,
which is only offered when `canManage` is true (an unprivileged member loses this tab entirely in
place, unlike the original's "member, workspace admin, platform admin" access) — PlacePeople.tsx:PlaceWorkspace.

- WSU-01 Page header + refresh → placed, partial, same as WS-01 (refresh kept, title hidden)
- WSU-02 Config tabs → ! NOT DRAWN IN THIS MOCK (a cost, see notes above), same as WS-02
- WSU-03 Lead banner "Step N of M" → placed unchanged — WorkspaceSettings.tsx:SetupLead (canManage-gated, see note above)
- WSU-04 Lead banner once complete → placed unchanged — WorkspaceSettings.tsx:StripLead
- WSU-05 Intro paragraph → placed unchanged — Onboarding.tsx:OnboardingScreen
- WSU-06 Checklist loading/error/empty → placed unchanged
- WSU-07 Checklist header → placed unchanged
- WSU-08 Count-summary + step bar → placed unchanged — Onboarding.tsx:StepBar
- WSU-09 Checklist rows → placed unchanged — Onboarding.tsx:Checklist
- WSU-10 Step issue/error copy → placed unchanged — Onboarding.tsx:IssueCopy
- WSU-11 "Last probe did not complete" note → placed unchanged
- WSU-12 Install-the-App help note → placed unchanged
- WSU-13 Per-step action buttons → placed unchanged — Onboarding.tsx:StepAction
- WSU-14 Workspace step "none" state → placed unchanged — OnboardingWorkspace.tsx:WorkspaceStepBody
- WSU-15 Workspace step "requested" state → placed unchanged
- WSU-16 Workspace step approved/applying/needs_owner → placed unchanged — ApprovedWaiting/JobSteps
- WSU-17 Workspace step "ready" state → placed unchanged
- WSU-18 Workspace step "denied" state → placed unchanged
- WSU-19 Workspace step "failed" state → placed unchanged
- WSU-20 Workspace step refusal/optional-step note → placed unchanged
- WSU-21 Claude-account step body → placed unchanged — OnboardingWorkspace.tsx:ClaudeAccountStepBody
- WSU-22 "After the checklist" card → placed unchanged — WorkspaceSettings.tsx:AfterChecklist
- WSU-23 "On Overview" status paragraph → placed unchanged — WorkspaceSettings.tsx:OnOverview
- WSU-24 "Show on Overview" button → placed unchanged
- WSU-25 GitHub section header → placed unchanged — PermissionCards.tsx:GitHubPermissions
- WSU-26 GitHub section loading/error → placed unchanged
- WSU-27 GitHub section lead paragraph → placed unchanged
- WSU-28 App permissions card → placed unchanged — PermissionCards.tsx:AppCard/PermCard
- WSU-29 "Add to App" fix link → placed unchanged
- WSU-30 "A platform admin adds these…" hint → placed unchanged
- WSU-31 App permissions "not read" error → placed unchanged
- WSU-32 Org permissions cards → placed unchanged — PermissionCards.tsx:OrgCard/OrgDetail
- WSU-33 "Review on GitHub" fix link → placed unchanged
- WSU-34 "No connected org" empty state → placed unchanged

## Add a new workspace (NW)

All embedded unchanged via `WorkspaceSettingsScreen(part="new")` inside a Dialog on the Workspaces-list
screen, reachable only via the "Add a new workspace" button which is itself only rendered for a
platform admin — PlacePeople.tsx:PlaceWorkspaces.

- NW-01 Page header + Config tabs → placed, partial: refresh kept (same CSS as WS-01); Config tabs ! NOT DRAWN IN THIS MOCK (a cost, see notes above) (hidden, same as WS-02); gating preserved (dialog only offered to platform admin)
- NW-02 "Reading whether you are a platform admin…" → kept as today (a read state: refresh, loading, error, 403 or empty); the place pages would wrap the same reads in the same states; not drawn in a frame (fixtures always answer). ! not shown
- NW-03 "Not available to you" lead → kept as today (a read state: refresh, loading, error, 403 or empty); the place pages would wrap the same reads in the same states; not drawn in a frame (fixtures always answer). ! not shown
- NW-04 "Add a new workspace" lead banner → placed unchanged — WorkspaceSettings.tsx:AddWorkspace
- NW-05 Workspace-kind tabs → placed unchanged — NewWorkspace.tsx:NewWorkspaceForm
- NW-06 Address field → placed unchanged
- NW-07 Display name field → placed unchanged
- NW-08 First admin's email field → placed unchanged
- NW-09 "What this gets" paragraph → placed unchanged — NewWorkspace.tsx:Gets
- NW-10 Refusal error message → placed unchanged — NewWorkspace.tsx:Refused
- NW-11 "Create workspace" submit button → placed unchanged
- NW-12 First-admin outcome message → placed unchanged — NewWorkspace.tsx:FirstAdminOutcome
- NW-13 Progress: record id/name → placed unchanged — NewWorkspace.tsx:Progress
- NW-14 Progress "requested" state → placed unchanged
- NW-15 Progress approved/applying → placed unchanged
- NW-16 Progress "needs_owner" → placed unchanged
- NW-17 Progress "ready" → placed unchanged
- NW-18 Progress "denied" → placed unchanged
- NW-19 Progress "failed" → placed unchanged
- NW-20 Progress unknown-state dash → placed unchanged
- NW-21 WorkspaceTimeline → placed unchanged — NewWorkspace.tsx:WorkspaceTimeline
- NW-22 "Add another" button → placed unchanged — WorkspaceSettings.tsx:AddWorkspace
- NW-23 "What creating it does" card → placed unchanged
- NW-24 Explanatory paragraph → placed unchanged
- NW-25 "Filed <id>…" message → placed unchanged

## Admin › People and roles (AP)

Reimplemented against fixtures as Platform people — PlacePeople.tsx:PlacePeople (the `scoped`
boolean branches the same function for a non-platform workspace admin rather than a separate
component).

- AP-01 Page title → placed, renamed "Platform people" / scoped "People in your workspaces"
- AP-02 Subtitle → placed
- AP-03 Refresh control → kept as today (a read state: refresh, loading, error, 403 or empty); the place pages would wrap the same reads in the same states; not drawn in a frame (fixtures always answer). ! not shown
- AP-04 Admin-gate (403) region → kept as today (a read state: refresh, loading, error, 403 or empty); the place pages would wrap the same reads in the same states; not drawn in a frame (fixtures always answer). ! not shown
- AP-05 Full-platform "nobody waiting" lead → kept as today (a read state: refresh, loading, error, 403 or empty); the place pages would wrap the same reads in the same states; not drawn in a frame (fixtures always answer). ! not shown
- AP-06 Full-platform "N requests waiting + Review" lead → placed, reshaped: the full "Waiting for you" card is always shown directly rather than teased behind a lead + button
- AP-07 Full-platform lead unread state → kept as today (a read state: refresh, loading, error, 403 or empty); the place pages would wrap the same reads in the same states; not drawn in a frame (fixtures always answer). ! not shown
- AP-08 Workspace-admin "nobody waiting" lead → placed — `pl-behind is-ok` banner
- AP-09 Workspace-admin "N waiting + Review" lead → ! NOT DRAWN IN THIS MOCK (a cost, see notes above) (the scoped branch hardcodes a permanent zero, no N>0 path exists)
- AP-10 Workspace-admin lead unread state → kept as today (a read state: refresh, loading, error, 403 or empty); the place pages would wrap the same reads in the same states; not drawn in a frame (fixtures always answer). ! not shown
- AP-11 "Workspace building is off" banner → kept as today (a read state: refresh, loading, error, 403 or empty); the place pages would wrap the same reads in the same states; not drawn in a frame (fixtures always answer). ! not shown
- AP-12 "requests to join could not be read" banner → kept as today (a read state: refresh, loading, error, 403 or empty); the place pages would wrap the same reads in the same states; not drawn in a frame (fixtures always answer). ! not shown
- AP-13 "Waiting for you" card → placed
- AP-14 "Nothing else is waiting" empty line → each answered request is replaced by its result line in place (PlacePeople.tsx:ReqActs); the empty line is today's, kept
- AP-15 Workspace-request row (limit chips/ceiling note) → placed, partial: shown as plain text "default limits", not the structured `LimitChips` component
- AP-16 Approve control + busy + outcome → placed, partial: button renders, inert (no onClick), no outcome line
- AP-17 Deny inline reason textarea → Platform people › Waiting for you: Deny opens a reason box in place (PlacePeople.tsx:ReqActs)
- AP-18 Loan-request pool-size sentence → Platform people › Lend dialog: "The pool holds 3 accounts; 1 is lent" (PlacePeople.tsx:ReqActs)
- AP-19 Lend control (picker/confirm dialog) → Platform people › Waiting for you › Lend… opens the dialog that asks which account (PlacePeople.tsx:ReqActs); also on Home
- AP-20 Decline control (reason input) → Platform people › Decline asks a reason in place (PlacePeople.tsx:ReqActs)
- AP-21 Join-request row (join a group workspace) → Platform people › Waiting for you: "Requests to join a group workspace: none waiting" line; a join request would be a row of this same list (PlacePeople.tsx `.pl-join-none`)
- AP-22 Join Approve control → same row kind as AP-21, Approve in one click; none open in the fixtures
- AP-23 Join Deny control → same row kind as AP-21, Deny with a reason; none open in the fixtures
- AP-24 Workspace-admin "Waiting for you" (join only) card → ! NOT DRAWN IN THIS MOCK (a cost, see notes above)
- AP-25 "workspaces' roles could not be read" banner → kept as today (a read state: refresh, loading, error, 403 or empty); the place pages would wrap the same reads in the same states; not drawn in a frame (fixtures always answer). ! not shown
- AP-26 Grid card head (filter + count + Grant admin) → placed, reshaped: filter+count stay on the grid card; "Grant admin" is relocated to the separate "Platform admins" side card
- AP-27 Workspace filter → placed
- AP-28 Count chip → placed
- AP-29 "Grant admin" button + GrantDialog → placed, partial: button exists (relocated, see AP-26/AP-53); no dialog opens (inert)
- AP-30 "Find a person" search box → placed
- AP-31 "No joined-by-request mark" notice → ! NOT DRAWN IN THIS MOCK (a cost, see notes above)
- AP-32 Empty state "no one has signed in" → kept as today (a read state: refresh, loading, error, 403 or empty); the place pages would wrap the same reads in the same states; not drawn in a frame (fixtures always answer). ! not shown
- AP-33 Scoped empty state → kept as today (a read state: refresh, loading, error, 403 or empty); the place pages would wrap the same reads in the same states; not drawn in a frame (fixtures always answer). ! not shown
- AP-34 NobodyShown filtered-empty message → kept as today (a read state: refresh, loading, error, 403 or empty); the place pages would wrap the same reads in the same states; not drawn in a frame (fixtures always answer). ! not shown
- AP-35 Full table columns → placed, partial: Person/per-workspace roles/Own workspace/Last active/Manage present; Platform role, Claude account and Ceiling are not separate columns (see AP-38/41/42)
- AP-36 Scoped table columns → placed, exact match
- AP-37 Person cell → placed (GitHub mark simplified to an "expired" warning only)
- AP-38 Platform role cell → ! NOT DRAWN IN THIS MOCK (a cost, see notes above) (shown only as a tag inside the Person cell, not its own column)
- AP-39 Role cell (word/badge/none/requested/joined-mark) → placed, partial: word/badge/none present; no "requested" state, no joined-by-request mark
- AP-40 Own-workspace cell → placed, partial: ready/requested/none states present; no failure note, denial reason or job-step progress
- AP-41 Claude-account cell (in the grid) → ! NOT DRAWN IN THIS MOCK (a cost, see notes above) (that info only appears inside the Manage drawer)
- AP-42 Ceiling cell (in the grid) → ! NOT DRAWN IN THIS MOCK (a cost, see notes above) (same, only in the Manage drawer)
- AP-43 Last-active cell → placed
- AP-44 "Manage" button → placed
- AP-45 Drawer Facts section → placed — `dl.pl-facts`
- AP-46 Drawer Ceiling section → placed, partial: "Change" button present and inert; no stepper/confirm dialog
- AP-47 Drawer Claude account section → placed, partial: "Lend…" button present and inert
- AP-48 Drawer Admin switch → placed, display-only: a disabled, read-only checkbox; no confirm dialog, not actually toggleable
- AP-49 Drawer "Roles in workspaces" section → placed, fully interactive — `ul.pl-roles-edit` with pending-change confirm bar and Save
- AP-50 Drawer chrome → placed, partial: scrim-click + Close button close it; no focus trap, no Escape-key handling (grep confirms no `onKeyDown`/`Escape` in PlacePeople.tsx)
- AP-51 "Workspace roles" admin card (per-workspace card) → ! NOT DRAWN IN THIS MOCK (a cost, see notes above) on this screen; the equivalent per-workspace information is redistributed onto the Workspaces screen's own WsPeople/WsRoles tabs instead
- AP-52 Workspace-roles card list states → ! NOT DRAWN IN THIS MOCK (a cost, see notes above), same reason
- AP-53 "Admins" side card → placed; inline Grant-admin form/confirm dialogs not wired (inert button only)
- AP-54 "Recent admin actions" side card → placed, not foldable (always expanded, unlike the original's fold)
- AP-55 Audit body → placed, partial: per-event lines render; no empty-state text, no housekeeping fold/unfold
- AP-56 Outcome/error line after a write (SaidLine) → placed, but only demoed for one scripted scenario (`state==='changed'`, the "Mei Chen admin of design" message), not wired generally to the other forms

## Workspaces you admin (WYA)

Reimplemented as the Workspaces list — PlacePeople.tsx:PlaceWorkspaces.

- WYA-01 Page title → placed, renamed "Workspaces"
- WYA-02 Refresh control → kept as today (a read state: refresh, loading, error, 403 or empty); the place pages would wrap the same reads in the same states; not drawn in a frame (fixtures always answer). ! not shown
- WYA-03 Subtitle → placed
- WYA-04 Error/retry region for the read → kept as today (a read state: refresh, loading, error, 403 or empty); the place pages would wrap the same reads in the same states; not drawn in a frame (fixtures always answer). ! not shown
- WYA-05 Empty state "You administer no group workspace" → placed, generic wording only: falls through to the search-mismatch fallback (`No workspace matches "…"`), not a dedicated message
- WYA-06 "Behind" lead card → placed — `pl-behind` banner naming the furthest-behind workspace
- WYA-07 List of administered workspaces → placed
- WYA-08 "admin as a platform admin" marker → placed, moved to the one-workspace page's header chip row (`{isPlatform && <span>as a platform admin</span>}`), not shown per-row on the list
- WYA-09 Per-workspace links (Workspace Settings / MCP servers / People and roles / Secrets) → placed, partial: one link opens the workspace page, whose tabs cover Settings and People/Roles; MCP servers and Secrets have **no equivalent anywhere** in place code (grep for "mcp"/"secret" across place/*.tsx finds nothing relevant)
- WYA-10 Setup progress strip + "reading/not read" states → placed, partial: the `ProgressBar` renders; the failure states ("Reading setup…"/"Setup not read") are not modeled (fixtures always resolved)
- WYA-11 Setup-progress "next" click opens Config → ! NOT DRAWN IN THIS MOCK (a cost, see notes above) for ordinary rows (no click target on the per-row "next" text); only the single global "furthest behind" row gets an actual "Open its setup" button
- WYA-12 Design note (personal workspaces never listed; general-reach-only excluded) → placed (behavior), consistent: the list is built from `mineIds`/`HOME_ADMINISTERED`, with the caller's own personal workspace added as a distinct first row — PlacePeople.tsx:PlaceWorkspaces

## Nav (NAV) — Spine.tsx / ConfigSetup.tsx

- NAV-01 "On this page" panel (8 jump links) → placed, redrawn as 6 items — Spine.tsx:OVERVIEW_JUMPS = jumps.ts:PLACE_JUMPS when `HOMESET==='place'`; clicks select a deployment tab via PlaceHome.tsx's own click listener
- NAV-02 Live count badges on 4 of 8 jumps → the Home panel jumps Now, Waiting and Failures carry Overview's counts (jumps.ts `count`, Spine.tsx MOCK-HOOK)
- NAV-03 Config section panel (6 rows incl. GitHub access/Repositories/Secrets/MCP servers) → ! NOT DRAWN IN THIS MOCK (a cost, see notes above). No place file renders a Config panel; "People and roles" and "Workspace Settings" are superseded by tabs on the workspace page, and GitHub access/Repositories/Secrets/MCP servers have no equivalent anywhere (grep confirms)
- NAV-04 Config row setup badges → ! NOT DRAWN IN THIS MOCK (a cost, see notes above), same reason
- NAV-05 Admin panel (9 rows) → placed, partial: 2 of 9 rows are relabeled for place (`Spine.tsx:204` "People and roles"→"Platform people", `:208` "Workspaces you admin"→"Workspaces"); the other 7 (Pool limits, Runner sizes, Tenants, Platform counts, Platform secrets, Setup, Health) are untouched by design
- NAV-06 Admin panel two groups → placed, unaffected (no MOCK-HOOK edit; continues to work as before)
- NAV-07 Admin row status badges → placed, unaffected (badge logic keyed by `p.key`, unaffected by the relabeling)
- NAV-08 "In Automate" group → placed, unaffected (no MOCK-HOOK edit)
- NAV-09 Capacity › Accounts admin pill → placed, unaffected (no MOCK-HOOK edit)
- NAV-10 SetupStrip on every Config page → ! NOT DRAWN IN THIS MOCK (a cost, see notes above). Not referenced by any place/*.tsx file (grep confirms), and it is mounted by App.tsx's `ConfigPage` wrapper, which place's embeds bypass entirely
- NAV-11 Compact SetupMeter in the Config panel → ! NOT DRAWN IN THIS MOCK (a cost, see notes above), same reason (grep confirms no reference)

## Home / App.tsx navigation test

- HOME-01 Root `/` → `/overview` fallback, and Overview as universal fallback → placed, analogous mechanism: `HomeRedraw.tsx:205` falls back an unrecognized `?homescreen=` to `'home'` the same way, and `Place.tsx:PLACE_SCREENS` routes both the `home` and `overview` keys to the same `PlaceHome` component; this is the mock-scaffold's own query-param routing, not the real app's `addressToPath`/`pathToAddress` (untouched, still governs the actual console when no `?homeset` is present)

## Reads per page

- Overview's own reads (loadCapacity/loadTasks/loadLeases/loadProviders/loadAccountPool/loadWorkflows/loadStats/loadSpend/loadTasksInState/loadLiveSession/loadTask) → placed unchanged, since `<OverviewScreen />` is embedded as-is; `loadOnboarding`/`dismissOnboarding` do **not** fire in place Home because the `setup` slot that would trigger them is never rendered (see OV-06); `loadApprovals` is additionally called a second time, directly, by PlaceHome.tsx itself for the personal band
- Config People and roles reads (loadMe/loadWorkspaceRoles/setWorkspacePersonRole/…/loadAccessMembers) → NOT PLACED. WsPeople/WsRoles use only mock fixtures (`homesetRoster`, `homesetMembers`); none of these API functions are imported by PlacePeople.tsx
- Workspace Settings reads (loadMe/loadProfile/loadWorkspaceRoles/renameWorkspace/…) → placed unchanged, since `WorkspaceSettingsScreen` is embedded as-is for the Settings tab
- Workspace setup reads (loadProfile/loadOnboarding/loadWorkspace/requestWorkspace/requestLoan/loadForgePermissions/loadMe) → placed unchanged, embedded as-is for the Setup tab
- Add a new workspace reads (loadProfile/createAdminWorkspace/loadAdminWorkspace) → placed unchanged, embedded as-is in the dialog
- Admin People and roles reads (loadPeople/approveWorkspace/…/clearAdminWorkspacePersonRole) → NOT PLACED. PlacePeople.tsx's `PlacePeople` function imports no API module at all; everything is fixture-driven
- Workspaces you admin reads (loadMyTenants) → NOT PLACED, same reason — PlaceWorkspaces uses `HOME_WORKSPACES`/`HOME_ADMINISTERED` fixtures only

## Path constants found

- `overview/now` = `/overview`, also root `/` target → unaffected by place; still governs the real app when `?homeset` is absent (see HOME-01)
- `config/people` = `/config/people` → not a place route; place reaches the equivalent content via `?homeset=place&homescreen=people` (member) or `=settings&tab=people` (admin, per-workspace)
- `config/workspace` = `/config/workspace` → not a place route; place reaches it via `?homescreen=settings&ws=<id>&tab=settings`
- `WORKSPACE_SETUP_PATH` = `/config/workspace/setup` → not a place route; place reaches it via `?homescreen=settings&ws=<id>&tab=setup`
- `WORKSPACE_NEW_ADDRESS` = `config/workspace?part=new` → not a place route; place reaches it via the Workspaces-list "Add a new workspace" dialog (`?homescreen=workspaces`)
- `admin/people` = `/admin/people` → not a place route; place reaches the equivalent via `?homescreen=people` (non-member)
- `admin/workspaces` = `/admin/workspaces` → not a place route; place reaches the equivalent via `?homescreen=workspaces`

