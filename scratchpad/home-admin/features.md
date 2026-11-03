<!-- MOCK-HOOK FILE -->

# Feature inventory — Overview, Config, Admin and related console pages

Read from source in `apps/swarm-ui/src`. IDs are grouped by page; roles are
`member` / `workspace admin` / `platform admin` (a row may list more than
one). "State" says when the feature is drawn (always / while loading / when
empty / on error / admin only / etc.).

## Overview (OV)

Note: Overview.tsx does **not** import or render `QuestionsCard.tsx` — that
component belongs to `AgentDetail.tsx`, not this page.

- OV-01 | Page title "Overview" with the section-help `?` link | Overview.tsx:OverviewScreen (PageHead) | member, workspace admin, platform admin | always
- OV-02 | Refresh control: age of newest read, "Paused · resume" after 15 min idle, manual refresh button | Overview.tsx:OverviewScreen (RefreshControl) | member, workspace admin, platform admin | always; "stale"/"reading" sub-states
- OV-03 | "On this page" jump-count strip above the lead (problems, agents holding capacity, waiting, recent failures) | Overview.tsx:OverviewScreen via Spine.tsx:OverviewCounts | member, workspace admin, platform admin | always, counts hidden per-section while that section's checks/reads are still loading
- OV-04 | Workspace-scope note ("workspace <tenant>" / "workspace —" / "workspace reading…") | Overview.tsx:OverviewScreen (CountNote span.ov-scope) | member, workspace admin, platform admin | always; 3 sub-states (reading / no tenant found / named)
- OV-05 | Read tally ("N/8 reads") with tone dot and `?` help note | Overview.tsx:OverviewScreen (CountNote span.ov-tally, HelpCard topic="absent-vs-zero") | member, workspace admin, platform admin | always; tone varies ok/pending/refused(admin)/broken
- OV-06 | Setup checklist card ("Set up SwarmCloud"), entry point passed from App | Onboarding.tsx:SetupCard, rendered via Overview.tsx:OverviewScreen `{setup}` slot | member, workspace admin, platform admin | only while checklist incomplete and not dismissed; absent once complete, hidden, or read not landed
- OV-07 | Setup card header actions: step-count chip, Hide button, "Open setup" primary link | Onboarding.tsx:SetupCard | member, workspace admin, platform admin | while card is shown
- OV-08 | Setup card step-progress bar (one segment per step) | Onboarding.tsx:StepBar | member, workspace admin, platform admin | while card is shown
- OV-09 | Setup checklist steps list (Sign in, Request your workspace, Add a Claude account, Connect GitHub, Install the App, Enable orgs, Choose repositories, Verify access, Ready), each with state mark, evidence line, issue copy, and its own action button | Onboarding.tsx:Checklist / StepAction | member, workspace admin, platform admin | each row's action only drawn when that step is not done / is waiting
- OV-10 | Setup card footer note explaining Hide vs. Workspace Settings persistence | Onboarding.tsx:SetupCard | member, workspace admin, platform admin | while card is shown
- OV-11 | "Waiting on you" card entry point (Automate approvals inbox), passed from App | Schedules.tsx:WaitingOnYouCard, rendered via Overview.tsx:OverviewScreen `{waitingOnYou}` slot | member, workspace admin, platform admin | only drawn when the inbox holds ≥1 item; nothing while reading/empty/failed
- OV-12 | "Waiting on you" header with live count badge | Schedules.tsx:WaitingOnYouCard | member, workspace admin, platform admin | while card is shown
- OV-13 | "Waiting on you" list rows: kind label, linked summary (or plain text if no subject address), relative age | Schedules.tsx:WaitingOnYouCard | member, workspace admin, platform admin | shows up to 5 oldest items
- OV-14 | "Waiting on you" footer note ("oldest N of M… waiting items hold no capacity") | Schedules.tsx:WaitingOnYouCard | member, workspace admin, platform admin | while card is shown, extra clause only when truncated
- OV-15 | "Needs a look" section heading with aggregate count/accessible summary | Overview.tsx:NeedsALook | member, workspace admin, platform admin | always
- OV-16 | "Needs a look" blind-checks badge ("N blind") | Overview.tsx:NeedsALook | member, workspace admin, platform admin | only when ≥1 check is blind; tone differs if all blind checks are admin-only
- OV-17 | "Needs a look" partial mark (pill) when some checks are blind but problems exist | Overview.tsx:NeedsALook (Mark kind="partial") | member, workspace admin, platform admin | while blind.length>0 and reading.length===0 and problems.length>0
- OV-18 | "Needs a look" reading state: skeleton check cards (height held from last render) | Overview.tsx:NeedsALook | member, workspace admin, platform admin | while any check status is "reading"
- OV-19 | "Needs a look" problem cards, one per derived problem (dispatch paused, leases overdue/silent/past-TTL, provider quota throttled/exhausted/disabled, accounts REAUTH_REQUIRED, pools over ceiling, stalled workflows, parked-too-long, recent-failures-spike) | Overview.tsx:CheckCard / checks.ts:deriveChecks | member, workspace admin, platform admin (Leases check's problems are admin-only) | while problems.length>0 and reading.length===0
- OV-20 | "Needs a look" check card: mark, title, id-line with linked workflow ids, detail line, scoped link, optional per-workflow links | Overview.tsx:CheckCard | member, workspace admin, platform admin | per problem card; Leases check problems are platform-admin only
- OV-21 | "Needs a look" all-clear absence state ("Nothing wrong" / "N not checked") | Overview.tsx:NeedsALook (CardAbsent) | member, workspace admin, platform admin | when problems.length===0; sub-state "partial"/"admin" when some checks are blind
- OV-22 | Hand-off cards: one card per agent waiting for a person (reason, expected site, time-left clock, controller) | Overview.tsx:HandoffCards / Handoff.tsx:HandoffBanner, handoffOf, needsYou | member, workspace admin, platform admin | only drawn when ≥1 task `needsYou`; up to 3 shown, oldest-waiting first
- OV-23 | Hand-off card "who is waiting" line (step/agent kind, short task id, waiting age, workflow title) | Overview.tsx:handoffWho | member, workspace admin, platform admin | per hand-off card
- OV-24 | Hand-off card "Take control" button (held/hold-phase only) | Overview.tsx:HandoffTakeControl / LiveBrowser.tsx:LiveControl | member, workspace admin, platform admin (further gated by live-session `you.can_control`) | only on a held (RUNNING, phase="hold") hand-off
- OV-25 | Hand-off card "I'm ready" action (parked phase) | Handoff.tsx:HandoffBanner | member, workspace admin, platform admin | only on a parked (phase="parked") hand-off
- OV-26 | Hand-off card "Open →" link to the agent | Overview.tsx:HandoffCardList | member, workspace admin, platform admin | per hand-off card
- OV-27 | Hand-off "+N more waiting" link to Agents › Waiting | Overview.tsx:HandoffCardList | member, workspace admin, platform admin | when more than 3 agents are waiting for a person
- OV-28 | Lifecycle band region (Waiting / Holding capacity / Finished today) | OverviewRegions.tsx:LifecycleBand | member, workspace admin, platform admin | always
- OV-29 | Lifecycle band "Waiting" cell, broken down by QUEUED/READY/PARKED state marks | OverviewRegions.tsx:LifecycleBand | member, workspace admin, platform admin | counted from `/v1/stats`
- OV-30 | Lifecycle band "Holding capacity" cell, broken down by LEASED/DISPATCHED/STARTING/RUNNING state marks | OverviewRegions.tsx:LifecycleBand | member, workspace admin, platform admin | counted from `/v1/stats`
- OV-31 | Lifecycle band "Finished today" cell, broken down by SUCCEEDED/FAILED/DEAD_LETTERED/CANCELLED marks, with "≥N" lower-bound + partial mark | OverviewRegions.tsx:LifecycleBand | member, workspace admin, platform admin | counted off the task page; partial sub-state when page has a next-page
- OV-32 | Lifecycle band per-cell stale/age footer ("counted N min ago") | OverviewRegions.tsx:LifecycleBand | member, workspace admin, platform admin | only when stats read is stale or aged
- OV-33 | Lifecycle band per-cell page-coverage footer ("of the N newest read", "no agent exists") | OverviewRegions.tsx:LifecycleBand | member, workspace admin, platform admin | "Finished today" cell only
- OV-34 | "Running now" card header, note ("N listed"), "All live →" link | Overview.tsx:RunningCard (CardHead) | member, workspace admin, platform admin | always
- OV-35 | "Running now" card loading skeleton | Overview.tsx:RunningCard | member, workspace admin, platform admin | while task read is loading
- OV-36 | "Running now" card error/blind absence ("Agent list unread") | Overview.tsx:RunningCard (CardAbsent) | member, workspace admin, platform admin | on task-read error
- OV-37 | "Running now" card real-zero absence ("No agent exists") | Overview.tsx:RunningCard (CardAbsent) | member, workspace admin, platform admin | when task read is empty
- OV-38 | "Running now" card "Nothing running" absence, cross-checked against `/v1/stats` count | Overview.tsx:RunningCard (CardAbsent) | member, workspace admin, platform admin | when no HOLDING-state rows found
- OV-39 | "Running now" table: State / Agent·profile / Runtime / Cost so far columns | Overview.tsx:RunningCard, RunningRow | member, workspace admin, platform admin | desktop; up to 8 rows then a "N more" disclosure
- OV-40 | "Running now" row: state mark, agent/workflow title link, silent-worker chip, runner profile, workflow link | Overview.tsx:RunningRow | member, workspace admin, platform admin | "silent" chip only when the admin-gated leases read supplied a silent-duration
- OV-41 | "Running now" row Runtime cell (elapsed / "leased" / dash with reason when lease unread) | Overview.tsx:Runtime | member, workspace admin, platform admin | dash sub-state when leases read failed/admin-gated
- OV-42 | "Running now" row Cost so far cell (figure, "last attempt" sub-text, or dash with reason) | Overview.tsx:CostSoFar | member, workspace admin, platform admin | dash sub-states: not served / not yet reported / unread
- OV-43 | "Running now" phone list (mark, name, elapsed), with its own "N more" disclosure | Overview.tsx:PhoneRunRow | member, workspace admin, platform admin | phone width only
- OV-44 | "Running now" card footer (row count vs. stats count, "cost so far shows once an attempt ends") | Overview.tsx:RunningCard | member, workspace admin, platform admin | always once landed
- OV-45 | "Waiting, and why" card header, note (total waiting · "none cost anything"), "All waiting →" link | Overview.tsx:OverviewScreen (CardHead) | member, workspace admin, platform admin | always
- OV-46 | "Waiting, and why" grouped list rows (state mark, reason title, detail line, count) | OverviewRegions.tsx:WaitingWhy / waitGroups | member, workspace admin, platform admin | up to 6 groups, then "N more reasons" link
- OV-47 | "Waiting, and why" absence states (unread list / "None among the N newest") | OverviewRegions.tsx:WaitingWhy | member, workspace admin, platform admin | on task-read failure or zero waiting rows
- OV-48 | "Recent token cost" card header, note ("last N attempt"), "Timeline →" link, `?` help note | Overview.tsx:OverviewScreen (CardHead, explain="token-cost") | member, workspace admin, platform admin | always
- OV-49 | "Recent token cost" loading skeleton | Overview.tsx:SpendBody | member, workspace admin, platform admin | while spend rollup is loading
- OV-50 | "Recent token cost" error absence (task-read failed vs. attempt-read failed) | Overview.tsx:SpendBody (CardAbsent) | member, workspace admin, platform admin | on error
- OV-51 | "Recent token cost" zero absences ("No agent exists" / "No attempt has ended" / "No agent has run") | Overview.tsx:SpendBody (CardAbsent) | member, workspace admin, platform admin | on empty rollup, three distinct causes
- OV-52 | "Recent token cost" big figure (dollar sum or dash) with absent/partial mark | Overview.tsx:SpendBody | member, workspace admin, platform admin | partial mark on a measured figure (sample); absent mark when nothing reported
- OV-53 | "Recent token cost" four-token-count list (input/output/cache read/cache write) | Overview.tsx:TokenMix | member, workspace admin, platform admin | each count independently dash vs. figure
- OV-54 | "Recent token cost" provenance footer (sample coverage, unmeasured count, failed-reads count, staleness, "updates only on refresh") | Overview.tsx:SpendBody | member, workspace admin, platform admin | each clause conditional on its own count
- OV-55 | "Headroom" card header, "Pools →" link, `?` help note | Overview.tsx:OverviewScreen (CardHead, explain="pools-all-at-once") | member, workspace admin, platform admin | always
- OV-56 | Headroom "Can start now, by runner profile" sub-heading and per-profile tiles | Overview.tsx:HeadroomBody, CapacityStacks, ProfileTile | member, workspace admin, platform admin | always
- OV-57 | Headroom profile tile: disabled state ("disabled", reason) | Overview.tsx:ProfileTile | member, workspace admin, platform admin | when `profile.available === false`
- OV-58 | Headroom profile tile: figure (+N / 0 / dash) and "binds <pool(s)>" or refusal reason line | Overview.tsx:ProfileTile | member, workspace admin, platform admin | normal case
- OV-59 | Headroom card capacity-read absence/empty states ("Pool state unread" / "No pools exist" / "No runner profile") | Overview.tsx:CapacityStacks (CardAbsent) | member, workspace admin, platform admin | on capacity-read error/empty/no-profiles
- OV-60 | Headroom card footer (pool count read, whose workspace, staleness) | Overview.tsx:CapacityStacks | member, workspace admin, platform admin | always once landed
- OV-61 | Headroom "Subscription accounts" sub-heading and one-line account summary | Overview.tsx:HeadroomBody, AccountLine | member, workspace admin, platform admin | always
- OV-62 | Account line: usable/total figure, best-account % used + window, staleness | Overview.tsx:AccountLine / accountHeadroom | member, workspace admin, platform admin | dash sub-state when nothing measured
- OV-63 | Account line "N projected (...) not counted" note | Overview.tsx:AccountLine | member, workspace admin, platform admin | when projected accounts exist
- OV-64 | Account line "N needs sign-in" warning chip | Overview.tsx:AccountLine (WarnMark) | member, workspace admin, platform admin | when any account `needsAHuman`
- OV-65 | Account line "Accounts →" link | Overview.tsx:AccountLine | member, workspace admin, platform admin | always
- OV-66 | Account line absence states ("Account pool unread" / "No account registered") | Overview.tsx:AccountLine (CardAbsent) | member, workspace admin, platform admin | on accounts-read error/empty
- OV-67 | "Recent failures" card header, note (failures summary string), "All recent →" link | Overview.tsx:OverviewScreen (CardHead) | member, workspace admin, platform admin | always
- OV-68 | "Recent failures" table (mark, agent/workflow name+id, why, ended-ago time) for FAILED/DEAD_LETTERED rows | OverviewRegions.tsx:RecentFailures | member, workspace admin, platform admin | up to 6 rows then "N more" disclosure
- OV-69 | "Recent failures" "N cancelled" folded disclosure | OverviewRegions.tsx:RecentFailures | member, workspace admin, platform admin | only when cancelled rows exist
- OV-70 | "Recent failures" absence states (unread / partial-vs-zero / "None among the N newest") | OverviewRegions.tsx:RecentFailures | member, workspace admin, platform admin | on task-read failure, or zero failed/cancelled rows
- OV-71 | "Pools" card header, note (pool count · "busiest first" · "N lowered"), "Pools →" link | Overview.tsx:OverviewScreen (CardHead) / poolsNote | member, workspace admin, platform admin | always
- OV-72 | "Pools" full-width row of per-pool usage bars (name, usage track, active/limit, paused/full/near-full marks) | Overview.tsx:PoolsBody, PoolRow, busiestFirst | member, workspace admin, platform admin | sorted busiest-first
- OV-73 | "Pools" card absence states ("Pool state unread" / "No pools exist") | Overview.tsx:PoolsBody (CardAbsent) | member, workspace admin, platform admin | on capacity-read error/empty
- OV-74 | Session-expired full-page empty state ("Session expired", "Reload to sign in" button) | Overview.tsx:OverviewScreen | member, workspace admin, platform admin | when any read answers unauthenticated/session_expired

## Config › People and roles (CP)

- CP-01 | Tab strip switching between "Roles" and "Members' GitHub" views (`?view=members`) | WorkspacePeople.tsx:WorkspacePeopleScreen | member, workspace admin, platform admin | shown only to a platform admin, or when the URL already names `view=members`
- CP-02 | "Roles" tab option | WorkspacePeople.tsx:WorkspacePeopleScreen | member, workspace admin, platform admin | default/bare-path tab
- CP-03 | "Members' GitHub" tab option | WorkspacePeople.tsx:WorkspacePeopleScreen | member, workspace admin, platform admin | content behind it is platform-admin gated (see CP-57/CP-58)
- CP-04 | Page lead: "— this workspace's roles could not be read, so who holds one is not known." | WorkspacePeople.tsx:PeopleLead | member, workspace admin, platform admin | roles read errored or empty
- CP-05 | Page lead absent entirely for a personal workspace | WorkspacePeople.tsx:PeopleLead | workspace's own person | personal workspace, roles read ok
- CP-06 | Page lead "Read only for you": names the admin(s) or "nobody holds admin yet", plus "You hold…"/"Everyone else holds…" | WorkspacePeople.tsx:PeopleLead | member/viewer (non-admin) | group workspace, viewer is not can_manage
- CP-07 | Page lead "Step 4 · People invited — {reason}" when the setup read cannot say who holds a role | WorkspacePeople.tsx:PeopleLead | workspace admin, platform admin | setup-read's people step unreadable
- CP-08 | Page lead "Needs an admin": "N person/people are waiting for an admin to answer their request." | WorkspacePeople.tsx:PeopleLead | workspace admin, platform admin | can_manage and pending>0
- CP-09 | Page lead "Nobody else holds a role of their own yet" + "Give a person a role" button (scrolls/focuses invite field) | WorkspacePeople.tsx:PeopleLead | workspace admin, platform admin | can_manage, no pending, holders===0
- CP-10 | Page lead "Step 4 · done": "N other person holds/people hold a role; nothing here is waiting for you." | WorkspacePeople.tsx:PeopleLead | workspace admin, platform admin | can_manage, holders>0
- CP-11 | Card "Roles in this workspace" | WorkspacePeople.tsx:RolesPage | member, viewer, workspace admin, platform admin | group workspace, always
- CP-12 | Chip badge "read only for members" in the card's action row | WorkspacePeople.tsx:RolesPage | member, viewer | readOnly (group and not can_manage)
- CP-13 | Refresh control on the roster card | WorkspacePeople.tsx:RolesPage | member, viewer, workspace admin, platform admin | always
- CP-14 | Loading/error/empty region for `GET /v1/workspace/roles` with "Try again" retry | WorkspacePeople.tsx:RolesPage | member, viewer, workspace admin, platform admin | while loading/on error/when empty
- CP-15 | Personal-workspace message block ("You are this workspace's admin; a personal workspace has only you.") | WorkspacePeople.tsx:PersonalOnly | workspace's own person; platform admin | personal workspace
- CP-16 | Button-link "Open Workspace Settings" | WorkspacePeople.tsx:PersonalOnly | workspace's own person, platform admin | personal workspace
- CP-17 | Button-link "Add a new workspace" | WorkspacePeople.tsx:PersonalOnly | platform admin | personal workspace, viewer is platform admin
- CP-18 | "Your role" paragraph ("You hold {role} in {name}, {source}, by {granted_by} {time}.") | WorkspacePeople.tsx:Roster | member, viewer, workspace admin, platform admin | group workspace, roles read ok
- CP-19 | Platform-admin reach notice ("You are not in {name}'s group… recorded in its audit log.") | WorkspacePeople.tsx:Roster | platform admin | viewer reaches via platform_admin
- CP-20 | "Default role" paragraph (who holds the default, migration note) | WorkspacePeople.tsx:Roster | member, viewer, workspace admin, platform admin | group workspace, roles read ok
- CP-21 | Roster table "Who holds which role in …" | WorkspacePeople.tsx:People | member, viewer, workspace admin, platform admin | group workspace; stacks into cards below 720px
- CP-22 | Roster column: Person (email, "you" marker) | WorkspacePeople.tsx:People | member, viewer, workspace admin, platform admin | always
- CP-23 | Roster column: Role | WorkspacePeople.tsx:People | member, viewer, workspace admin, platform admin | always
- CP-24 | Roster column: Given by (incl. "(platform admin)" suffix) | WorkspacePeople.tsx:People | member, viewer, workspace admin, platform admin | always
- CP-25 | Roster column: When (relative time, title = exact timestamp) | WorkspacePeople.tsx:People | member, viewer, workspace admin, platform admin | always
- CP-26 | Roster column: Reason | WorkspacePeople.tsx:People | member, viewer, workspace admin, platform admin | always
- CP-27 | Roster column: Actions — "Return to default" button, busy/disabled while in flight | WorkspacePeople.tsx:People | workspace admin, platform admin | can_manage only
- CP-28 | Synthetic "you" row when caller holds no override | WorkspacePeople.tsx:People | member, viewer, workspace admin, platform admin | caller not in data.people
- CP-29 | Write-result note beside the roster | WorkspacePeople.tsx:RolesPage (Said at:'roster') | workspace admin, platform admin | after "Return to default" succeeds/fails
- CP-30 | Hint "Only this workspace's admins change roles. You can read them here." | WorkspacePeople.tsx:Roster | member, viewer | not can_manage
- CP-31 | "What each role permits" definition list | WorkspacePeople.tsx:RoleTable | member, viewer, workspace admin, platform admin | shown when summaries.length>0
- CP-32 | "Set the default" form: role select + "Set default" button | WorkspacePeople.tsx:DefaultForm | workspace admin, platform admin | can_manage; busy while submitting
- CP-33 | Write-result note beside the default form | WorkspacePeople.tsx:RolesPage (Said at:'default') | workspace admin, platform admin | after "Set default" succeeds/fails
- CP-34 | Card "Give a person their own role" | WorkspacePeople.tsx:RolesPage | workspace admin, platform admin | can_manage, group workspace
- CP-35 | Info paragraph: "Google owns who is in the workspace…" | WorkspacePeople.tsx:PersonForm | workspace admin, platform admin | always on form
- CP-36 | Email input field (required, type=email, maxLength 254) | WorkspacePeople.tsx:PersonForm | workspace admin, platform admin | form open
- CP-37 | Role segmented control (viewer/member/admin) | WorkspacePeople.tsx:PersonForm | workspace admin, platform admin | form open
- CP-38 | Reason input field (optional, maxLength 280) | WorkspacePeople.tsx:PersonForm | workspace admin, platform admin | form open
- CP-39 | "Set role" submit button (busy/disabled) | WorkspacePeople.tsx:PersonForm | workspace admin, platform admin | form open/submitting
- CP-40 | Write-result note beside the person form | WorkspacePeople.tsx:RolesPage (Said at:'person') | workspace admin, platform admin | after "Set role" succeeds/fails
- CP-41 | Card "Role events" | WorkspacePeople.tsx:RolesPage (Events) | member, viewer, workspace admin, platform admin | always
- CP-42 | Role/platform-admin event list, one sentence per event; empty-state "No role has been given or changed here…" | WorkspacePeople.tsx:Events | member, viewer, workspace admin, platform admin | list when events exist, empty-state otherwise
- CP-43 | Card "Join link" | WorkspacePeople.tsx:JoinLinkCard | workspace admin, platform admin | group workspace, can_manage
- CP-44 | Join-link status paragraph (on/off, who turned it on, when) | WorkspacePeople.tsx:JoinLinkCard | workspace admin, platform admin | on/off state
- CP-45 | Hint "Nobody gets in by holding the link: each request waits in Admin › People and roles…" | WorkspacePeople.tsx:JoinLinkCard | workspace admin, platform admin | always
- CP-46 | One-time reveal banner + read-only link field + Copy button | WorkspacePeople.tsx:JoinLinkCard | workspace admin, platform admin | right after turning on/rotating; never shown again
- CP-47 | Write-result note after Turn on/off/rotate | WorkspacePeople.tsx:JoinLinkCard | workspace admin, platform admin | after action succeeds/fails
- CP-48 | "Make a new link" button (rotate) | WorkspacePeople.tsx:JoinLinkCard | workspace admin, platform admin | link currently on
- CP-49 | "Turn the link off" button (danger) | WorkspacePeople.tsx:JoinLinkCard | workspace admin, platform admin | link currently on
- CP-50 | "Turn the link on" button (primary) | WorkspacePeople.tsx:JoinLinkCard | workspace admin, platform admin | link currently off
- CP-51 | Card "Joined by request" | WorkspacePeople.tsx:JoinedByRequestCard | workspace admin, platform admin | group workspace, can_manage
- CP-52 | Notice: "Approving a request does not let anyone in yet: platform memberships are off…" | WorkspacePeople.tsx:JoinedByRequestCard | workspace admin, platform admin | platform_memberships is off
- CP-53 | Write-result note ("membership by request is ended…") | WorkspacePeople.tsx:JoinedByRequestCard | workspace admin, platform admin | after "Remove" succeeds/fails
- CP-54 | Empty-state "Nobody holds a membership of {name} by request…" | WorkspacePeople.tsx:JoinedByRequestCard | workspace admin, platform admin | members.length===0
- CP-55 | Table columns: Person, Let in by, When, Access, Actions | WorkspacePeople.tsx:JoinedByRequestCard | workspace admin, platform admin | members.length>0
- CP-56 | "Remove" button per row | WorkspacePeople.tsx:JoinedByRequestCard | workspace admin, platform admin | row present
- CP-57 | Card "Members' GitHub connections" (second tab's content) | WorkspacePeople.tsx:Members | platform admin | `?view=members`
- CP-58 | Admin-gate panel (FailedPanel) drawn from a 403/admin_required refusal | WorkspacePeople.tsx:Members | member, viewer, workspace admin (non-platform-admin) | `/v1/access/members` refuses with 403
- CP-59 | Refresh control on the Members card | WorkspacePeople.tsx:Members | platform admin | shown only when not gated
- CP-60 | Loading/error region for `GET /v1/access/members` with retry | WorkspacePeople.tsx:Members | platform admin | while loading/on error
- CP-61 | Empty-state "No member has started" / "Nobody has connected GitHub or chosen a repository yet." | WorkspacePeople.tsx:Members | platform admin | members.length===0
- CP-62 | Lead paragraph describing the table | WorkspacePeople.tsx:Members | platform admin | members.length>0
- CP-63 | Column headers: Member, Connection, Owners, Repositories (hidden on phone) | WorkspacePeople.tsx:Members | platform admin | members.length>0
- CP-64 | Per-member row: name, GitHub handle, connection mark, owners list, repository/write counts | WorkspacePeople.tsx:MemberRow | platform admin | one row per member
- CP-65 | Hint "Each member connects as themselves. An admin sees states and names, never a value…" | WorkspacePeople.tsx:Members | platform admin | always

## Workspace Settings (WS)

- WS-01 | Page header "Workspace Settings" with a refresh control re-reading profile and workspace-roles | WorkspaceSettings.tsx:WorkspaceSettingsScreen | member, workspace admin, platform admin | always
- WS-02 | Config tabs: Settings / Workspace setup / Add a new workspace | WorkspaceSettings.tsx:WorkspaceSettingsScreen | member/workspace admin see Settings+Workspace setup; platform admin additionally sees Add a new workspace | always
- WS-03 | Lead banner "Setup's next step" + "Open Workspace setup" button | WorkspaceSettings.tsx:SetupLead | member, workspace admin, platform admin | checklist incomplete
- WS-04 | Lead banner once complete: "Nothing here needs you" / "Setup's next step is on X" + button / "N of M setup steps not measured" | WorkspaceSettings.tsx:StripLead | member, workspace admin, platform admin | checklist complete
- WS-05 | "This workspace" card: Name / Id / Kind / Your role; empty message "The profile read names no acting workspace." | WorkspaceSettings.tsx:ThisWorkspace | member, workspace admin, platform admin | always once profile ok; empty-state otherwise
- WS-06 | "This workspace" card: Default role row + "People and roles" link | WorkspaceSettings.tsx:ThisWorkspace / DefaultRole | member, workspace admin, platform admin | group workspace only
- WS-07 | "Rename" button | WorkspaceSettings.tsx:ThisWorkspace | workspace admin, platform admin | group workspace, effective role admin
- WS-08 | Rename form: "New name" input, hint, Save/Cancel, refusal message | WorkspaceSettings.tsx:RenameForm | workspace admin, platform admin | after Rename clicked; disabled while saving
- WS-09 | "Code intelligence" card lead paragraph + refresh | CodeIntelligence.tsx:CodeIntelligenceCard | member, workspace admin, platform admin | always
- WS-10 | "Send code to Jev" switch + "Never sent" list + Set-by line + Saved/error messages | CodeIntelligence.tsx:JevSwitch | member (read-only); workspace owner/admin or platform admin (can edit) | disabled for non-editors; busy while saving
- WS-11 | "Visual QA" card lead paragraph + refresh | WorkspaceSettings.tsx:QaSettingsCard | member, workspace admin, platform admin | always
- WS-12 | QA flows-per-step read-only figure | WorkspaceSettings.tsx:QaFlowsPerStep | member (can_edit=false) | not editable
- WS-13 | QA flows-per-step form (number input, bounds hint, validation, Save, Saved/refusal) | WorkspaceSettings.tsx:QaFlowsPerStep | workspace owner/admin, platform admin | editable; validation error out of bounds
- WS-14 | "Per-agent budget" card lead paragraph + refresh | WorkspaceSettings.tsx:WorkspaceBudgetCard | member, workspace admin, platform admin | always
- WS-15 | Budget read-only view (Budget, Platform ceiling, Set by, restriction note) | WorkspaceSettings.tsx:BudgetReadOnly | member (can_edit=false) | not editable
- WS-16 | Budget form: Off/Dollars/Tokens radiogroup, amount input, hint, invalid/over-max/over-ceiling warnings, Save, Set-by, Saved/refusal | WorkspaceSettings.tsx:BudgetForm | workspace owner/admin, platform admin | editable; disabled while saving

## Workspace setup (WSU)

- WSU-01 | Page header "Workspace Settings" with refresh control (profile only) | WorkspaceSettings.tsx:WorkspaceSettingsScreen | member, workspace admin, platform admin | always
- WSU-02 | Config tabs (same as WS-02) | WorkspaceSettings.tsx:WorkspaceSettingsScreen | member, workspace admin, platform admin | always
- WSU-03 | Lead banner "Step N of M · next" + "Open GitHub access" button | WorkspaceSettings.tsx:SetupLead | member, workspace admin, platform admin | checklist incomplete
- WSU-04 | Lead banner once complete: "Setup is complete" / strip-vs-checklist mismatch note + "Open X" button | WorkspaceSettings.tsx:StripLead | member, workspace admin, platform admin | checklist complete or strip has a step left
- WSU-05 | Intro paragraph (connect GitHub / install App / enable orgs / choose repos / verify access) | Onboarding.tsx:OnboardingScreen | member, workspace admin, platform admin | always
- WSU-06 | Checklist card loading/error/empty states ("The checklist answered with no steps") | Onboarding.tsx:OnboardingScreen | member, workspace admin, platform admin | while loading/on error/when empty
- WSU-07 | Checklist card header: title, "Show on Overview" button, refresh, footer "Derived x ago for user in workspace Y" | Onboarding.tsx:OnboardingScreen | member, workspace admin, platform admin | "Show on Overview" only when dismissed and not complete
- WSU-08 | Count-summary sentence + step bar | Onboarding.tsx:StepBar | member, workspace admin, platform admin | always once loaded
- WSU-09 | Checklist rows: title, state word, "next" chip, evidence line, checked-at time | Onboarding.tsx:Checklist | member, workspace admin, platform admin | always
- WSU-10 | Step issue/error copy message(s), with "Open on GitHub" link | Onboarding.tsx:IssueCopy | member, workspace admin, platform admin | when a step carries an issue/code
- WSU-11 | "Last probe did not complete…" note on Connect GitHub step | Onboarding.tsx:Checklist | member, workspace admin, platform admin | last probe incomplete
- WSU-12 | "Connecting authorised the App…" help note on Install the App step | Onboarding.tsx:Checklist | member, workspace admin, platform admin | step not done, not waiting on another
- WSU-13 | Per-step action buttons: Connect GitHub/Reconnect, Re-check, Install the App, Enable orgs, Choose repositories, Verify access, Start an agent | Onboarding.tsx:StepAction | member, workspace admin, platform admin | varies by step state; hidden once done
- WSU-14 | Workspace step, "none" state: helper + "Request my workspace" button | OnboardingWorkspace.tsx:WorkspaceStepBody | member, workspace admin, platform admin | no workspace requested
- WSU-15 | Workspace step, "requested" state: request line, elapsed time, migration/waiting note | OnboardingWorkspace.tsx:WorkspaceStepBody | member, workspace admin, platform admin | requested
- WSU-16 | Workspace step, approved/applying/needs_owner state: ApprovedWaiting/"Setting up…" + elapsed time + notes + JobSteps timeline | OnboardingWorkspace.tsx:WorkspaceStepBody / ApprovedWaiting / JobSteps | member, workspace admin, platform admin | approved/applying/needs_owner
- WSU-17 | Workspace step, "ready" state: "Workspace ready (id)" | OnboardingWorkspace.tsx:WorkspaceStepBody | member, workspace admin, platform admin | ready
- WSU-18 | Workspace step, "denied" state: reason quote + "Request again" button or wait-until text | OnboardingWorkspace.tsx:WorkspaceStepBody | member, workspace admin, platform admin | denied
- WSU-19 | Workspace step, "failed" state: "Setting up your workspace stopped" + failure copy | OnboardingWorkspace.tsx:WorkspaceStepBody | member, workspace admin, platform admin | failed
- WSU-20 | Workspace step: refusal message on request failure + "Submissions are not held for this step right now" | OnboardingWorkspace.tsx:WorkspaceStepBody | member, workspace admin, platform admin | write failure / optional step not ready
- WSU-21 | Claude-account step: counts line, helper text, "Add key" link, "Request a loan" button / "Loan requested", refusal, optional-step note | OnboardingWorkspace.tsx:ClaudeAccountStepBody | member, workspace admin, platform admin | step not done
- WSU-22 | "After the checklist" card: Secrets / People invited rows + Set/Invite buttons | WorkspaceSettings.tsx:AfterChecklist | member, workspace admin, platform admin | People invited row absent for personal workspace
- WSU-23 | "On Overview" card status paragraph (complete/hidden/shown/default) | WorkspaceSettings.tsx:OnOverview | member, workspace admin, platform admin | always
- WSU-24 | "On Overview" card "Show on Overview" button + refusal message | WorkspaceSettings.tsx:OnOverview | member, workspace admin, platform admin | checklist hidden/dismissed
- WSU-25 | GitHub section header: "Checked x ago" + "↻ Re-check" | PermissionCards.tsx:GitHubPermissions | member, workspace admin, platform admin | always
- WSU-26 | GitHub section loading/error states | PermissionCards.tsx:GitHubPermissions | member, workspace admin, platform admin | while loading/on error
- WSU-27 | GitHub section lead paragraph ("GitHub does not let SwarmCloud grant permissions itself…") | PermissionCards.tsx:GitHubPermissions | member, workspace admin, platform admin | once loaded
- WSU-28 | App permissions card: Details/Hide toggle, count chip, summary, gap lines, grants table | PermissionCards.tsx:AppCard / PermCard | member, workspace admin, platform admin | always
- WSU-29 | App permissions card "Add to App" fix link | PermissionCards.tsx:AppCard | platform admin | gaps exist and server grants settings URL
- WSU-30 | App permissions card hint "A platform admin adds these to the App…" | PermissionCards.tsx:AppCard | member, workspace admin (non-platform-admin) | gaps exist
- WSU-31 | App permissions card "not read"/"The App was not read." error state | PermissionCards.tsx:AppCard | member, workspace admin, platform admin | read error
- WSU-32 | Org permissions cards (one per installation): toggle, count chip, summary, gap/pending lines, grants table, "Also granted" details, "Covers: N repositories", "Manage on GitHub" link, checked-at line | PermissionCards.tsx:OrgCard / OrgDetail | member, workspace admin, platform admin | one per installation
- WSU-33 | Org permissions card "Review on GitHub" fix link + "Only an owner of X can accept" hint | PermissionCards.tsx:OrgCard | member, workspace admin, platform admin | gaps exist
- WSU-34 | "No connected org" empty state | PermissionCards.tsx:GitHubPermissions | member, workspace admin, platform admin | no org reached through the App

## Add a new workspace (NW)

- NW-01 | Page header + Config tabs (page itself platform-admin gated) | WorkspaceSettings.tsx:WorkspaceSettingsScreen | platform admin | always
- NW-02 | "Reading whether you are a platform admin…" loading message | WorkspaceSettings.tsx:WorkspaceSettingsScreen | member, workspace admin | while profile loading
- NW-03 | "Not available to you" / "Adding a workspace is for platform admins" lead + "Open Workspace setup" button | WorkspaceSettings.tsx:WorkspaceSettingsScreen | member, workspace admin (non-platform-admin) | profile loaded, viewer not platform admin
- NW-04 | "Add a new workspace" lead banner explaining the approval flow | WorkspaceSettings.tsx:AddWorkspace | platform admin | always
- NW-05 | Workspace-kind tabs: Personal / Group / Service | NewWorkspace.tsx:NewWorkspaceForm | platform admin | always
- NW-06 | Address field (email input; label/placeholder vary by kind) | NewWorkspace.tsx:NewWorkspaceForm | platform admin | disabled while posting
- NW-07 | Display name (optional) field | NewWorkspace.tsx:NewWorkspaceForm | platform admin | disabled while posting
- NW-08 | First admin's email address (optional) field + hint | NewWorkspace.tsx:NewWorkspaceForm | platform admin | Group kind only
- NW-09 | "What this gets" paragraph, text differs per kind | NewWorkspace.tsx:Gets | platform admin | always
- NW-10 | Refusal error message (code + sentence) | NewWorkspace.tsx:Refused | platform admin | submit failure
- NW-11 | "Create workspace"/"Creating…" submit button | NewWorkspace.tsx:NewWorkspaceForm | platform admin | disabled while posting or address empty
- NW-12 | First-admin outcome message (granted & member / granted & pending / not granted) | NewWorkspace.tsx:FirstAdminOutcome | platform admin | after filing a Group request with a first admin
- NW-13 | Progress: record id + name/principal line | NewWorkspace.tsx:Progress | platform admin | after filing
- NW-14 | Progress, "requested" state line + held/migration note | NewWorkspace.tsx:Progress | platform admin | state=requested
- NW-15 | Progress, approved/applying state | NewWorkspace.tsx:Progress | platform admin | approved/applying
- NW-16 | Progress, "needs_owner" state | NewWorkspace.tsx:Progress | platform admin | needs_owner
- NW-17 | Progress, "ready" state ("Workspace ready") | NewWorkspace.tsx:Progress | platform admin | ready
- NW-18 | Progress, "denied" state (reason quote) | NewWorkspace.tsx:Progress | platform admin | denied
- NW-19 | Progress, "failed" state ("Setting up stopped" + failure copy) | NewWorkspace.tsx:Progress | platform admin | failed
- NW-20 | Progress, unknown-state Dash fallback | NewWorkspace.tsx:Progress | platform admin | unrecognized state
- NW-21 | WorkspaceTimeline: ordered job-step list with tone, label, state word, timestamp, code | NewWorkspace.tsx:WorkspaceTimeline | platform admin | after filing, polled every 5s until settled
- NW-22 | "Add another" button | WorkspaceSettings.tsx:AddWorkspace | platform admin | after a request is filed
- NW-23 | "What creating it does" card: ordered list of job steps, "a service workspace only" annotations | WorkspaceSettings.tsx:AddWorkspace | platform admin | always
- NW-24 | "What creating it does" card explanatory paragraph (POST /v1/admin/workspaces route) | WorkspaceSettings.tsx:AddWorkspace | platform admin | always
- NW-25 | "Filed <id>. Admin › Tenants lists it under Being created…" message | WorkspaceSettings.tsx:AddWorkspace | platform admin | after filing

## Admin › People and roles (AP)

- AP-01 | Page title "People and roles" | People.tsx:PeopleScreen | workspace admin, platform admin | always
- AP-02 | Subtitle (platform-wide wording vs scoped wording) | People.tsx:PeopleScreen | workspace admin, platform admin | text varies by scope
- AP-03 | Refresh control beside the title | People.tsx:PeopleScreen | workspace admin, platform admin | always
- AP-04 | Admin-gate region (403 on `GET /v1/admin/people` draws as a plain error/retry region) | People.tsx:PeopleScreen | n/a (member refused) | on error/403
- AP-05 | Full-platform lead: "Nobody is waiting for an admin" | People.tsx:PeopleLead | platform admin | no open request, joins read ok
- AP-06 | Full-platform lead: "N requests are waiting for an admin", oldest named, "Review the oldest" button | People.tsx:PeopleLead | platform admin | ≥1 open request
- AP-07 | Full-platform lead: unread state ("who is waiting to join… could not be read") | People.tsx:PeopleLead | platform admin | join reads failed, nothing else waits
- AP-08 | Workspace-admin lead ("Your workspaces"): "Nobody is waiting for you in X" | People.tsx:ScopedBody | workspace admin (non-platform) | no open join request in their workspaces
- AP-09 | Workspace-admin lead: "N people are waiting to join X" + "Review it/the N" button | People.tsx:ScopedBody | workspace admin (non-platform) | ≥1 open join request
- AP-10 | Workspace-admin lead: unread state | People.tsx:ScopedBody | workspace admin (non-platform) | join read failed
- AP-11 | "Workspace building is off" banner | People.tsx:BuildingOff | platform admin | provisioning.available === false
- AP-12 | "The requests to join could not be read" warning banner | People.tsx:JoinsUnread | workspace admin, platform admin | join_requests_error present
- AP-13 | "Waiting for you" card: amber, one row per open request, oldest-first, count+age chip | People.tsx:WaitingCard | platform admin | total waiting > 0
- AP-14 | Waiting-card empty line "Nothing else is waiting." | People.tsx:WaitingCard | platform admin | total becomes 0 but a decision outcome is still shown
- AP-15 | Workspace-request row: avatar, name, "asked <time>", workspace id, limit chips / default-ceiling note | People.tsx:WaitingCard, LimitChips, Requested | platform admin | row kind = workspace
- AP-16 | Approve control (one click) + busy state + outcome line | People.tsx:Approve | platform admin | workspace-request row
- AP-17 | Deny control: inline reason textarea (required, max 500), Cancel/Deny | People.tsx:Deny | platform admin | workspace-request row
- AP-18 | Loan-request row: avatar, name, "Borrow a Claude account · asked <time>", pool size sentence | People.tsx:WaitingCard, PoolLine | platform admin | row kind = loan
- AP-19 | Lend control: account picker/confirm dialog | People.tsx:LoanPicker | platform admin | loan-request row, pool non-empty
- AP-20 | Decline control: optional reason input, Cancel/Decline | People.tsx:DeclineLoan | platform admin | loan-request row
- AP-21 | Join-request row: avatar, requester name, "to join <workspace> · asked <time>", quoted message | People.tsx:JoinReq | platform admin, workspace admin | open join request
- AP-22 | Join Approve control: role select + Approve button; success/"waiting for group change" text | People.tsx:JoinApprove | platform admin, workspace admin | open join request
- AP-23 | Join Deny control: required reason textarea, Cancel/Deny | People.tsx:JoinDeny | platform admin, workspace admin | open join request
- AP-24 | Workspace-admin "Waiting for you" card (join requests only) | People.tsx:JoinWaitingCard | workspace admin (non-platform) | ≥1 open join request
- AP-25 | "The workspaces' roles could not be read" banner | People.tsx:ColumnsUnread | platform admin | workspaces_error present
- AP-26 | "People and their roles" card head: Workspace filter, count chip, "Grant admin" button | People.tsx:PeopleBody | platform admin | full view
- AP-27 | Workspace filter (Segmented): "Every workspace" + one per column | People.tsx:WorkspaceFilter | platform admin | ≥2 workspace columns
- AP-28 | Count chip ("N of M people in X" / "N people · M pending") | People.tsx:PeopleBody | platform admin | always
- AP-29 | "Grant admin" button opening GrantDialog | People.tsx:PeopleBody, GrantDialog | platform admin | always
- AP-30 | "Find a person" search box + match-count line | People.tsx:FindPerson | workspace admin, platform admin | rows.length > 0
- AP-31 | "No 'joined by request' mark…" notice | People.tsx:MembersUnread | workspace admin, platform admin | one or more member-list reads failed
- AP-32 | Empty state "no one has signed in" | People.tsx:PeopleBody | platform admin | doc.people empty
- AP-33 | "Nobody has been seen in X yet, and nobody holds a role there." | People.tsx:ScopedBody | workspace admin (non-platform) | scoped doc.people empty
- AP-34 | NobodyShown filtered-empty message | People.tsx:NobodyShown | workspace admin, platform admin | filter/search narrows to zero rows
- AP-35 | Full table columns: Person, Platform role, per-workspace role columns, Own workspace, Claude account, Ceiling, Last active, Manage | People.tsx:PeopleBody, Row | platform admin | rows present
- AP-36 | Scoped table columns: Person, one per administered workspace, Last active | People.tsx:ScopedBody | workspace admin (non-platform) | rows present
- AP-37 | Person cell: avatar, name/email, GitHub mark, legacy "platform owner" chip | People.tsx:PersonCell, Avatar, GitHubMark | workspace admin, platform admin | always
- AP-38 | Platform role cell | People.tsx:PlatformRoleCell | platform admin | full view, grid API
- AP-39 | Role cell (grid): role word, admin badge, none/requested/dash states, joined-by-request mark | People.tsx:RoleCell, JoinedMark | workspace admin, platform admin | grid API
- AP-40 | Own-workspace cell: state tone+word, workspace id, failure note, denial reason, live job-step progress | People.tsx:WorkspaceCell | workspace admin, platform admin | always
- AP-41 | Claude-account cell: source summary, multi-loan disclosure, borrow/decline chips | People.tsx:ClaudeCell, ClaudeSummary | platform admin | full view, non-phone
- AP-42 | Ceiling cell: numeric value or dash | People.tsx:Row | platform admin | full view, non-phone
- AP-43 | Last-active cell | People.tsx:Row, ScopedBody | workspace admin, platform admin | always
- AP-44 | "Manage" button opening the person's drawer | People.tsx:Row | platform admin | full view only
- AP-45 | Manage drawer: Facts section | People.tsx:Drawer | platform admin | drawer open
- AP-46 | Manage drawer: Ceiling section (stepper, numeric field, Save, confirm dialog) | People.tsx:Drawer, Ceiling | platform admin | drawer open, person has workspace record
- AP-47 | Manage drawer: Claude account section | People.tsx:Drawer, ClaudeSection | platform admin | drawer open
- AP-48 | Manage drawer: Admin switch (on/off, confirm dialog) | People.tsx:Drawer, AdminSwitch | platform admin | drawer open
- AP-49 | Manage drawer: "Roles in workspaces" section | WorkspaceRolesAdmin.tsx:PersonWorkspaceRoles | platform admin | drawer open
- AP-50 | Drawer chrome: focus trap, Escape/scrim/Close dismissal | People.tsx:Drawer | platform admin | drawer open
- AP-51 | "Workspace roles" card: per-workspace default role, named admins, override list, "Name an admin"/"Set default" forms, pagination | WorkspaceRolesAdmin.tsx:WorkspaceRolesCard | platform admin | full view only
- AP-52 | Workspace-roles card list states (loading/error/empty) | WorkspaceRolesAdmin.tsx:ListPage | platform admin | loading/error/empty
- AP-53 | "Admins" side card: admin list, inline "Grant admin" form, confirm dialogs | People.tsx:Admins, AdminList | platform admin | full view only
- AP-54 | "Recent admin actions" side card (folded, "N this week") | People.tsx:Audit | platform admin | full view only
- AP-55 | Audit body: empty state, housekeeping fold/unfold, per-run list lines | People.tsx:Audit, auditRuns | platform admin | card expanded
- AP-56 | Outcome/error line after any write (SaidLine) | People.tsx:SaidLine | workspace admin, platform admin | after any action

## Workspaces you admin (WYA)

- WYA-01 | Page title "Workspaces you admin" | WorkspacesYouAdmin.tsx:WorkspacesYouAdminScreen | member (workspace admin), platform admin | always
- WYA-02 | Refresh control beside the title | WorkspacesYouAdmin.tsx:WorkspacesYouAdminScreen | member, platform admin | always
- WYA-03 | Subtitle describing the page | WorkspacesYouAdmin.tsx:WorkspacesYouAdminScreen | member, platform admin | always
- WYA-04 | Error/retry region for `GET /v1/tenants/mine` | WorkspacesYouAdmin.tsx:WorkspacesYouAdminScreen | member, platform admin | on read error
- WYA-05 | Empty state "You administer no group workspace" | WorkspacesYouAdmin.tsx:NoneToAdmin | member, platform admin | administers no group workspace
- WYA-06 | "Behind" lead card naming the workspace furthest behind in setup + "Open <X>'s Config" button | WorkspacesYouAdmin.tsx:AdminList | member (workspace admin), platform admin | ≥1 administered workspace with readable setup progress
- WYA-07 | List of administered workspaces (display name + tenant id) | WorkspacesYouAdmin.tsx:AdminList | member, platform admin | ≥1 administered workspace
- WYA-08 | "admin as a platform admin" marker | WorkspacesYouAdmin.tsx:AdminList | platform admin (de-facto, not named) | role source is platform_admin
- WYA-09 | Per-workspace links: Workspace Settings, MCP servers, People and roles, Secrets | WorkspacesYouAdmin.tsx:AdminList | member, platform admin | per row, always
- WYA-10 | Per-workspace setup progress strip (segments/count/next-step), "Reading setup…"/"Setup not read" states | WorkspacesYouAdmin.tsx:AdminList, SetupProgress | member, platform admin | per row, while/if setup read is in flight or fails
- WYA-11 | Setup-progress "next" click opens that step's Config page | WorkspacesYouAdmin.tsx:AdminList, SetupProgress | member, platform admin | per row, when a next step exists
- WYA-12 | Design note: personal workspaces never listed; platform admin's general-reach-only workspaces excluded (behavior, not a drawn element) | WorkspacesYouAdmin.tsx (header comment) | member, platform admin | n/a

## Nav (NAV) — Spine.tsx / ConfigSetup.tsx

- NAV-01 | Overview's "On this page" panel lists 8 jump links in page order (Needs a look, Waiting/working/done, Running now, Waiting and why, Recent token cost, Headroom, Recent failures, Pools); a click replaceState's a `#id` hash and scrolls/closes the phone drawer | Spine.tsx:OVERVIEW_JUMPS/PanelPages (jumpTo) | all roles | always while section is Overview
- NAV-02 | 4 of the 8 Overview jump links (Needs a look, Running now, Waiting and why, Recent failures) carry a live count via `OverviewCounts`/`publishOverviewCounts` so the panel and the page's cards read the same numbers | Spine.tsx:OverviewCounts/CountMark | all roles | no mark while the read is in flight; dash when asked-for but null; number otherwise
- NAV-03 | Config section's panel lists 6 rows in fixed order: GitHub access, Repositories, Secrets, MCP servers, People and roles, Workspace Settings; "People and roles" is dropped in a personal workspace | Spine.tsx:PANEL_PAGES.config / configPagesFor | member, workspace admin | People and roles hidden when personal===true
- NAV-04 | Each Config row (except Workspace Settings) draws a setup badge (done tick, count, "N to set", "via group", or unknown dash) fed by the onboarding read | Spine.tsx:PanelRows / ConfigSetup.tsx:configBadges/ConfigBadgeMark | member, workspace admin | varies per step state; Workspace Settings carries no badge
- NAV-05 | Admin section's panel lists up to 9 rows: Pool limits, Runner sizes, Tenants, Platform counts, Platform secrets, Setup, Health, People and roles, Workspaces you admin | Spine.tsx:PANEL_PAGES.admin / adminPagesFor | platform admin sees all 9; workspace admin (non-platform) sees only People and roles + Workspaces you admin; others see no Admin item at all | gated by role read
- NAV-06 | Admin panel drawn in two groups ("People and workspaces"/"Your workspaces" first, then "Platform"); a group with zero rows is not rendered | Spine.tsx:adminGroups/ADMIN_WORKSPACE_KEYS | platform admin sees both groups; workspace admin sees only the first | heading text depends on whether any Platform row exists
- NAV-07 | Admin rows carry status badges from reads already made ("Workspaces you admin" = admin'd count, "People and roles" = admin_pending/joinsToDecide, "Pool limits" = "N near ceiling", "Tenants" = "N waiting", "Platform secrets" = ******** count) | Spine.tsx:adminPanelCounts | role-scoped per bullet | no badge while the underlying read is incomplete or inapplicable
- NAV-08 | "In Automate" group inside the Admin panel links to Schedules and Approvals; a platform admin's Schedules link opens the All-workspaces scope labelled "all workspaces" | Spine.tsx:inAutomateLinks | platform admin, workspace admin | Approvals link shows a CountMark; "all workspaces" sub-label only for platform admin
- NAV-09 | Capacity › Accounts' "Provider quota" child row carries a small "admin" pill rather than being hidden | Spine.tsx:PANEL_PAGES.capacity, PanelPage.admin | all roles see the row/pill; the screen behind it refuses non-admins | always shown when admin===true
- NAV-10 | SetupStrip renders across the top of every Config page (mounted once by App.tsx's ConfigPage): GitHub connected → Repositories chosen → Secrets set → People invited (3 steps on a personal workspace); folds to one line when every step is done | ConfigSetup.tsx:SetupStrip; App.tsx:ConfigPage | member, workspace admin | folded when done===of and of>0
- NAV-11 | Compact SetupMeter (segmented bar, "Setup N of M steps") drawn in the Config panel itself, fed by the same `useSetupSteps` read as the strip | ConfigSetup.tsx:SetupMeter | member, workspace admin | always while Config panel is open; next-todo step marked is-next

## Home / App.tsx navigation test

- HOME-01 | The root address `/` resolves to address `overview/now`, which `addressToPath`'s FIXED table maps to `/overview`, rendering OverviewScreen; an unrecognized address also falls back to `/overview` — Overview is both the literal home route and the universal fallback | App.tsx / paths.ts (pathToAddress, addressToPath FIXED) | all roles | always

The comment block at the top of App.tsx ("THE NAVIGATION, AND WHY IT IS AS FEW THINGS AS IT IS") explains why the console collapsed to five sections (Overview, Work, Automate, Capacity, Admin) named after objects rather than questions, and states that every tab's label must equal the heading of the screen it opens — a reader landing on a different heading than the one they clicked cannot tell a rename from a misnavigation. That rule is enforced by a test that parses `SECTIONS` and the `SectionBody` switch statement directly out of the shipped App.tsx source (not a hand-maintained fixture), resolves every route's component to the heading it actually renders, and asserts the two strings agree for all of the console's routes, plus a second test guarding the parser itself so a broken regex cannot silently empty the route table and pass vacuously.

### Path constants found

- `overview/now` (FIXED entry) = `/overview` — also the target of root `/` via `pathToAddress`
- `config/people` (FIXED entry) = `/config/people`
- `config/workspace` (FIXED entry) = `/config/workspace` (Workspace Settings' default "settings" part)
- `WORKSPACE_SETUP_ADDRESS` = `config/workspace?part=setup` → `WORKSPACE_SETUP_PATH` = `/config/workspace/setup`
- `WORKSPACE_NEW_ADDRESS` = `config/workspace?part=new` (no dedicated path constant; resolves via the generic FIXED entry + query)
- `admin/people` (FIXED entry) = `/admin/people`
- `admin/workspaces` (FIXED entry) = `/admin/workspaces`; tab key `WORKSPACES_YOU_ADMIN_TAB` = `workspaces`

## Reads per page

- Overview: loadCapacity (GET /v1/capacity), loadTasks (GET /v1/tasks), loadLeases (GET /v1/admin/leases), loadProviders (GET /v1/providers), loadAccountPool (GET /v1/accounts), loadWorkflows (GET /v1/workflows), loadStats (GET /v1/stats), loadSpend (aggregates per-task loadAttempts, GET /v1/tasks/{id}/attempts), loadTasksInState (GET /v1/tasks with state= query), loadLiveSession (GET /v1/tasks/{id}/live/session), loadTask (GET /v1/tasks/{id}), loadOnboarding (GET /v1/onboarding), dismissOnboarding (POST /v1/onboarding/dismiss), loadApprovals (GET /v1/approvals). githubAppName is a pure helper, not a network call.
- Config People and roles: loadMe (GET /v1/tenants/me), loadWorkspaceRoles (GET /v1/workspace/roles), setWorkspacePersonRole (PUT /v1/workspace/roles/person), clearWorkspacePersonRole (DELETE /v1/workspace/roles/person), setWorkspaceDefaultRole (PUT /v1/workspace/roles/default), loadJoinLink (GET /v1/workspace/join-link), setJoinLink (PUT /v1/workspace/join-link), loadJoinedMembers (GET /v1/workspace/members), removeJoinedMember (DELETE /v1/workspace/members/person), loadAccessMembers (GET /v1/access/members).
- Workspace Settings: loadMe (GET /v1/tenants/me), loadProfile (GET /v1/profile), loadWorkspaceRoles (GET /v1/workspace/roles), renameWorkspace (PUT /v1/workspaces/{tenant_id}/name), loadQaSettings (GET /v1/qa-settings), putQaSettings (PUT /v1/workspaces/{tenant_id}/qa-settings), loadWorkspaceBudget (GET /v1/budget), putWorkspaceBudget (PUT /v1/workspaces/{tenant_id}/budget), loadCodeIntelligence (GET /v1/code-intelligence), loadJudgment (GET /v1/judgment), putCodeIntelligence (PUT /v1/workspaces/{tenant_id}/code-intelligence). budgetAmount, budgetLimitsWords, budgetRefusalWords and parseBudgetAmount are pure formatting helpers, not reads.
- Workspace setup: loadProfile (GET /v1/profile), dismissOnboarding (POST /v1/onboarding/dismiss), loadOnboarding (GET /v1/onboarding), loadWorkspace (GET /v1/workspace), requestWorkspace (POST /v1/workspace), requestLoan (POST /v1/workspace/loan-request), loadForgePermissions (GET /v1/forge/permissions), loadMe (GET /v1/tenants/me). githubAppName is a pure helper, not a network call.
- Add a new workspace: loadProfile (GET /v1/profile), createAdminWorkspace (POST /v1/admin/workspaces), loadAdminWorkspace (GET /v1/admin/workspaces/{workspace_id}).
- Admin People and roles: loadPeople (GET /v1/admin/people), approveWorkspace (POST /v1/admin/workspaces/{workspace_id}/approve), denyWorkspace (POST /v1/admin/workspaces/{workspace_id}/deny), retryWorkspace (POST /v1/admin/workspaces/{workspace_id}/retry), setWorkspaceCeiling (PUT /v1/admin/workspaces/{workspace_id}/limits), setWorkspaceLoan (PUT /v1/admin/people/{workspace_id}/loan), declineLoanRequest (POST /v1/admin/people/{workspace_id}/loan/decline), grantAdmin (PUT /v1/admin/admins/{email}), removeAdmin (DELETE /v1/admin/admins/{email}), loadAdminPersonRoles (GET /v1/admin/roles/person), loadAdminWorkspaceRoles (GET /v1/admin/workspaces/{tenant_id}/roles), loadAdminWorkspacesRoles (GET /v1/admin/workspaces/roles), setAdminWorkspaceDefaultRole (PUT /v1/admin/workspaces/{tenant_id}/roles/default), setAdminWorkspacePersonRole (PUT /v1/admin/workspaces/{tenant_id}/roles/person), clearAdminWorkspacePersonRole (DELETE /v1/admin/workspaces/{tenant_id}/roles/person).
- Workspaces you admin: loadMyTenants (GET /v1/tenants/mine).
