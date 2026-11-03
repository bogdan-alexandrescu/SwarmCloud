# Work section: feature inventory (today, read from the code)

Every user-visible feature of the console's Work section (Agents: Live / Waiting / Recent lists and one
agent's detail with its live browser; Workflows: the list and one workflow's page), read from
apps/swarm-ui/src on main at 57392c6 (2026-11-02). One line per feature:
`feature — component (file:line) — state/screen it appears in — what it does`.

897 feature lines in 27 sections. Line numbers are as of 57392c6 without the mock-up patch.
Every redesign direction places EVERY line somewhere and maps it in its notes.md.
States used below: list tabs Live / Waiting / Recent; agent states (queued, leased, starting, running,
parked with a park reason, waiting on an owner question, failed, succeeded, cancelled); workflow states
(running, parked, failed, done); panes (Details, Browser, Logs, Changes, Children, Attempts, Artifacts,
Checkpoints); workflow views (Graph, Table, Steps over time, Changes); phone.

## Agents.tsx — AgentsScreen / AgentsBody (the Agents list + rows)

- Three tabs Live/Waiting/Recent — AgentsScreen (Agents.tsx:1086) — Agents list rest — `role="tablist"` strip, each tab a count badge, click or Left/Right/Home/End keys switch tab
- Tab accessible label with count and meaning — AgentsScreen (Agents.tsx:1093) — Agents list rest — aria-label reads "Live 6: holding a pool slot, and costing money" etc., `+` suffix and extra sentence when the count is a lower bound over a capped page
- Landing tab auto-select — AgentsBody (Agents.tsx:200,814) — first load — opens on the first tab (in Live, Waiting, Recent order) that has any row, Live preferred when all are empty; "land once, then stay" afterwards
- Fold/unfold list to 64px strip — AgCollapse (Agents.tsx:2185) — split pane open — « / » button, also bound to the `[` key; only shown while an agent is open
- Search box "Find by name or task id" — AgentsBody (Agents.tsx:1114) — search typed — filters every tab's rows by task id, step id, workflow id, metadata title or computed agent name (`matchesQuery`)
- Scope qualifier beside search — AgentsBody (Agents.tsx:1128) — search typed; Live tab; capped page — shows "all N live" (Live tab, fully read), "showing the N most recent" (phone-width capped page), or "more beyond" (desktop capped page); full sentence in title/aria-label
- Whole-task-id lookup — IdLookup (Agents.tsx:1438) — search typed with a full `task_<20hex>` id not in the loaded page — looks in the loaded page's other tabs first, then `GET /v1/tasks/{id}`; shows found-elsewhere row, loading state, "no agent in this tenant" (404), or "could not look up" (failed read)
- Profile filter dropdown — AgentsBody (Agents.tsx:1146) — filter open — `profile:` select, "all" plus every distinct `runner_profile` among loaded rows
- Recent state segmented filter — AgentsBody (Agents.tsx:1164) — Recent tab — all / failed / cancelled / succeeded buttons with counts (DEAD_LETTERED never offered, nothing writes it)
- Live "All / Browsers" switch — AgentsBody (Agents.tsx:1194) — Live tab, when any browser agent exists — narrows Live to only browser agents (needs-you first, then running, then starting), with a count
- "Watch all browsers" theater link — AgentsBody (Agents.tsx:1230) — Live tab, Browsers view — `⤢` link opens the first (needs-you) browser row's Browser tab in the full-screen theater; hidden in the fixture build
- Browser-unused filter chip — AgentsBody (Agents.tsx:1242) — arrived via `?browser=unused` link (e.g. from Platform counts) — "browser not used" tag with a `×` to clear the filter back to the plain list
- "Failed first" checkbox — AgentsBody (Agents.tsx:1258) — Recent tab, no state filter chosen — sorts failed rows to the top without changing the list's own order otherwise
- "Group by workflow" checkbox — AgentsBody (Agents.tsx:1269) — Waiting or Recent tab — groups rows by `workflow_id`, standalone agents last
- Started/Ended sort toggle — AgentsBody (Agents.tsx:1301) — any tab — cycles list's own order → "Started, newest first" → "Started, oldest first" → back to list's own order; button label and aria-label always say current order and what pressing does next
- Empty state: real zero — AgentsBody (Agents.tsx:1355) — tab has no rows, no search/filter active — per-tab message ("No agent is holding a pool slot", "Nothing is waiting… costs nothing", "Nothing has finished"); Live's empty state alone also shows a "?" HelpCard linking `#help/capacity`
- Empty state: search/filter found nothing in a capped page — AgentsBody (Agents.tsx:1341) — search or Recent-state filter on a capped (>page-limit) load — "partial" mark, explains the filter only covers the loaded rows and offers "load more" or id lookup
- Empty state: browser-unused filter found nothing — AgentsBody (Agents.tsx:1328) — `browser=unused` filter active — zero/partial mark depending on whether page is capped
- Waiting tab grouped by remedy — WaitingGroups (Agents.tsx:1596) — Waiting tab — sections "Needs action", "No room", "Not yet eligible", plus one section per distinct park reason, each with a count (lower-bound `+` when page is capped); empty groups are not drawn
- Group-by-workflow sections — GroupedRows (Agents.tsx:1887) — Waiting/Recent tab with "Group by workflow" on — one section per workflow id, heading = workflow title (linked to `#work/workflows?wf=<id>`) or raw id, a rolled-up state chip, "N here" count, and any reason shared by ≥2 consecutive rows said once above them
- "Load N more" pagination — AgentsBody (Agents.tsx:1403) — any tab with a capped page, except a fully-read Live — button reads the next page via `next_page_token`, "Loading…" while in flight, shows a failure note if the read errors; "N loaded · more beyond" caption
- Per-row fields (compact row) — CompactRow (Agents.tsx:2242) — every list row, phone and desktop — state mark/dot, name (step led by workflow title, own title, or agent-kind+short id), full task id line with copy, elapsed/ended time column (ticking while running), state word, `attempt N of M` (live rows / over-cap rows), budget warning mark, `profile · model`, browser-use mark (used/unused/unknown), owner (from `submitted_by`), run duration for finished rows, "cancelling" tag, "needs you" tag, cancel-span note ("last start to cancel, may include parked time"), why/reason line (red if failed, amber if it needs action), "same reason" note when hidden to avoid repetition
- Row hover/focus card in strip mode — CompactRow (Agents.tsx:2623) — list folded to 64px strip; row hover — portal tooltip beside the row with state, elapsed, name, profile · owner, "click to open · ↑↓ to move"; Up/Down arrow keys move focus between row openers
- Device outline thumbnail — DeviceThumb (Agents.tsx:2678) — Live tab, RUNNING live-browser rows — small phone/tablet/desktop/unknown outline in portrait/landscape, re-read on every list poll
- Row click opens the agent split — CompactRow (Agents.tsx:2420) — row click/Enter — opens `AgentSplit` for that task; row gets `aria-current`; opened-by-keyboard moves focus into the panel
- Open row auto-scroll — AgentsBody (Agents.tsx:983) — deep link or id lookup opens a row off-screen — scrolls the row into view once per agent id
- List summary line — AgentsScreen (Agents.tsx:643) — Agents list rest — "N loaded · M live [· tenant]" under the screen title
- Poll cadence — AgentsScreen/pollInterval (Agents.tsx:233,505) — Live tab holds rows: screen re-reads live+waiting states every 5s; otherwise every 30s; paused while an agent panel is open (always 30s then) or the tab is hidden
- Scrim behind open agent — AgentsScreen (Agents.tsx:693) — split pane open, 760–1099px width — translucent backdrop over the list so the row edges don't look continuous with the panel

## agentlist.ts — the agent list's address (shared by Agents.tsx and App.tsx)

- Three tabs as an address segment — AGENT_TABS (agentlist.ts:25) — URL — `#work/running/<live|waiting|recent>`
- Recent's state filter as an address segment — RECENT_STATES (agentlist.ts:36) — URL, Recent tab — `#work/running/recent/<failed|cancelled|succeeded>`; DEAD_LETTERED deliberately excluded (nothing ever writes it)
- Group-by-workflow toggle in the URL — agentListQuery/listFlags (agentlist.ts:99,108) — URL query — `?group=wf`
- Failed-first toggle in the URL — agentListQuery/listFlags (agentlist.ts:99,108) — URL query — `?first=failed`
- Browser-unused filter link — BROWSER_UNUSED_ADDRESS (agentlist.ts:122) — URL query, linked from Admin › Platform counts "Unused browser time" — `work/running/recent?browser=unused`
- "‹ Back to <tab/state>" label — backLabel (agentlist.ts:177) — agent split pane, phone layout — reads the tab name ("Live"/"Waiting"/"Recent") or, inside Recent's filter, the filter word ("Failed"/"Cancelled"/"Succeeded")
- Short task id — shortTaskId (agentlist.ts:185) — row display — first 8 hex chars after the `task_` prefix
- Agent kind in words — agentKind (agentlist.ts:196) — row/breadcrumb — "browser check" for profile `browser`, else "`<profile>` agent"
- Agent display name — agentName (agentlist.ts:212) — list rows, Overview, Timeline, breadcrumb — step id if part of a workflow, else "`<kind>` · `<short id>`"
- Breadcrumb learns agent names — rememberAgentName/useAgentName (agentlist.ts:225,232) — App.tsx breadcrumb (Head) — once the split reads a task, its name is cached and the crumb re-renders with it instead of the bare id
- Workflow link — workflowHref (agentlist.ts:244) — group headers, row links — `#work/workflows?wf=<id>`

## AgentSplit.tsx — the agent detail split pane

- Underline tab bar with counts — AgentSplit/AgTabsEdge (AgentSplit.tsx:399) — split pane open — Details, Browser (live-browser agents only), Logs, Changes, Children (when any), Attempts, Artifacts, Checkpoints; a tab with no countable figure shows a dash with its reason in the title, never a bare 0
- Fold list via `[` key — AgentSplit (AgentSplit.tsx:222) — split pane open — same toggle as the list's « » button and `listSnap`
- Phone back link — AgentSplit (AgentSplit.tsx:377) — phone width, split pane open — "‹ `<tab/state>`" returns to the list address it was opened from
- Header actions: state pill, name, Copy link, Take control, Stop, ✕ — AgHead (re-exported from agent-split/head.tsx) (AgentSplit.tsx:384) — split pane open, header row — drawn by `AgHead`; ✕ sits after Copy link in the action row
- Hand-off banner — AgHandoff (AgentSplit.tsx:418) — a live-browser agent waiting for a person, any tab but Browser — shows the reason, expected site, time left, "Take control" and "Open in theater"; the Browser tab draws its own instead
- Pod-start problem banner — PodStartProblem (AgentSplit.tsx:429) — agent whose pod failed to start — inline banner over the pane
- Details tab — AgentDetailScreen (AgentSplit.tsx:473) — Details pane — leading card with the last log line, links to Logs
- Browser tab / theater — LiveBrowserPane (AgentSplit.tsx:438) — Browser pane, live-browser agent — live session view; `?theater=1` opens it full-screen; "Open in theater" link from other tabs
- Logs tab — AgentLogs (AgentSplit.tsx:448) — Logs pane — full-height log viewer
- Changes tab — AgentChangesPane (AgentSplit.tsx:455) — Changes pane — diff viewer at full pane height; open file rides the address as `/changes/<path>`; opening it folds the list to its strip
- Children tab — AgChildrenPane (AgentSplit.tsx:461) — Children pane, agent has sub-agents — lists child agents
- Attempts tab — AttemptTimelineScreen (+ RecordingsPanel for live-browser tasks) (AgentSplit.tsx:481-485) — Attempts pane — attempt history timeline; browser recordings panel appended for live-browser tasks
- Checkpoints tab — CheckpointsPane (AgentSplit.tsx:491) — Checkpoints pane — mounted always (hidden via CSS), kept alive across tab switches
- Artifacts tab — ArtifactsScreen (AgentSplit.tsx:488) — Artifacts pane — inputs/outputs/logs/transcript; open output rides the address as `/artifacts/<name>`
- Escape key behavior — AgentSplit/foldInner (AgentSplit.tsx:355,509) — split pane open, Escape pressed — closes the innermost open disclosure/details element first; only closes the whole panel once nothing inner is open
- Loading placeholders per tab — AgentSplit (AgentSplit.tsx:432-467) — tab selected before its read lands — "Reading the agent…" pending mark with a progress bar, one sentence per tab
- Auto-opens Browser or Details on arrival — useOpeningPane (AgentSplit.tsx:298) — agent opened with no pane named — a running live-browser agent opens on Browser, otherwise Details; `/details` in the address pins Details even for a running browser agent
- List waits for panel's first read — agentPanelOpening/agentPanelShown/agentPanelGone (AgentSplit.tsx:212-217) — agent panel opening — the list's own first read is held back until this panel's task+attempts reads answer, to avoid two competing reads racing on open
- Live poll while Browser tab is open — useLiveSession (AgentSplit.tsx:268) — Browser tab or its theater on screen — re-reads the live session and the task repeatedly until the task finishes
- Header's own poll — AgentSplit (AgentSplit.tsx:171-181) — any tab, task not finished — re-reads the task document every `DRAWER_POLL_MS` so the state pill/name/Stop stay current; paused while the document tab is hidden
- Attempt count in the header — AgentSplit/readAttempts (AgentSplit.tsx:187-203,529) — header — re-read only when the task's generation/attempt_count/state changes, not every poll; falls back to the task's own counter on a failed read

## App.tsx — Work section routes and navigation

- Work section nav tabs — SECTIONS (App.tsx:299-330) — rail/breadcrumb — "Agents" (`running`), "Workflows", "Runs", "Timeline", "Start an agent" (`new`), "Submit a workflow" (`new-workflow`), "Submit from a GitHub issue" (`new-issue`)
- Agent list address parsing — fromAddress (App.tsx:1151-1159) — URL `#work/running[/<tab>[/<state>]]` — resolved via `parseAgentList`; an unrecognised tail under `running/` is read as the plain list, never as a task id
- Agent list address writing — canonical (App.tsx:1268-1272) — URL — `work/running/<tab or tab/state>` plus `?group=wf&first=failed&browser=unused` flags, only while no drawer is open
- Agent drawer address parsing — fromAddress (App.tsx:1109-1140) — URL `#work/task/<id>[/<pane>][/artifacts/<name>|/changes/<path>]` — also accepts the legacy `#work/<id>` form; reads `?theater=1` and the `/details` vs `/detail` spelling once on arrival
- Agent drawer address writing — canonical (App.tsx:1253-1257) — URL — `work/task/<id>`, `.../artifacts/<name>`, `.../changes/<path>`, or `.../<pane>`; `detail` pane has no segment
- Workflows/Runs list filters in the URL — fromAddress/canonical (App.tsx:1175-1178,1280) — URL `?<query>` on `/workflows` or `/runs` — kept verbatim as the screen's `view`; `wf=<id>` opens one workflow, `run=<id>` one issue run
- Timeline view/page in the URL — fromAddress/canonical (App.tsx:1162-1165,1275-1278) — URL `/timeline` or `/timeline/outcomes` with `?span=…&table=1` — Lanes vs Outcomes page, filters carried as `view`
- SectionBody routing — SectionBody (App.tsx:2381-2393) — render — `work/running` → AgentsScreen, `work/workflows` → WorkflowsScreen, `work/timeline` → TimelineLanesScreen (or ActivityScreen for Outcomes), `work/new` → SubmitScreen, `work/new-workflow` → SubmitWorkflowScreen, `work/new-issue` → IssueSubmitScreen, `work/runs` → RunsScreen
- Agent split mounted beside the list — App() (App.tsx:1737-1749) — any Work screen, agent id in the address — renders `AgentSplit` with taskId, pane, artifact, file, theater, detailNamed, closeTo (list address to return to), go, onGroup, base (`work/task/<id>`)
- List told which tab to move to — onGroup/useListGroup — App.tsx:149(prop),1738-1747 — agent's state changes tab (e.g. finishes) while its drawer is open — the router moves the list, the back link and the address to the new tab without closing the drawer
- Row-open callback — openAgent (App.tsx:2329) — row click in AgentsScreen — navigates to `work/task/<id>`

Files covered: `AgentDetail.tsx` (5774 lines), `AgentLogs.tsx`, `AgentBudget.tsx`, `AgentChildren.tsx`, `Handoff.tsx`, `StartupProgress.tsx`, `Liveness.tsx`
(all under `apps/swarm-ui/src/`).

## AgentDetail.tsx (lines 1–2987)

- Close drawer control — AgentDetail (AgentDetail.tsx:310) — all states — icon-only button that closes the agent drawer, calling `onClose`
- Agent drawer dialog — AgentDetail (AgentDetail.tsx:309) — all states — `role="dialog"` container labelled "Agent {taskId}" wrapping the whole standalone (non-headed) detail view
- Tenant-switch offer banner — AgentDetail (AgentDetail.tsx:276) — task found in another of caller's tenants (404 cross-tenant) — info banner "This task is in {tenant}" with a "Switch to {tenant}" button that calls `chooseTenant` and reloads
- Auto re-read while open (drawer poll) — AgentDetail (AgentDetail.tsx:253, `drawerPoll` at 379) — running/non-terminal; also terminal until settled — re-polls every 10s (`DRAWER_POLL_MS`) and keeps polling a terminal task up to 30s (`DRAWER_SETTLE_MS`) until its finish write is whole
- Header summary line (non-headed) — AgentDetail (AgentDetail.tsx:256) — all states — shows `runner_profile · resource_class · N attempts` (or "attempts unread")
- Shared split clock — AgentDetail (AgentDetail.tsx:453) — running; inside split vs standalone — one 1s ticking clock shared across the whole details pane so running figures agree
- Stop control (non-headed only) — AgentDetail (AgentDetail.tsx:469) — running/queued/leased (non-terminal, non-headed) — renders `StopRun` button labelled for "this agent", reload on success
- "What it's building" line — AgentDetail (AgentDetail.tsx:486, `DtBuilding` at 951) — all states — one line combining workflow title/unit/step/issue or prompt's first sentence, with tooltip of full prompt (first 300 chars)
- Alerts area (park/blocked/cancel banners) — AgentDetail (AgentDetail.tsx:488, `Alerts` at 2754) — parked (various reasons); blocked/READY; cancel requested — amber "park reason" bar with eligible-at time, amber "waiting for <pool>" bar, amber "blocked by" list with pool/active/limit, red "Cancellation requested" bar
- Wait panel — AgentDetail (AgentDetail.tsx:489) — waiting/parked with `waiting` API data — delegates to `WaitPanel` which is the single statement of why/next-check/pool
- "Why" sentence — AgentDetail (AgentDetail.tsx:490, `Why` at 2941) — queued/blocked (not parked-handoff, not full-waiting) — one explanatory sentence (`whyAgent`), amber when it needs action
- Questions card — AgentDetail (AgentDetail.tsx:499) — waiting on owner question — shows the agent's questions for its owner (deep-linkable at `#questions`)
- Held-questions card — AgentDetail (AgentDetail.tsx:503) — merge held by owner questions — shows questions blocking a merge, each under its asking step, with answer control
- Decision card — AgentDetail (AgentDetail.tsx:504) — step with a verdict gate — shows rule/verdict/what was weighed
- Failure card (DtFailure) — AgentDetail (AgentDetail.tsx:506, body 1097) — failed/cancelled — heading stating cause in plain words ("Failed: it ran out of time" etc.), cause chip, attempts-used count, who wrote the error, full monospace error text, cancel requester, "Run again" button, help topic link
- "Run again" button — AgentDetail (AgentDetail.tsx:1127, `RunAgain` at 1074) — failed, non-workflow task — opens Submit prefilled with profile/prompt/repo via `prefillTask`
- Outcome card (lead) — AgentDetail (AgentDetail.tsx:508) — succeeded — renders `Output` as the leading card
- Now card (lead) — AgentDetail (AgentDetail.tsx:510, `DtNow` at 1016) — running/queued/leased/starting — phase/now line, last log line, heartbeat badge, checkpoints fact, "Output none yet" note, silent-worker warning
- "Now" line (phase + news + elapsed) — AgentDetail (AgentDetail.tsx:1000, `nowParts` at 984) — running — e.g. "running · tool call: ... · 17m 33s"
- Phase chips (queue/cold start/run) — AgentDetail (AgentDetail.tsx:1041, `DtPhases` at 809) — running/attempted — chips per phase with durations, arrows between them, open/current phase highlighted
- Heartbeat/liveness badge — AgentDetail (AgentDetail.tsx:1046) — running, non-finished — `LivenessBadge` showing "live Ns ago" / "silent" state
- Checkpoints fact (Now/Outcome) — AgentDetail (AgentDetail.tsx:1049, `DtCheckpoints` at 702, `SharedCheckpoints` at 774) — all states — count written, time of newest, bucket-kept count, partial-read marks
- "Output none yet" note — AgentDetail (AgentDetail.tsx:1050) — running, no result yet — explicit "written when the attempt ends"
- Silent-worker warning text — AgentDetail (AgentDetail.tsx:1056) — heartbeat silent — warning paragraph with explanation
- Error banner (secondary, non-failed lead) — AgentDetail (AgentDetail.tsx:512, body 3007) — running with a last_error but not FAILED/CANCELLED lead — shown under leading card
- Budget card — AgentDetail (AgentDetail.tsx:516) — any task with a budget — limit, source, usage, warnings, "work kept on stop" (delegates to `BudgetCard`)
- Resources disclosure row — AgentDetail (AgentDetail.tsx:521, `DtResourceRow` at 2230) — all states — collapsed one-line summary (`cores · memory · tokens · cost`) expandable to strip + full Resources card
- Stat strip (Elapsed/Attempt/Peak memory/CPU/Cost cells) — AgentDetail (AgentDetail.tsx:522, `DtStrip` at 1430, `DtStatCell` at 1242) — running/ran/failed — five metric tiles with bars, units, sub-captions, absent/unread/reading tones
  - Elapsed/Waiting/Run/Ran tile — AgentDetail.tsx:1457 — shows duration, start/end times, multi-run/account-swap note (`runsLine`)
  - Attempt count tile — AgentDetail.tsx:1499 — "N of max" with retries or counted-vs-documents mismatch note
  - Peak memory tile — AgentDetail.tsx:1536 — bytes, % of class limit, worst-earlier-attempt callout, OOM-near-miss flag
  - CPU tile — AgentDetail.tsx:1597 (`cpuCell` at 1713) — peak/mean cores vs limit, sizing/burst note
  - Cost tile — AgentDetail.tsx:1625-1691 — $ amount (or "at least $"), token breakdown sub-line, "no model call" fact for non-reporting profiles
- Resources card ("Against each limit") — AgentDetail (AgentDetail.tsx:523, `DtResources` at 2018) — all states — Memory/Workspace/CPU peak/CPU mean rows each with bar, value, note, help topic
- Utilisation-over-time panel — AgentDetail (AgentDetail.tsx:2143) — running/finished — embeds `UtilisationPanel` (CPU/memory per 5s window with throttling shading)
- "Details: memory, spend and each attempt over time" disclosure — AgentDetail (AgentDetail.tsx:2144) — all states — expands to the full `Attempts` list
- Progress card — AgentDetail (AgentDetail.tsx:525, `DtProgress` at 1789) — all states — per-attempt phase bars on one shared time scale, legend (queue/cold start/run/failed/open), link to Attempts tab
- Attempt phase row — AgentDetail (AgentDetail.tsx:1860, `DtPhaseRow` at 1988) — per attempt — compact stacked bar (queue/cold/run) with total span, open/red-for-failed styling
- "Attempt read failed" / "no attempts" / "counts N returned 0" empty states — AgentDetail (AgentDetail.tsx:1812, 1818, 1832) — read failure / never admitted / document-count mismatch — distinct empty-state messages, never conflated
- Recent events list (last 5, newest first) — AgentDetail (AgentDetail.tsx:1904, `DtEventRow` at 1764) — all states — time, kind, gap since prior event, owning attempt label
- "All N events" disclosure — AgentDetail (AgentDetail.tsx:1931) — events.length > 5 — expands remaining events oldest-after-5
- Event-history read-failed / "0 events" states — AgentDetail (AgentDetail.tsx:1892, 1897) — read failure / impossible-empty — explicit failed-read messaging
- "Page proof"/pagination caveat note — AgentDetail (AgentDetail.tsx:1913, 1924) — terminal with missing terminal event vs normal — partial-read mark or "Newest first · first page only" aria-label caveat
- Submitted-by line — AgentDetail (AgentDetail.tsx:1943) — all states — "submitted {time} · by {PersonName} · {age}"
- Output card (secondary, once there's output) — AgentDetail (AgentDetail.tsx:529-531) — running with a result_summary or refused publish — shown under leading card even before terminal
- Input card — AgentDetail (AgentDetail.tsx:532) — all states — masked prompt/input viewer (body defined later)
- Checkpoints help topic icon — AgentDetail (AgentDetail.tsx:1038) — Now card — `HelpCard topic="checkpoints"`
- Attempt-documents help topic icons — AgentDetail (AgentDetail.tsx:1128, 1809) — Failure card / Progress card
- Requests-are-ceilings help topic icon — AgentDetail (AgentDetail.tsx:2125) — Resources card
- Per-metric help notes (`?` tooltips) — AgentDetail (AgentDetail.tsx:1256) — on every stat-strip cell with a `help` topic (peak-memory, cpu-figures, token-cost)
- Cancellation requester display — AgentDetail (AgentDetail.tsx:1171, `cancelRequester` at 1206) — cancelled on request — "Stop requested by {name}" or "who asked is not on the events read"
- Cancel note text — AgentDetail (AgentDetail.tsx:1182) — cancelled — shows recorded cancel text as a neutral note, not an error
- "Error written by" attribution — AgentDetail (AgentDetail.tsx:1186) — failed — names reconciler/worker/API as the writer (`errorWriter`)

## AgentDetail.tsx (lines 2900–5774)

- Park reason banner with eligible-time — AgentDetail (AgentDetail.tsx:2786) — parked (any non-hand-off, non-wait-panel park reason) — shows the enum reason in bold, " · eligible at/since <time>", and a fallback sentence when the screen has no copy for the reason
- Hand-off "parked since" label — AgentDetail (AgentDetail.tsx:2882) — parked (HUMAN_REQUIRED hand-off) — folds the park instant into the hand-off banner instead of a separate eligibility bar
- Park eligibility clock ("eligible at/since") — AgentDetail (AgentDetail.tsx:2901) — parked with a `next_eligible_at` — renders the formatted local time the park becomes eligible again, future vs past wording
- "Waiting for" pool bar — AgentDetail (AgentDetail.tsx:2803) — queued/ready, blocked on a pool — names the pool the task is waiting on (live) and "(holds no capacity)" aside when applicable; amber only when action is needed
- Last-scheduler-pass blockers list — AgentDetail (AgentDetail.tsx:2823) — READY with blockers — lists each blocker's pool, reason, active/limit counts, and unrecognised-reason fallback text
- Cancellation-requested bar — AgentDetail (AgentDetail.tsx:2852) — live task with `cancel_requested` — red bar reading "Cancellation requested"
- "Why" sentence (single explanation of non-movement) — AgentDetail (AgentDetail.tsx:2982) — queued/leased/running, not parked-handoff, not fully-waiting — one derived sentence on why the agent hasn't progressed, styled as warning only when it needs action
- Error banner (full, monospace) — AgentDetail (AgentDetail.tsx:3007) — failed — shows `last_error` in full with an "error · <writer>" eyebrow naming who wrote it (reconciler / scheduler · dispatch / scheduler · cancel / scheduler / agent, at finish)
- Attempt history read-failure empty state — AgentDetail (AgentDetail.tsx:3081) — attempts tab, attempts read failed — "Attempt history could not be read" message with detail
- Attempt history real-zero / hole-in-record states — AgentDetail (AgentDetail.tsx:3098) — attempts tab, zero attempts returned — distinguishes "counts N · returned 0" (a hole) from "no attempt yet" (real zero), with the task's current state word
- Attempts section heading with count and partial mark — AgentDetail (AgentDetail.tsx:3146) — attempts tab, 1+ attempts returned — "Attempts" heading, bare count chip, and a partial-data mark when fewer attempts were returned than `attempt_count`
- Token/spend chart over all attempts — AgentDetail (AgentDetail.tsx:3167) — attempts tab, multiple attempts — `TokenSpendChart` showing spend coverage across the run
- Attempt wall-clock duration chart — AgentDetail (AgentDetail.tsx:3176) — attempts tab — `AttemptDurations` visualizing queue/cold-start/run phases per attempt
- Attempt card list — AgentDetail (AgentDetail.tsx:3178) — attempts tab — renders one `AttemptCard` per attempt, ordered oldest first
- Attempt card: generation label & outcome chip — AgentDetail (AgentDetail.tsx:3358) — any attempt (parked / superseded / ended-no-record / running / finished) — attempt ordinal+generation title, outcome chip corrected for parked/superseded cases
- "latest" tag on newest attempt — AgentDetail (AgentDetail.tsx:3367) — any attempt that is the latest — grey outline "latest" metadata tag
- OOM near-miss badge — AgentDetail (AgentDetail.tsx:3368) — attempt flagged `oom_near_miss` — red "OOM near miss" tone mark
- Backend label on attempt card — AgentDetail (AgentDetail.tsx:3380) — any attempt — shows humanised backend (e.g. "Cloud Run" vs raw `CLOUD_RUN_JOB`)
- Attempt start/end facts — AgentDetail (AgentDetail.tsx:3389) — any attempt — start and end timestamps, em-dash when absent
- Attempt account fact — AgentDetail (AgentDetail.tsx:3298) — any attempt — which account ran the attempt, or "not read"/"not assigned"
- Attempt model fact (with class) — AgentDetail (AgentDetail.tsx:3244, `AttemptModel`) — any attempt — model name run, resource/model class, "(default)" marker, and a CLI-model-mismatch warning line when the CLI ran a different model than pinned
- Attempt "ran" duration (multi-attempt only) — AgentDetail (AgentDetail.tsx:3408) — attempts with several>1 — per-attempt elapsed span, or an absent mark when the end was never recorded
- Attempt error block — AgentDetail (AgentDetail.tsx:3436) — failed/earlier attempt with its own error distinct from task's last_error — full monospace error text
- Attempt resources panel (requested vs utilised) — AgentDetail (AgentDetail.tsx:3568) — any attempt — memory/workspace/CPU bars against the resource-class ceiling, with "no ceiling" marks for missing catalogue
- Memory peak RSS bar (CeilingRow) — AgentDetail (AgentDetail.tsx:3598) — any attempt — peak RSS vs class memory limit, live-heartbeat fallback note
- Workspace peak disk bar (CeilingRow) — AgentDetail (AgentDetail.tsx:3609) — any attempt — peak disk usage vs class disk limit
- CPU peak/mean bars and note — AgentDetail (AgentDetail.tsx:3730, 4168) — any attempt — peak/mean vCPU vs ceiling, with "at exit"/"live reading"/"last written"/"heartbeat event" provenance strip and sized-cpu burst note (guaranteed vs burst cores)
- Memory history step chart — AgentDetail (AgentDetail.tsx:3676) — any attempt — `PeakMemoryChart` showing how peak RSS was reached over heartbeats
- Attempt spend (tokens & cost) fact list — AgentDetail (AgentDetail.tsx:4205) — any attempt — in/out/cache-read/cache-write tokens and USD cost, or a reason mark when none reported (runner doesn't report / still running / never started / genuinely none)
- Attempt checkpoints summary & "resumed from" line — AgentDetail (AgentDetail.tsx:4304) — any attempt — count of checkpoints written, "resumed <id> · from <attempt> · N files · size" note, and location-coverage fraction when some checkpoint locations are unknown
- Checkpoint cadence strip — AgentDetail (AgentDetail.tsx:4368) — attempt with checkpoints — `CheckpointStrip` timeline of checkpoint events
- Output/Code card heading (Output vs Outcome vs Code) — AgentDetail (AgentDetail.tsx:4441) — running (Output, empty) / finished with git (Code) / lead card on finished agent (Outcome) — single heading that adapts to context
- "last attempt of N" scope qualifier — AgentDetail (AgentDetail.tsx:4456) — finished, retried task — marks that the Output/Code panel only describes the last attempt
- Artifacts link from Output card — AgentDetail (AgentDetail.tsx:4466) — finished — "Artifacts ›" link/tab-switch to the Artifacts tab
- Help tooltip on Output card ("outcome-explained") — AgentDetail (AgentDetail.tsx:4471) — any — `HelpCard` popover explaining what the outcome describes
- Refused-and-retrying lead (publish refusal while non-terminal) — AgentDetail (AgentDetail.tsx:4480) — running, mid-retry after a publish refusal — shows the credential/title refusal lead before task finishes
- "nothing written yet" empty state — AgentDetail (AgentDetail.tsx:4484) — running, no result yet — zero-state placeholder
- "nothing ran" empty state — AgentDetail (AgentDetail.tsx:4497) — finished, no attempt ever started — zero-state
- "finished with no summary" partial state — AgentDetail (AgentDetail.tsx:4504) — finished, parked-without-summary — partial-state note pointing to timeline
- Git outcome section (full) — AgentDetail (AgentDetail.tsx:4512, 4627) — finished with a result summary — commits, dirty files, patch, branch, repo, PR link/state, auto-commit note, diffstat chart, commit table
- Spend summary from legacy result_summary — AgentDetail (AgentDetail.tsx:4535) — finished, no typed per-attempt spend — "spend · result summary" fallback with tokens/cost/models, marked "last attempt only"
- Publish-refused lead with per-refusal list — AgentDetail (AgentDetail.tsx:5095) — failed/retrying due to publish refusal — "Publish refused N times", listing each attempt's credential-file or pr-title refusal
- "Failed: nothing to publish" lead — AgentDetail (AgentDetail.tsx:5130) — failed, branch has no commits/no uncommitted files — failure lead
- "Pull request opened/reused" lead — AgentDetail (AgentDetail.tsx:5144) — finished, PR exists — bold sentence with PR link, "opened"/"reused", and branch name
- Base-pin line (clone pin provenance) — AgentDetail (AgentDetail.tsx:5169) — finished with git summary — "Base pinned to <sha>, from <source>" or "not pinned · <reason>" or "pin not recorded"
- "Changes ›" link to diff viewer tab — AgentDetail (AgentDetail.tsx:5226) — finished, has a patch and no credential-withholding refusal — links/switches to Changes tab with file count and patch size
- Copy "apply" button for patch — AgentDetail (AgentDetail.tsx:4734) — finished, has a patch artifact — copies `gsutil cat <uri> | git apply -` to clipboard
- Patch-discarded note (size cap) — AgentDetail (AgentDetail.tsx:4742) — finished, patch too large — "discarded at N bytes" partial mark
- Diffstat chart — AgentDetail (AgentDetail.tsx:4811) — finished with commits — `DiffstatChart` bar visualization of insertions/deletions per commit
- Commit table — AgentDetail (AgentDetail.tsx:4821) — finished with commits — stacked table of commit sha, subject, files changed (+binary count), +/− lines, with "newest N of M" caption when truncated
- Publish-outcome explanation (6+ causes) — AgentDetail (AgentDetail.tsx:5256) — failed/finished with no PR — distinguishes contributor-in-integrate (pushed-by-design), pushed-no-PR, collect-strategy (nothing pushed by request), token-cannot-push, no-reason-recorded, and free-text reason matches (changed nothing / parked / not-a-forge / forge-unreachable / publishing-disabled / repo-url-unknown / git push rejected)
- "Handed on" diff-to-dependant panel — AgentDetail (AgentDetail.tsx:4881) — workflow step, diff staged for a downstream step, no commits — shows which later workflow step(s) each unchanged-but-handed file was staged to, and the integrator's resulting PR link/state
- Handed-file diff stat + inline viewer — AgentDetail (AgentDetail.tsx:4944) — workflow step hand-off — "+N −M in K files, handed to <step> as <name>" with a "show diff"/collapsible inline `ArtifactViewer`
- Input card (prompt + facts) — AgentDetail (AgentDetail.tsx:5505) — any state — repo/ref, additional repositories, model, provider, priority, timeout (+ "over by" note when exceeded)
- Masked-input note ("masked N") — AgentDetail (AgentDetail.tsx:5508, 5726) — any — count of credential-shaped redactions in the prompt/metadata/full-input blocks
- Help tooltip on Input card ("input-as-submitted") — AgentDetail (AgentDetail.tsx:5511) — any — `HelpCard` popover
- Timeout-exceeded note — AgentDetail (AgentDetail.tsx:5546) — running past its `timeout_seconds` — "over by <duration>" tooltip, clarifying the timeout did not end the run
- Prompt preview + "Show prompt" disclosure — AgentDetail (AgentDetail.tsx:5576) — any, prompt present — 3-line preview plus collapsible full prompt with character count
- "No prompt" / "prompt not text" state — AgentDetail (AgentDetail.tsx:5560) — any, no prompt field or non-string prompt key — explanatory sentence
- Empty-prompt state — AgentDetail (AgentDetail.tsx:5571) — any, prompt submitted as empty string — "empty prompt" zero mark
- Metadata disclosure with key count — AgentDetail (AgentDetail.tsx:5584) — any — collapsible "Metadata · N keys" section
- Masked metadata table — AgentDetail (AgentDetail.tsx:5611) — any, metadata copy served — key/value table, platform-key badge ("platform · as stored"), masked-count note, zero/absent/not-served states
- Rest-of-input / full-input block — AgentDetail (AgentDetail.tsx:5687) — any — shows remaining submitted fields beyond the prompt (or the whole input when no prompt), each masked
- Staged inputs panel — AgentDetail (AgentDetail.tsx:5595) — any, task declared input files — `StagedInputs` component listing files staged into the workspace, linked to producing run
- Input-not-read states — AgentDetail (AgentDetail.tsx:5740) — any, input copy read failed/loading/unsupported by API — "reading", "not served by this API", or "input not read" with server error message

## AgentLogs.tsx

- Transcript/runner/stdout/stderr stream picker — AgentLogs (AgentLogs.tsx:792) — running; parked; waiting on owner question; failed; succeeded; cancelled; logs tab — segmented control to pick which of the four log views to read
- Search this window — AgentLogs (AgentLogs.tsx:800) — logs tab — text input filtering lines in the currently-loaded window only, not the whole stream
- Search hit counter — AgentLogs (AgentLogs.tsx:825) — logs tab (while searching) — shows "n of m · this window" or "none in this window"
- Enter jumps to next search hit — AgentLogs (AgentLogs.tsx:816) — logs tab — pressing Enter in search box calls `jump(targets.hits)`
- Follow/Paused toggle — AgentLogs (AgentLogs.tsx:697) — running — button (keyboard `F`) toggling auto-scroll-to-newest
- "Ended · complete log" status mark — AgentLogs (AgentLogs.tsx:687) — failed; succeeded; cancelled — replaces Follow button on a finished agent, says nothing more can arrive
- Jump to error button — AgentLogs (AgentLogs.tsx:715) — logs tab — button (keyboard `E`) cycling through lines matching the error pattern/transcript `is_error`
- Error count/position label — AgentLogs (AgentLogs.tsx:701-714) — logs tab — "Errors · reading" / "No error matched" / "N error lines" / "Error k of N"
- Attempt picker — AgentLogs (AgentLogs.tsx:747) — logs tab — `<select>` of attempts labelled by generation, exit code/"running"/"never started"/"end not recorded"
- Wrap toggle — AgentLogs (AgentLogs.tsx:778) — logs tab — button (keyboard `W`) toggling line wrap; defaults on for phone width
- Masked count chip — AgentLogs (AgentLogs.tsx:843) — logs tab — "masked N" warning chip when server redacted lines in this window
- Not-masked warning chip — AgentLogs (AgentLogs.tsx:844) — logs tab — warns when the server did not apply masking to this read
- "attempts not read" note — AgentLogs (AgentLogs.tsx:670) — logs tab — shown when attempts list failed to load, so only "latest" can be picked
- More menu (details/summary) — AgentLogs (AgentLogs.tsx:850) — logs tab — overflow menu collecting controls that spilled off the bar (unread note, wrap, attempt picker, error button) by priority order (`BAR_SPILL`)
- Escape closes More menu — AgentLogs (AgentLogs.tsx:616) — logs tab — keyboard handler stopping Escape from bubbling to close the whole agent split
- Read from first/newest window toggle — AgentLogs (AgentLogs.tsx:773) — finished + transcript view — button switching between the transcript's first window and its newest window
- Attempt fact row (id, generation, ordinal) — AttemptFact (AgentLogs.tsx:278) — logs tab (More menu) — shows `1 · gen 1 · att_8f8e…`
- Copy attempt id button — AttemptFact (AgentLogs.tsx:298) — logs tab (More menu) — copies full attempt id to clipboard, shows "copied"/"copy refused"
- Source fact (live tail / final record / artifact) — AgentLogs (AgentLogs.tsx:877-897) — logs tab (More menu) — names where the shown window was read from
- "following paused at …" note — AgentLogs (AgentLogs.tsx:886) — paused — shows the wall-clock time following was paused, with anything newer counted below
- Masked fact row — AgentLogs (AgentLogs.tsx:898) — logs tab (More menu) — "N in this window"
- Masking-applied fact row — AgentLogs (AgentLogs.tsx:904) — logs tab (More menu) — "at read time" / "not applied at read time"
- CLI-success-but-task-failed notice — AgentLogs (AgentLogs.tsx:922) — failed + transcript view, ending in reported success — banner clarifying the transcript's `result · success` is the CLI's own verdict, not why the task failed; quotes the actual recorded error
- Log body / gap notice — AgentLogs (AgentLogs.tsx:935-936) — logs tab — scrollable log content area; shows `GapNotice` for missing bytes between reads (transcript view)
- Loading indicator — AgentLogs (AgentLogs.tsx:937-941) — logs tab (reading) — "Reading … for gen N / the latest attempt" with progress bar
- "N new lines/steps since you paused · Follow" button — AgentLogs (AgentLogs.tsx:947) — paused; running — floating button resuming follow when new content arrived while paused
- Keyboard shortcuts F/W/E/⁄ — AgentLogs (AgentLogs.tsx:579-596) — logs tab — global key handler (ignored in inputs/buttons/links) for follow, wrap, next-error, focus-search
- Auto-default stream per runner — defaultView (AgentLogs.tsx:74) — logs tab (on open) — opens on `transcript` for claude/codex profiles, `runner` otherwise
- Reset state on switching agents — AgentLogs (AgentLogs.tsx:332-340) — logs tab — view/attempt/search/window reset when a different task id is opened in the same split
- Poll cadence while content can still change — AgentLogs (AgentLogs.tsx:383-392) — running; recently finished — re-reads every 5s unless tab hidden, until both transcript and logs settle
- Details tab "Last log line" strip — LogLastLine (AgentLogs.tsx:1012) — running; queued/leased; parked; failed; succeeded; cancelled (Details tab) — one-line preview of the newest non-progress log/transcript line
- "Open logs ›" link — LogLastLine (AgentLogs.tsx:1024) — Details tab — switches to the Logs tab
- "reading…" / "the log was not read" / "no log lines yet/in this read" states — LogLastLine (AgentLogs.tsx:984-992) — loading; error; terminal with no lines; live with no lines yet
- "(the error, as logged)" / "(not the error)" label — LogLastLine (AgentLogs.tsx:1003-1005) — failed — clarifies whether the shown last line is the recorded error
- "its CLI reported success · it failed after that" substitution — LogLastLine (AgentLogs.tsx:1011,1018-1019) — failed + transcript ends in reported success — avoids showing a misleading success line under a failure
- Markdown-rendered transcript line — LogLastLine (AgentLogs.tsx:1021) — Details tab, transcript view — renders the last line's inline markdown (code spans etc.)

## AgentBudget.tsx

- Budget row mark "stopped at its budget" — BudgetRowMark (AgentBudget.tsx:264-270) — failed (ended by budget) — warn-tone chip on the agents list row, titled with the stop sentence
- Budget row mark "budget N%" — BudgetRowMark (AgentBudget.tsx:272-277) — running past 80% warning — warn-tone chip showing warning percent
- Budget card header "Budget" / "Budget: stopped at its budget" — BudgetCard (AgentBudget.tsx:313-316) — set; stopped — section heading on the agent Details tab, hidden entirely when agent has no budget
- Budget card live warning chip — BudgetCard (AgentBudget.tsx:317-321) — running, warned, not finished — "budget N%" chip in the card header
- Limit fact rows (per unit) — BudgetCard (AgentBudget.tsx:323-335) — set — one row per limited unit (usd/tokens) showing amount and its source ("set by this step" / "the workspace's default" / "the platform's ceiling" / "where it came from is not recorded")
- Used fact row — BudgetCard (AgentBudget.tsx:336-349) — set; warned; stopped; finished unreported — shows amount used of limit, or "not reported" / "under 80% so far…" / "not read · the event read failed" / "reading…"
- Warned fact row — BudgetCard (AgentBudget.tsx:350-368) — warned or ended warned — shows when it crossed 80%, with time-ago and absolute-time title, and whether the agent itself was told ("told to finish and publish" / "this runner cannot be told mid-run" / "when is not on the events read")
- Stopped note with checkpoint/publish explanation — BudgetCard (AgentBudget.tsx:370-391) — stopped at budget — explains "stopped mid-turn, past 120%" or "stopped at a turn boundary", that work was checkpointed/published and the step can be resumed
- Pull request link on stop — BudgetCard (AgentBudget.tsx:378-384) — stopped, opened a PR — link "Pull request #N" opening in new tab
- Branch name on stop (no PR) — BudgetCard (AgentBudget.tsx:385-389) — stopped, no PR but has branch — shows branch name in monospace
- `useBudgetWarning` background read — useBudgetWarning (AgentBudget.tsx:223) — running or ended-warned — fetches newest events page once per attempt to find the 80% warning when not already in the held events page

## AgentChildren.tsx

- "child of …" parent link — AgParentLink (AgentChildren.tsx:106-119) — any child task (Details header) — link back to the parent agent, with parent's name or id, and "submitted by attempt …" note
- Children tab badge count — useChildCount (AgentChildren.tsx:127) — any state offering/serving children — numeric count read for the tab label
- Awaiting-children banner — AgChildrenPane (AgentChildren.tsx:160-176) — parked (reason `CHILDREN_INCOMPLETE`) — explains the parent holds no lease/capacity while waiting, moves to ready once all children end; shows "Await deadline" as explicitly not served
- Children loading state — AgChildrenPane (AgentChildren.tsx:178-183) — children tab, loading — "Reading this agent's children" with progress bar
- Children read error state — AgChildrenPane (AgentChildren.tsx:184-189) — children tab, error — "children not read · {message}"
- Children not-served state — AgChildrenPane (AgentChildren.tsx:190-198) — children tab, API doesn't serve parent_task_id — "not available on this deployment yet"
- Empty children state — ChildTable (AgentChildren.tsx:221-232) — children tab, zero children — "Children" heading plus "no children" with zero-mark
- Children header "waiting on N · M of —" — ChildTable (AgentChildren.tsx:239-249) — children tab, has children — count of unfinished children, total count, fan-out cap explicitly "not served", "depth 1: a child cannot have children" note
- Children-by-state mix chips — ChildTable (AgentChildren.tsx:250-256) — children tab, has children — one `StateMark` + count chip per distinct child state present
- "await refunds N used" — ChildTable (AgentChildren.tsx:257-260) — children tab, has children — count of await-resume refunds used, or em-dash if unknown
- Children table — ChildTable (AgentChildren.tsx:262-313) — children tab, has children — columns State, Child (link), Request id, Runner · resource class, From (parent attempt id), Now (elapsed), Outputs (file count written)
- Child row link — ChildTable (AgentChildren.tsx:285-287) — children tab — navigates to `#work/task/<id>` for that child
- Child row ordering — childOrder (AgentChildren.tsx:73-79) — children tab — running/queued first, then failed, then succeeded, each by submission time
- Footer disclaimer note — ChildTable (AgentChildren.tsx:314-317) — children tab, has children — "a failed child does not fail {agent}"

## Handoff.tsx

- Live-browser detection — isLiveBrowser (Handoff.tsx:56) — any state — determines whether a task's runner profile or `human_wait` field marks it as carrying a live browser
- Owner-questions hold detection — ownerQuestionsHold (Handoff.tsx:117) — parked (owner questions, merge profile) — reads a PARKED(HUMAN_REQUIRED) merge task's `blocked_by` entry to list unanswered owner questions, separate from a hand-off, so no hand-off UI is drawn for it
- Hand-off phase derivation — handoffOf (Handoff.tsx:149) — hold (RUNNING + human_wait) / parked (PARKED + HUMAN_REQUIRED, not owner-questions) — computes reason, expected site, deadline and controller for the "hold" phase, and dead-letter deadline for the "parked" phase
- Resuming-after-handoff detection — resumingAfterHandoff (Handoff.tsx:187) — resuming (READY/LEASED/DISPATCHED/STARTING with `metadata.human_ready` set) — flags a task on its way back from a hand-off so the UI shows a neutral "starting again" banner instead of nothing
- "Needs you" predicate — needsYou (Handoff.tsx:192) — hold or parked — true whenever `handoffOf` is non-null; used to decide whether a task counts in "Needs a look"/Waiting lists
- Time-left countdown text — timeLeft (Handoff.tsx:203) — hold (counting to park) and parked (counting to dead-letter) — formats remaining time as `m:ss left` under an hour or `h:mm:ss left` beyond, null once past deadline
- Live Browsers filter ordering — liveBrowserRows (Handoff.tsx:221) — running, live-browser tasks — filters to RUNNING live-browser tasks and sorts so tasks currently on hold (needing a person) come first
- "I'm ready" API action — humanReady (Handoff.tsx:232) — parked — posts `/v1/tasks/{id}/human-ready` with no body; fixture build always refuses with a 404-shaped error
- Parked hand-off reason lookup — useHandoffRequest (Handoff.tsx:256) — parked — reads the newest `human_handoff_requested` event to recover the reason/expected-site text the park cleared from the task row; returns `'unread'` if none found
- "Who you are" lookup — useYou (Handoff.tsx:290) — hold, when someone holds control — loads the live session and reads `session.you.email` once, to render "You are in control" instead of the controller's raw address
- Hand-off banner — HandoffBanner (Handoff.tsx:334) — hold; parked — headline "The agent needs your help: `<reason>`", warn-toned banner; shown across the Browser tab or once per waiting task on Overview
  - Headline/title text — Handoff.tsx:404 — hold/parked — `title={The agent needs your help: ${reasonLine}}`, falling back to "Reading why it asked…" or "The agent gave no reason that could be read."
  - "I'm ready" button — Handoff.tsx:408-410 — parked, not yet pressed — primary button, shows "Asking…" busy state while in flight, calls `humanReady`
  - Expected-site fact line — Handoff.tsx:420-428 — hold/parked — shows `expects <host>` or "no expected site given"
  - "Parked until you say you are ready" chip — Handoff.tsx:419 — parked, before ready — prepended fact-line text
  - Control status text — Handoff.tsx:393-398, rendered 429 — hold, controller present — "You are in control · return control when done" or "`<controller>` is in control"
  - Time-left clock text — Handoff.tsx:381-390, rendered 430 — hold ("… before the browser is parked" / hold-over message); parked ("… before it is given up (dead-lettered)" / past-24h message)
  - "What to be ready for" explanatory line — Handoff.tsx:440-445 — parked, reason unread — explains the browser restarts and the agent asks again there
  - "Starting your browser" confirmation line — Handoff.tsx:446-451 — parked, after pressing "I'm ready" (ready.kind === 'done') — tells the user the browser is admitted/cold-starting
  - Refusal error line — Handoff.tsx:452-456 — parked, `humanReady` failed — `role="alert"`, "Not resumed: `<error.message>`"
  - `children` slot for caller actions (Take control / Return control / Open in theater) — Handoff.tsx:339,345,411,414 — hold/parked — banner renders caller-supplied action buttons beside its own
- Resuming banner — ResumingBanner (Handoff.tsx:466) — resuming — info-toned (blue) banner, title "Starting your browser again — the agent will ask for you when it is back", explanatory line "You said you are ready. It is admitted like any agent and starts cold, so allow a minute or two."
- Hand-off strip wrapper — HandoffStrip (Handoff.tsx:485) — hold/parked/resuming — renders nothing when neither a hand-off nor a resume is in progress; otherwise wraps `TickingBanner`
- Ticking banner (live clock wrapper) — TickingBanner (Handoff.tsx:504) — hold/parked — drives the banner's countdown off a shared 1-second clock (`useNow(1000)`) so multiple on-page banners tick in sync

## StartupProgress.tsx

- Poll cadence constant — STARTUP_POLL_MS (StartupProgress.tsx:40) — starting — defines the 2s polling interval while startup is in progress
- Named startup stages — STARTUP_STAGES (StartupProgress.tsx:43) — starting — six labeled stages shown to the user: "Capacity reserved", "Machine found", "Downloading browser image", "Starting the agent", "Starting Chromium", "Live", each mapped from one or more raw pod/lifecycle steps
- `clock` time formatter — clock (StartupProgress.tsx:85) — starting — formats seconds as `m:ss`, clamped at zero
- Stage view computation — startupView (StartupProgress.tsx:91) — starting — computes each stage's status (done/skipped/current/todo), per-stage elapsed seconds, overall percent complete, total elapsed time, and the "time remaining" estimate
- "Skipped" stage detection (warm start) — StartupProgress.tsx:111-112 — starting, warm start — marks a stage whose steps were never reported, but a later stage was reached, as `skipped` ("already ready") rather than stuck
- Time-remaining estimate text — StartupProgress.tsx:126-132 — starting, with history (`sampleSize>0`) — shows `~N s left` based on the profile's historical median, or "taking longer than usual" once past the median; shows nothing when there's no history
- Event-based fallback data builder — fromEvents (StartupProgress.tsx:147) — starting, startup route not deployed (404) — derives steps from the task's own lifecycle events (`lease_acquired`, `dispatched`, `starting`, `running`) scoped to the current attempt
- Startup-read data mapper — fromRead (StartupProgress.tsx:164) — starting — converts the `/v1/tasks/{id}/startup` API response into the internal `StartupData` shape
- Startup data loader with fallback — readStartup (StartupProgress.tsx:174) — starting — tries `loadStartup`; on failure (e.g., 404) falls back to `loadTaskEventsPage`; returns null (keep last drawn) if both fail
- Visibility-aware polling hook — useStartup (StartupProgress.tsx:200) — starting — polls every `STARTUP_POLL_MS` only while the document tab is visible and not yet done; stops polling permanently once `done` is true; resumes on `visibilitychange`
- Live ticking clock hook — useNow (StartupProgress.tsx:236) — starting — updates once per second while `on`, drives the elapsed/remaining displays
- noVNC screen-connected detection — useConnected (StartupProgress.tsx:253) — starting (theater overlay) — watches a borrowed screen element's `data-conn` attribute via MutationObserver; once connected, the startup overlay steps aside
- Status marks/labels — MARK/SAY (StartupProgress.tsx:266-272) — done (checkmark/"done"), skipped (checkmark/"done"), current (dot/"in progress"), todo (circle/"to come") — visual/textual per-stage status indicators
- Full startup progress panel — StartupProgress (StartupProgress.tsx:280) — starting (Browser tab, theater overlay) — props: `taskId`, `state?`, `overlay?`, `hideWhenDone?`, `screen?`
  - Loading placeholder — StartupProgress.tsx:300-316 — data not yet loaded — "The agent's browser is starting." / "Reading its start-up."
  - Title with completion/progress text — StartupProgress.tsx:324-327 — starting/done — "The agent's browser is starting." or "...has started.", plus the "~N s left"/"taking longer" remaining text
  - Progress bar — StartupProgress.tsx:328-337, aria-label="Browser start-up" (line 331) — starting — `role="progressbar"`, `aria-valuemin/max/now`, `aria-valuetext` naming the current stage and percent; visual fill width = percent
  - Step list (ordered list of stages) — StartupProgress.tsx:339-350 — starting — each stage shows its mark, label, "skipped (already ready)" annotation when skipped, a screen-reader-only status word, and elapsed/done time (`clock(s.seconds)`) when known
  - `aria-current="step"` on the in-progress stage — StartupProgress.tsx:341 — current stage
  - Fallback-source notice — StartupProgress.tsx:352-354 — fallback (startup route 404'd) — "Start-up steps from the agent's events; the pod's own steps are not reported here yet."
  - Hide-when-done behavior — StartupProgress.tsx:298,318 — connected/done with `hideWhenDone` — renders nothing once a live screen is connected or startup is done and `hideWhenDone` is set
- Thumbnail thin progress bar — StartupBar (StartupProgress.tsx:364) — starting (workflow graph live thumbnail) — props: `taskId`
  - Thin progress bar — StartupProgress.tsx:372-382, aria-label="Browser start-up" (line 375) — starting — same progressbar semantics as the full bar, thin style
  - "starting m:ss" caption with tooltip — StartupProgress.tsx:383-385 — starting — `title={current?.label}` tooltip naming current stage, text shows elapsed time via `clock(v.elapsed)`
  - Renders nothing once done — StartupProgress.tsx:367 — done

## Liveness.tsx

- Liveness classification function — livenessOf (Liveness.tsx:50) — finished; not-started (queued/parked/other non-concurrency state); quiet (events unreadable); quiet (no timestamp); live (<180s); quiet (180-420s); silent (>420s) — computes `{kind, word, copy, say, tone}` from task state and newest event age
  - Finished-task special case — Liveness.tsx:55-61 — finished (terminal state) — returns `word:"done"`, blank copy, `say:"Finished."`; badge renders nothing for this case
  - Not-dispatched case — Liveness.tsx:62-74 — queued/parked/other non-concurrency state — word is the task's own state name (lowercased), copy "not dispatched", say explains there is no worker to be live or silent
  - Unreadable-events case — Liveness.tsx:79-87 — events param is null (read failed) — word "unknown", copy "events not read", say clarifies this is not evidence the worker is gone
  - No-timestamp case — Liveness.tsx:92-100 — newest event has no parseable timestamp — word "unknown", copy "no timestamp"
  - Live state — Liveness.tsx:111-113 — age < 180s — word "live", tone "ok", copy "`<N>`s ago"
  - Quiet state — Liveness.tsx:114-122 — age 180-419s — word "quiet", tone "warn", copy "`<age>` ago", say explains ~150s heartbeat cadence, "not yet alarming"
  - Silent state — Liveness.tsx:123-129 — age ≥ 420s — word "silent", tone "bad", say warns the worker may be gone and the reconciler reclaims a stale lease
  - Age formatting rule — Liveness.tsx:108-109 — quiet/silent — minutes under an hour (`Nm`), shared `spanText` formatter (e.g., `8d 21h`) beyond an hour
- Liveness badge component — LivenessBadge (Liveness.tsx:150) — live; quiet; silent; not-started; (nothing for finished) — props: `task`, `events`, `now`
  - Renders nothing for a finished task — Liveness.tsx:160 — finished
  - Accessible badge with tone mark, word, and optional copy — Liveness.tsx:161-177 — `role="img"`, `aria-label` carries the full sentence plus the shared resolution caveat (line 168), `title` repeats sentence+caveat for pointer hover (line 169)
  - Tone-colored dot/shape — ToneMark usage (Liveness.tsx:174) — ok/warn/bad/unknown — shared `.ctl-dot` shape+color, same silhouette vocabulary as the state chip
  - Word span — Liveness.tsx:175 — always — e.g. "live", "quiet", "silent", state name
  - Copy span (figure) — Liveness.tsx:176 — when `copy !== ''` — e.g. "4m ago", "not dispatched", "events not read"
  - Shared caption text — CAPTION constant (Liveness.tsx:21-22) — always (via aria-label/title) — discloses ~150s event-derived resolution vs. unexposed 30s lease heartbeat

Files: `LiveBrowser.tsx` (2352 lines), `LiveThumb.tsx` (729 lines), `LiveToolMenu.tsx` (64 lines), `BrowserBadges.tsx` (304 lines), `Theater.tsx` (1064 lines, covered only for its live-browser/theater-mode parts).

## LiveBrowser.tsx

- Session polling loop — LiveBrowser (LiveBrowser.tsx:301) — connecting; live — `useLiveSession` reads `/v1/tasks/{id}/live/session` on load and every `LIVE_POLL_MS` (3000ms) while an attempt is in flight or being watched.
- Poll backoff on read failure — LiveBrowser (LiveBrowser.tsx:269) — connecting (API blip) — `liveRereadDelay` doubles the poll interval per consecutive failure, capped at 30s; last-known session is kept, never torn down.
- Poll pauses on hidden tab — LiveBrowser (LiveBrowser.tsx:273,379) — any (tab backgrounded) — no reads while `document.visibilityState === 'hidden'`; resumes and re-reads at once on `visibilitychange`.
- Control-route answer drawn before next poll — LiveBrowser (LiveBrowser.tsx:246,312) — after Take/Return/Device/Member actions — `reload(answer)` paints a control route's response immediately rather than waiting for the next poll tick.
- Origin strip verdict/tone — LiveBrowser (LiveBrowser.tsx:435) — live; unknown page; warn; mismatch; refused — one of `unknown/ok/warn/mismatch/refused` with one sentence explaining the page's address safety.
- Blank page notice — LiveBrowser (LiveBrowser.tsx:440) — live, about:blank — "The page is blank... or the gate has not reported the page's address yet."
- Control refusal reasons (plain HTTP / IP literal / cert error) — LiveBrowser (LiveBrowser.tsx:424) — live, ineligible page — disables Take control with an explanation instead of hiding the button.
- Hand-off site-mismatch warning — LiveBrowser (LiveBrowser.tsx:448) — live, hand-off waiting — "This page is X, but the agent said it would ask you to sign in to Y."
- No-site-named warning — LiveBrowser (LiveBrowser.tsx:445) — live, hand-off waiting — "The agent did not say which site it wants you to sign in to."
- Agent-not-using-browser notice — LiveBrowser (LiveBrowser.tsx:506) — live, RUNNING, no agent connection for 60s after first frame — `AgentUnused`: "The agent isn't using this browser yet", links to Open the logs.
- Take control button (header) — LiveControl (LiveBrowser.tsx:589) — live, eligible, nobody/someone else in control — primary button, label "Take control" or "Take control from <email>".
- Take-control confirmation dialog — LiveControl (LiveBrowser.tsx:706) — live, about to take control — modal dialog quoting the agent's hand-off reason, explains the agent waits and every member watching sees the page, warns passwords are painted over.
- Take-control wrong-site re-ask — LiveControl (LiveBrowser.tsx:727) — live, page origin mismatches hand-off's expected site — `alertdialog`, red, requires a second explicit confirm (`confirmed_mismatch`) naming both domains.
- Return control button — LiveControl (LiveBrowser.tsx:640) — member has control (mine) — primary button "Return control", busy state "Returning…".
- View-only state line — LiveControl (LiveBrowser.tsx:666) — view-only, ineligible — "View only" or "<email> is in control" instead of a button.
- Control renewal heartbeat — LiveControl (LiveBrowser.tsx:625) — member has control — renews control every `LIVE_RENEW_MS` (20s) via `renewLiveControl`; control lapses 60s after the last renewal.
- Ask-dialog focus trap and Escape-to-close — useAsk (LiveBrowser.tsx:531) — any modal ask open — focuses first button on open, Tab wraps inside the dialog, Escape closes from anywhere and returns focus to the opener.
- Browser tab pane (main view) — LiveBrowserPane (LiveBrowser.tsx:758) — idle; loading; error; starting; live; closed; finished — composes hand-off banner, origin strip, toolbar, canvas/logs split, and the theater host.
- "Browser not running" notice — LiveBrowserPane (LiveBrowser.tsx:866) — idle, no attempt — "The agent's browser is not running. It starts when the agent is admitted again."
- "Reading the agent's live browser" notice — LiveBrowserPane (LiveBrowser.tsx:869) — loading — shown while the session read is in flight.
- Live-read error notice — LiveBrowserPane (LiveBrowser.tsx:872) — error — "The live browser could not be read: <message>".
- Start-up step progress — LiveBrowserPane (LiveBrowser.tsx:880) — starting (no session yet) — renders `<StartupProgress>` with pod/image/container/browser steps.
- Closed-session note — LiveBrowser (LiveBrowser.tsx:194,885) — disconnected (session closed) — distinguishes a parked hand-off ("Press I'm ready to resume") from an ordinary close ("The recording is kept for seven days.").
- Origin strip line (address + who's driving + badges) — LiveBrowserPane (LiveBrowser.tsx:931) — live — one line: scheme+host (`live-origin`), "Nobody is in control: the agent is driving." / "You are in control." / "<email> is in control.", then `BrowserBadges`.
- "About this address" disclosure — LiveBrowserPane (LiveBrowser.tsx:948) — live, no warning — `<details>`/`<summary aria-label="About this address">ⓘ</summary>` revealing the verdict sentence.
- Gate's browser-closed cover message — LiveBrowserPane (LiveBrowser.tsx:961); gateReason (LiveBrowser.tsx:1761) — live, agent attempt just ended — shows noVNC's desktop-name text ("The agent has finished; its browser is closed.") in the strip (`data-testid="live-cover"`).
- Punycode host decoding/flag — OriginHost (LiveBrowser.tsx:1059) — live, IDN host — shows the decoded host and a "punycode: <raw host>" note via title.
- Registrable-domain emphasis in origin host — OriginHost (LiveBrowser.tsx:1059) — live — subdomain shown plain, registrable domain shown bold.
- Open in theater (banner button) — OpenInTheater (LiveBrowser.tsx:1078) — live, hand-off banner — small button, only rendered inside a `<TheaterHost>`.
- Device size/orientation computation — deviceSize (LiveBrowser.tsx:1097) — live — resolves the reported preset to width/height/scale/mobile, long side across in landscape.
- Device caption line — deviceCaption (LiveBrowser.tsx:1118) — live — `"<Device> · <W>×<H> · <path>"`, "Device not reported" or "path not reported" as fallbacks.
- Device-not-confirmed warning — deviceCaption/frameContradicts (LiveBrowser.tsx:1118,1160) — live, frame orientation disagrees with reported device — "Device not confirmed: the gate reported X (orientation), but the screen it sends is <landscape|portrait>".
- Device fit-to-stage sizing — fitDevice (LiveBrowser.tsx:1174) — live — largest box of the device's proportions that fits the available room both ways, whole pixels, never larger than the room.
- Fit-shape proportion choice — fitShape (LiveBrowser.tsx:1197) — live — fits to the gate's reported framebuffer proportions unless it contradicts the device, else the device's own proportions.
- Draggable logs/browser split — LogSplit (LiveBrowser.tsx:1253) — live or finished, logs open — `role="separator"`, drag with pointer or ArrowLeft/ArrowRight/Home/End keys, 20–75% bounds, 5% step.
- Split position persisted — readSplit/keepSplit (LiveBrowser.tsx:1218,1224,1233) — any — stored in `localStorage` key `swarm.live.logsSplit` so later tabs reopen at the same split.
- Finished-agent browser view — FinishedBrowser (LiveBrowser.tsx:1321) — finished (task terminal) — shows end time, links to Attempts recordings and Artifacts screenshots, the last drawn frame (if watched), logs (if open), and `RecordingsPanel`.
- Last-frame snapshot kept after screen closes — LiveScreen cleanup (LiveBrowser.tsx:1896) — finished, was watching — captures the noVNC canvas via `canvas.toDataURL('image/png')` and shows it as an `<img>` in FinishedBrowser.
- Toolbar (two button groups) — LiveToolbar (LiveBrowser.tsx:1388) — live — left group "The agent's browser" (device menu, member-control checkbox); right group "This view" (◫ logs toggle, ⤢ Theater).
- Logs toggle button — LiveToolbar (LiveBrowser.tsx:1416) — live — `◫ logs`, `aria-pressed`, title "Hide the logs"/"Show the logs beside the browser".
- Device menu dropdown — DeviceMenu (LiveBrowser.tsx:1432) — live, desktop/tablet width — `Device: <preset> ▾` button opening a popover with the device chips/rotate/touch; collapses to the phone "⋯" menu below `usePhoneTables` width.
- "Let any member take control" toggle — MemberControl (LiveBrowser.tsx:1490) — live, submitter only (`you.can_open`) — checkbox bound to `session.open_to_members`; turning on asks for confirmation first, turning off is immediate.
- Member-control confirm dialog — MemberControl (LiveBrowser.tsx:1517) — live, turning member control on — explains any member can take control, persists across later attempts, every take is audited.
- Device chips (preset picker) — LiveDevice (LiveBrowser.tsx:1611,1663) — live, person in control only — one `Button` per preset (`aria-pressed` on the active one, `data-live-device` marks them for focus hand-back), disabled with a why for everyone else or when the gate hasn't reported a device.
- Rotate device — LiveDevice (LiveBrowser.tsx:1682) — live, in control — "⟲ rotate" toggles portrait/landscape via `setLiveDevice`.
- Touch toggle — LiveDevice (LiveBrowser.tsx:1694) — live, in control, mobile preset — "touch: on"/"touch: off", disabled for non-mobile presets or while waiting for confirmation.
- Device-change pending indicator — LiveBrowserPane (LiveBrowser.tsx:897,1023); useDeviceReport (LiveBrowser.tsx:1576) — live, device switch just requested — caption appends "· <preset> asked for; the browser has not applied it yet" (`data-testid="live-device-asked"`) and re-polls every `DEVICE_CONFIRM_MS` (1s) for up to `DEVICE_CONFIRM_FOR_MS` (15s).
- noVNC canvas viewer — LiveScreen (LiveBrowser.tsx:1814) — connecting; connected; reconnecting; failed — mounts `@novnc/novnc` RFB against a ticketed WebSocket, draws into a `<canvas>` inside `.live-screen`.
- Crisp pixel scaling at whole DPR ratios — crispAt (LiveBrowser.tsx:1775); sharpen (LiveBrowser.tsx:2155) — live, connected — sets `canvas.style.imageRendering = 'pixelated'` when the CSS size is a whole multiple of the frame size at the device pixel ratio; otherwise leaves smoothing.
- Reconnect with exponential backoff — liveReconnectDelay (LiveBrowser.tsx:165); retry (LiveBrowser.tsx:2063) — reconnecting — ordinary backoff (1s→∞ doubling to 30s cap) or fast jittered backoff (250ms→4s) after a relay-restart close (RFC 6455 code 1012).
- "Live connection failed" state — LiveScreen (LiveBrowser.tsx:2069,2221) — failed (>5 tries, not a relay restart) — empty-state panel "No picture from the agent's browser" with the failure reason, styled via `EmptyState kind="failed"`.
- "No picture yet" placeholder in frame — LiveScreen (LiveBrowser.tsx:2314) — connecting/reconnecting, no frame drawn yet — `.live-frame-empty` (`data-testid="live-frame-empty"`) shown inside the device frame until the first frame arrives.
- Connection status line under the frame — LiveScreen (LiveBrowser.tsx:2327) — connecting; connected view-only; connected in control (focused/unfocused); reconnecting; failed — e.g. "Connecting to the agent's browser.", "Click the browser to type into it.", "Your keys go to the agent's browser. Click outside it to use the console.", "View only."
- Start-up overlay on the canvas — LiveScreen (LiveBrowser.tsx:2348) — connecting, before first frame — `<StartupProgress overlay hideWhenDone>`.
- Click-to-type hint — LiveScreen (LiveBrowser.tsx:2308) — live, in control, canvas unfocused — "Click to type" hint floats over the canvas.
- Canvas focus follows control / device switch — LiveScreen (LiveBrowser.tsx:1983-2019) — in control — clicking anywhere on the screen focuses noVNC's canvas; a device switch or a canvas replacement (reconnect) hands focus back automatically.
- Keys stay local to the remote browser — keepKey (LiveBrowser.tsx:2240) — in control, canvas focused — `stopPropagation` so console shortcuts (Escape closing the split, `[` folding the list, Theater's `T`/Esc) never fire while typing into the agent's browser.
- Paste forwarded as clipboard text — onPaste (LiveBrowser.tsx:2246) — in control, canvas focused — intercepts a paste event and sends the clipboard text to noVNC's `clipboardPasteFrom`.
- Touch events gated to control+touch-on — gateTouch (LiveBrowser.tsx:2243) — in control with touch enabled — touch start/move/end/cancel only reach the canvas under those conditions; otherwise the page scrolls normally.
- Device frame outline (phone/tablet bezel) — LiveScreen (LiveBrowser.tsx:1795,2277) — live, mobile preset — `.live-frame.is-phone`/`.is-tablet`/`.is-desktop` with a 10px bezel for mobile, 0 for desktop.
- Narrow-tab stacking / floor height — LiveScreen (LiveBrowser.tsx:1806,1958) — phone-width tab (<480px) — the stage is sized to content and floored to the device's width-fit height, capped at the window height.
- Live-connection view-only vs control role/label — LiveScreen (LiveBrowser.tsx:2294) — view-only; in control — `role="application"` + `aria-label="The agent's browser. Click it to type into it."` when controllable, else `role="group"` + `aria-label="The agent's browser, view only."`.
- SaveAsLoginOffer trigger on control return — LiveBrowserPane (LiveBrowser.tsx:998,1045) — control just returned/lapsed, personal workspace — detects this viewer's control ending with nobody in control and offers to save the login.

## LiveThumb.tsx

- Live graph-node thumbnail — LiveThumb (LiveThumb.tsx:579) — connecting; live; reconnecting; no live view; unavailable — a `<canvas>` drawn by a hand-rolled, view-only RFB 3.8 client capped at ~1 frame/second (`THUMB_MIN_INTERVAL_MS`).
- Thumbnail status badge — LiveThumb (LiveThumb.tsx:563,683) — all statuses — `ctl-mark is-live`/`is-unread`/`is-absent`/`is-pending` beside the word "live · ≤1 fps" / "connecting" / "reconnecting" / "no live view" / "live view unavailable".
- Frame-count accessible label — LiveThumb (LiveThumb.tsx:716) — any — `aria-label` on the canvas states the status note plus "Frames shown: N."
- Retry button on persistent failure — LiveThumb (LiveThumb.tsx:692) — unavailable, 3 consecutive failed connections — "Retry" resets failure count and reconnects.
- No-session recheck (no socket spent) — LiveThumb (LiveThumb.tsx:637,642) — no live session yet — stops reconnecting, shows "no live view", and only re-reads the session (`loadLiveSession`, no ticket) every `THUMB_SESSION_RECHECK_MS` (30s).
- Reconnect after socket close — LiveThumb (LiveThumb.tsx:645-651) — reconnecting — retries after `THUMB_RECONNECT_MS` (5s), capped at `THUMB_MAX_FAILURES` (3) tries with zero frames before giving up.
- Start-up bar on the card — LiveThumb (LiveThumb.tsx:711) — not yet live — `<StartupBar>` shown until the first frame; disappears once live.
- "Open Browser tab" link — LiveThumb (LiveThumb.tsx:720) — thumbnail in list/graph — links to the agent's own Browser tab address.
- "⤢ Theater" link — LiveThumb (LiveThumb.tsx:723) — thumbnail in list/graph — links to `theaterPath(taskId, workflowId)` (`?theater=1&wf=<id>`), `aria-label="Open in theater"`.
- Shows only for RUNNING live-browser steps — showsLiveThumb (LiveThumb.tsx:97) — RUNNING + `claude-code-browser` profile — gates whether a workflow step's node draws the card at all.
- Live-steps list for theater prev/next — workflowLiveAgents (LiveThumb.tsx:113) — theater mode, workflow context — builds the ordered `LiveAgentRef[]` (`needsYou` from `task.human_wait`) that the theater's prev/next buttons walk.
- RFB thumbnail protocol client — RfbThumb (LiveThumb.tsx:312) — live — hand-written RFB 3.8 state machine: RAW + DesktopSize encodings only, sends SetPixelFormat/SetEncodings/FramebufferUpdateRequest, paces one outstanding request at a time.
- Thumbnail canvas draw via ImageData — LiveThumb sink (LiveThumb.tsx:604-618) — live — `ctx.putImageData(new ImageData(pixels, w, h), 0, 0)`, resized via `canvas.width/height` on each DesktopSize rect.

## LiveToolMenu.tsx

- Phone "⋯" overflow menu — LiveToolMenu (LiveToolMenu.tsx:20) — phone width (`usePhoneTables`) — collapses the device chips/rotate/touch/logs toggle into one "⋯" button (`aria-label="Browser view options"`); on wider viewports renders its children inline unchanged.
- Menu dismiss on outside click / Escape — LiveToolMenu (LiveToolMenu.tsx:26,53) — phone, menu open — closes on a pointerdown outside the menu box or on Escape inside it.

## BrowserBadges.tsx

- Browser-tool badge — BrowserBadges (BrowserBadges.tsx:253) — live — shows "Auto", "Auto: <tool>", "Playwright"/"dev-browser"/"Jev", or "Jev off · Playwright" (warn tone) when Jev was picked but fell back.
- Jev-fallback reason card — BrowserBadges (BrowserBadges.tsx:174,262) — live, Jev selected but disabled — explains `no_key` (add a TypeSafe key in Config › Secrets), `not_installed`, `not_selected`, or `no_route` (no tailnet egress).
- Egress/proxy badge — BrowserBadges (BrowserBadges.tsx:268) — live — "Direct", "Residential", "Residential · <COUNTRY>", "Proxy failing" or "Proxy off" (tone ok/warn/plain by state).
- Jev-on badge — BrowserBadges (BrowserBadges.tsx:286) — live, Jev enabled — "Jev" note badge; card says it sends page text to TypeSafe and whose key it uses (user/tenant).
- QA-identity badge — QaIdentityBadge (BrowserBadges.tsx:191) — live, task submitted with a QA identity — "Signed in as QA identity (<identity>)"; drawn even with no `browser` status object.
- Badge hover/focus card — Badge (BrowserBadges.tsx:119) — any badge — shows its detail card on hover (120ms delay) or immediately on keyboard focus; Escape or blur closes it; `role="tooltip"`.
- Last browser-tool action timestamp — lastAction (BrowserBadges.tsx:169) — live, tool used at least once — "Last browser action: <Tool>, <time ago>." shown inside each badge's card.
- Defensive parse of the `browser` status object — browserStatusOf (BrowserBadges.tsx:78) — live, older API — returns null (draws nothing) if the shape is incomplete, rather than drawing a partially-wrong badge.

## Theater.tsx (live-browser / theater-mode parts)

- Theater host wrapper — TheaterHost (Theater.tsx:431) — live; finished (view only) — wraps the Browser tab body, owns open/closed state, `T`-key listener, and the self-read live-agents list used when no explicit list is passed.
- Open theater via `⤢ Theater` button — TheaterButton (Theater.tsx:508) — live, inside a TheaterHost — `aria-label="Open theater (T)"`, title "Theater (T)", `data-theater-button`.
- Open theater via `T` key — TheaterHost (Theater.tsx:483) — live, theater closed, focus not in a text field or the remote screen — opens without Ctrl/Meta/Alt or key repeat.
- Theater address sync (`?theater=1`) — syncTheaterQuery (Theater.tsx:420) — theater opened/closed on a `/browser` tab — replaces (not pushes) the URL so reload/copy-link preserves theater state; keeps other query params (e.g. `wf`).
- Scroll/focus restore on close — TheaterHost (Theater.tsx:446,471) — theater closing — restores every scrolled ancestor's `scrollTop` and refocuses the `⤢` button (or whatever had focus before opening).
- Theater full-screen dialog — Theater (Theater.tsx:555,814) — theater mode — `role="dialog" aria-modal="true" aria-label="Theater: <agent name>"`, portalled to `document.body`, class `th-root`, `data-theater`.
- Borrowed screen node (no second connection) — Theater (Theater.tsx:574-587) — theater mode — moves the Browser tab's existing noVNC-owned DOM node into the theater's stage and back on close; opens no new socket or ticket.
- Esc closes theater (not the tab behind it) — Theater (Theater.tsx:609,623,646) — theater mode, focus outside the remote screen — stops propagation so the agent-split's own Escape handler doesn't also fire; a key typed into the remote screen is left alone.
- Focus trap (Tab stays inside) — Theater (Theater.tsx:620,623) — theater mode — Tab wraps from last focusable control back to the first and vice versa, except inside the remote screen.
- Focus pulled back into the theater — Theater (Theater.tsx:597) — theater mode, focus escapes to the page behind — refocuses the root unless the new focus is inside a dialog/alertdialog/toast portal.
- Native fullscreen + landscape lock on phone — Theater (Theater.tsx:657) — phone (`pointer: coarse`, ≤640px) — calls `root.requestFullscreen()` then `screen.orientation.lock('landscape')`; leaving fullscreen by the system gesture closes the theater.
- "LIVE / REPLAY / ENDED" pill — Theater (Theater.tsx:826) — live; replaying a recording; session ended — `.th-pill-live` with `is-replay`/`is-ended` modifiers; `data-testid="theater-live-pill"`.
- "You are in control" / "Needs you" flag — Theater (Theater.tsx:833) — member has control; hand-off waiting — `.th-flag.is-yours` or `.th-flag`.
- Prev/next live agent navigation — Theater (Theater.tsx:840-857) — theater mode, multiple live agents — `‹ prev agent` / `next ›` buttons, disabled with a reason when there is none or the list hasn't loaded; needs-you agents sort first (`orderLiveAgents`).
- "Esc · back to tab" button — Theater (Theater.tsx:858) — theater mode — explicit close button mirroring the Escape key.
- Left column tabs: Hand-off / Timeline / Logs — Theater (Theater.tsx:806-909) — theater mode — `role="tablist"`; Hand-off tab only present while `handoff` is set and auto-selected when one arrives; each panel mounts once and stays mounted (hidden) after first view.
- Hand-off checklist panel — Theater (Theater.tsx:893) — theater mode, agent waiting for a human — delegates to `HandoffChecklist` with `youControl`, `stripOk` (origin matches expected site), Return-control and Take-control wiring.
- Timeline panel — Theater (Theater.tsx:904) — theater mode — delegates to `TheaterTimeline`.
- Logs panel — Theater (Theater.tsx:905) — theater mode — reuses the tab's own `logs` slot, or "The tab has no log view for this agent."
- Origin strip shown dimmed during replay — Theater (Theater.tsx:913) — replaying a recording — strip still reflects the LIVE page, tagged "Live now, not the replay:".
- Replay timestamp tag — Theater (Theater.tsx:919) — replaying — "Replay · <clock time>" (`data-testid="theater-replay-tag"`).
- Recording scrubber — Theater (Theater.tsx:1011-1037) — theater mode — `<input type="range">` over segments laid out at one minute (`SEGMENT_SECONDS`) each, labelled via `aria-valuetext`; `data-testid="theater-scrubber"`.
- "◀◀ replay" button — Theater (Theater.tsx:1001) — recording available — seeks to position 0 and starts playback.
- "live ●" button — Theater (Theater.tsx:1039) — replaying — returns to the live view (`setPlaying(null)`), `aria-pressed` reflects whether live is current.
- Replay video element — Theater (Theater.tsx:925-943) — replaying — `<video autoPlay muted playsInline>` streaming one recorded segment at a time, auto-advances to the next segment on end.
- Recording listing re-read while live — Theater (Theater.tsx:684-718) — live session, theater open — re-reads `/v1/tasks/{id}/live/recordings` every `RECORDINGS_POLL_MS` (60s) so newly closed segments appear without reopening the theater.
- "No recording yet" / listing failure messages — Theater (Theater.tsx:1048-1057) — no segments; listing failed; segments belong to another tenant — distinct messages for each case.
- DEVICE sidebar — Theater (Theater.tsx:950-964) — theater mode — shows current device via `DeviceOutline` + text ("Not reported" or "<preset> · <orientation>[ · touch]"), and re-hosts the tab's own `LiveDevice` controls (one implementation of who may switch).
- Device outline icon — DeviceOutline (Theater.tsx:211) — theater mode — SVG-less `<span>` outline shaped desktop/phone/tablet/unknown, rotated when the device is landscape (`is-landscape`).
- OTHER LIVE sidebar list — Theater (Theater.tsx:965-997) — theater mode, other live-browser agents exist — lists each agent with its device outline, name (clickable to navigate), short task id, and a "Needs you" flag; "Reading the live browser agents…" / failure message while unread.
- Self-read live-agents list — useLiveAgents (Theater.tsx:141) — theater mode, no explicit `agents` prop — polls `loadTasks` (`view=summary`) every `LIVE_AGENTS_POLL_MS` (30s) and filters to `liveBrowserRows`.
- Device chip list recolored to match outline — Theater (Theater.tsx:777-797) — theater mode — re-labels the tab's horizontal device chips as a vertical list with per-chip outline (`data-outline`) and orientation markers.
- Control button re-hosted in footer — Theater (Theater.tsx:1059) — theater mode — the tab's own `control` slot (Take/Return control) is rendered in the theater's bottom bar, so eligibility stays decided in one place.
- "Browser has not connected yet" placeholder — Theater (Theater.tsx:945) — live, no screen node yet — shown before noVNC has mounted anything to borrow.
- Start-up overlay in theater stage — Theater (Theater.tsx:947) — live, before first frame — `<StartupProgress overlay hideWhenDone>` anchored to the borrowed screen.

Files: `Workflows.tsx` (6940 lines), `workflowlist.ts` (460 lines).

## WorkflowsScreen — routing, address, board-wide stores

- List vs. one-workflow page by address — WorkflowsScreen (Workflows.tsx:369) — workflows list; one workflow page — routes between `/workflows` and `/workflows/<id>` purely from `query.wf`, no separate router.
- Workflow page title from Recent name, else id — WorkflowsScreen (Workflows.tsx:412) — one workflow page — `pageTitle` is the remembered label (`useRecentName`) or the raw workflow id until one is known.
- Breadcrumb carries the list's filters, no duplicate back-link — WorkflowsScreen (Workflows.tsx:413-417) — one workflow page — `Work ▸ Workflows · Failed ▸ <name>`; the page itself draws no `‹ Workflows` link.
- New history entry per workflow/view change — WorkflowsScreen/go (Workflows.tsx:389-398) — one workflow page, switching workflow via same-step scrubber or tab — `pushState` so Back returns to the prior workflow/view instead of replacing it (filters/sort still use `replaceState`).
- Changes tab's step filter and open file ride the address — WorkflowsScreen/chooseChanges (Workflows.tsx:399-403) — Changes tab — `?step=&file=` written only on that tab, dropped by every other choice.
- 10s poll while any workflow is running, none once finished — workflowPoll (Workflows.tsx:355-357) — workflows list; one workflow page — `Screen`'s poll pauses when the tab is hidden and reads at once on return.
- Page's own skeleton with disabled head and real tabs — WorkflowsScreen (Workflows.tsx:435-438) — one workflow page, loading — `WorkflowPageSkeleton` so the board lands under the tabs, not above generic rows.
- List's own skeleton with real toolbar/table head — WorkflowsScreen (Workflows.tsx:467-469) — workflows list, loading — `WorkflowListSkeleton`.
- Empty workflows list points at Submit form — WorkflowsScreen (Workflows.tsx:471-483) — workflows list, zero rows ever — explains what a workflow is and links "Submit a workflow" to `#work/new-workflow`.
- "No workflows" on a page with nothing found — WorkflowsScreen (Workflows.tsx:439) — one workflow page, nothing in the read — "Individually submitted agents appear under Agents."
- Single `?` help topic for the whole screen — WorkflowsScreen (Workflows.tsx:428, 464) — list and page — `help="absent-vs-zero"`, one topic for every absent figure on the screen.
- Recent-name live update — useRecentName (Workflows.tsx:494-503) — list and page — re-reads the Recent-workflows store on `RECENT_WORKFLOWS_EVENT` so the page/breadcrumb title updates the moment a name is learned.
- Page count note: shape · progress · owner · started — pageSummary (Workflows.tsx:516-533) — one workflow page — `4 steps in 3 stages (1 → 2 → 1) · 1 of 4 steps done · by <PersonName> · started 8m ago`.
- UTC timestamp, same-day seconds vs. other-day date — utcStamp (Workflows.tsx:544-555) — anywhere a timestamp is shown — `02:20:01Z` if same UTC day as now (kept to the second so two rows a second apart still sort visibly), else `Oct 27 02:20Z`.
- Head start time in UTC with age title — workflowStartUtc (Workflows.tsx:558-567) — one workflow page head — reuses `workflowStartText`'s never/unread/at kinds but prints the instant via `utcStamp`.
- Started phrase wording per kind — startedPhrase (Workflows.tsx:576-580) — one workflow page head — "started 8m ago" / "started — not read" / the bare "never started" text, never "started start not read".
- Top-of-list running/parked/waiting/unknown/finished/failed counts — listSummary (Workflows.tsx:592-619) — workflows list — e.g. "2 running · 1 parked · 1 waiting · 31 finished · 1 failed · of the 100 newest"; parked and waiting (READY/QUEUED/SUBMITTED) are split out of "running", and each state this read could not derive is named in its own words.
- One workflow's own read, not the 100-row board — loadWorkflowPage (Workflows.tsx:632-649) — one workflow page — `GET /v1/workflows/{id}` directly; a 404 is said as one; any other failure falls back to the board.
- Page read joined into board context for the sibling scrubber — joinWorkflowPage (Workflows.tsx:657-710) — one workflow page — merges the page's own workflow/tasks into the board read (or vice versa) so the same-step scrubber still has every other workflow to walk.
- One shared attempt-telemetry read for head/table/nodes — useWorkflowUsage (Workflows.tsx:735-792) — one workflow page — single `loadWorkflowUsage` call, deduped and capped, with the page's own workflow's steps always inside the cap; re-reads only when the set of task ids changes (not on every 10s poll) so figures don't flicker.
- Per-board UI state survives a reload-remount — useBoardStores (Workflows.tsx:283-343) — board/page — which stages are expanded, each workflow's zoom choice, the one picked step board-wide, and each workflow's recorded cancel answer all live above `Screen`'s remount key.

## Board & WorkflowCard — one workflow's card (collapsed and open)

- States-unread caveat banner, drawn only when needed — Board (Workflows.tsx:902) — list/page, partial task read — one-line chrome strip wrapping `StatesUnavailable`.
- Grid of workflow cards for the focused workflow(s) — Board (Workflows.tsx:915) — workflows list; one workflow page — maps `shown` workflows to `WorkflowCard`.
- Selection scoped to its own card — Board (Workflows.tsx:919) — any — inspector only draws on the card whose workflow id matches the picked step.
- "States could not be read" status mark — StatesUnavailable (Workflows.tsx:998-1004) — list/page, partial task read — `role="status"` `aria-label` states steps/order/dependencies are correct but states are not shown; nothing below is evidence either way.
- Progress text when no server census exists — rollupLine (Workflows.tsx:1077) — one workflow, no rollup — `"${total} steps"`, untrustworthy.
- Progress text for a partial census — rollupLine (Workflows.tsx:1089) — one workflow, partial census — `"${n} of ${total} steps unread"`.
- Full outcome rollup line — rollupLine (Workflows.tsx:1114) — one workflow, census complete — `"${done}/${total} done"` joined with failed/dead-lettered/cancelled/not-started clauses.
- Terminal "all steps ended" summary — rollupLine (Workflows.tsx:1125-1132) — workflow finished — `"All N steps ended: X succeeded, Y failed…"`.
- Non-terminal progress sentence for screen readers — rollupLine (Workflows.tsx:1139) — workflow running — e.g. "3 of 6 steps done, 1 failed."
- Failures grouped by cause — failedClauses (Workflows.tsx:1159) — one workflow with failures — `"4 failed: input collision"` per cause group.
- Bare failure count when causes don't fully account — failedClauses (Workflows.tsx:1160-1161) — one workflow, unexplained failures — `"${n} failed"`.
- Pull-request wait tag on the card — PullRequestWaitTag (Workflows.tsx:1185-1189) — workflow waiting on / closed-unmerged PR — `<span className="tag wait">`.
- "Stop requested" tag while cancel is pending — cancelPending (Workflows.tsx:1199-1208) — workflow with cancel requested, not yet terminal — gates the `CANCEL_PENDING_WORD` tag.
- "Not checked" drift fact list — StateDrift (Workflows.tsx:1238-1254) — workflow card, drift unreadable — `stored as X` / `steps: not checked` with a reasoned `aria-label`.
- Stored-vs-derived state drift facts — StateDrift (Workflows.tsx:1256-1279) — workflow card, stored/derived states disagree — `stored as`/`steps` pair, plus "stored copy corrected" when `drift.repaired`.
- "Why step X failed" arrival lead section — WorkflowCard (Workflows.tsx:1504-1518) — one workflow page, arriving on a failed step — `<section aria-label="Why step {id} failed">` with full failure text and an "Open the step's card: agent, logs" button.
- Lead-banner visibility suppresses duplicate failure text in the step card — WorkflowCard (Workflows.tsx:1503, 1519) — failed workflow page — IntersectionObserver-gated.
- First click on an arrival pick opens the card instead of toggling it — WorkflowCard/pickStep (Workflows.tsx:1423-1444) — workflow page, step arrives pre-picked — scrolls/opens and focuses its close control.
- Return focus to the originating node on panel close — WorkflowCard (Workflows.tsx:1469-1485) — step panel open, closed via ×/Escape — refocuses the node/row button for that step.
- Times facts (started/submitted) with UTC+age titles — WorkflowCard (Workflows.tsx:1553-1564) — one workflow card, not on page — two `.ctl-fact` entries.
- Census fact line in the open card — WorkflowCard→CensusFact (Workflows.tsx:1565, 1827-1830) — one workflow card, not on page — "progress" fact showing `roll.text`.
- Stored-state drift display in card — WorkflowCard→StateDrift (Workflows.tsx:1571) — card only, not page.
- Dispatch/publish line on every card — WorkflowCard→WorkflowDispatchLine (Workflows.tsx:1572) — any workflow card.
- View bar (tabs + stray/unreadable marks), drawn only when non-empty — WorkflowCard (Workflows.tsx:1579-1586) — workflow card — `ViewControl`, `StrayMark`, `UnreadableMark`.
- Graph view with a side inspector panel — WorkflowCard (Workflows.tsx:1599-1628) — view=graph — `WorkflowGraph` plus `WfPhoneStages` on phone (page only), plus `StepPanel` beside it when a step is picked.
- Table/Timeline view with docking vs. under-row panel — WorkflowCard (Workflows.tsx:1641-1663) — view=table/timeline — `StepPanel` docked beside the table when not narrow, else inline under the row.
- Section state classes `is-open`/`has-pr`/`is-page` — WorkflowCard (Workflows.tsx:1543-1546) — card chrome.
- Narrow/dock breakpoint via matchMedia — useNarrow/subscribeNarrow/atNarrowWidth (Workflows.tsx:1677-1691) — Table/Timeline view, viewport < 1100px — decides docked vs. under-row panel.
- Measured split-width docking thresholds — DOCK_MIN_PX (Workflows.tsx:1706) — workflow page, Table (1392px)/Timeline (1112px) — overrides the viewport breakpoint with the actual measured split-column width.
- Live box-width measurement — useBoxWidth (Workflows.tsx:1709-1721) — any width — ResizeObserver feeds the narrow calc.
- Step details panel (`aside`), self-focusing, Escape closes — StepPanel (Workflows.tsx:1748-1763) — step picked — `aria-label="Step details"`.
- Pull-request link with explanatory title — PullRequestLink (Workflows.tsx:1773-1776) — PR href is http(s) — `<a>PR #{number}</a>`, "its current state is not read here."
- Pull-request non-link fallback — PullRequestLink (Workflows.tsx:1777-1781) — recorded PR URL isn't http(s) — plain `<span>` explaining why it isn't a link.
- Stage-band chip tone — chipTone (Workflows.tsx:1786-1787) — stage band chip — tints only live/park/bad states, leaves others neutral.
- Step brand mark — LookMark (Workflows.tsx:1799-1805) — graph node, stage band, list row — `NamedMark`; forces amber "warn" hue for an unread state.
- Stage band per-count CSS modifier — bandClass (Workflows.tsx:1811) — stage band — `is-unknown` or `is-${hue}`.
- Collapsed topology shape, as text only (deliberately no mini-map) — Shape (Workflows.tsx:1856-1862) — collapsed stage/row — `"1 → 5 → 1"` with `title=shape.label`.
- Runner-profile mix chips, folding to "+N" — Mix (Workflows.tsx:1911-1927) — workflow card — up to `MIX_NAMED_MAX` named chips, full list in `title`.
- Mix column measures its own width to decide folding — Mix (Workflows.tsx:1893-1902, 1909) — any width — ResizeObserver on the chip row.
- Spend "reading…" state — Spend (Workflows.tsx:1954-1957) — cost read in flight — short "…" / long "cost: reading…".
- Spend "not reported" absent state — Spend (Workflows.tsx:1971-1975) — no step reported a cost — distinguishes "no agent joined" vs. "none of N joined steps reported".
- Spend dollar figure with partial-coverage note — Spend (Workflows.tsx:2000-2026) — at least one step reported cost — `money(spend.usd)` plus "${covered} of ${due} reported" when partial.
- Dollar formatting — money (Workflows.tsx:2037) — any spend figure — delegates to `usd()`.
- Dispatch line computed from joined tasks — WorkflowDispatchLine (Workflows.tsx:2066-2078) — workflow card.
- Dispatch "not reported"/"no agent joined" absence — WorkflowDispatch (Workflows.tsx:2111-2123) — tasks joined but no dispatch read, or none joined — two distinct `aria-label`s.
- Dispatch strategy + "opens" consequence fact — WorkflowDispatch (Workflows.tsx:2139-2155) — dispatch read — `<code>{strategy}</code>` plus short consequence phrase.
- Integrator agent link — WorkflowDispatch (Workflows.tsx:2159-2176) — strategy="integrate" with an integrator task — `<a href="#work/task/{id}">`, `aria-label="Open the integrator agent {id}"`.
- PR fact inside dispatch facts — WorkflowDispatch (Workflows.tsx:2180-2185) — PR present — reuses `PullRequestLink`.
- Dispatch "is-none" modifier — WorkflowDispatch (Workflows.tsx:2137) — dispatch pushes nothing.
- Stray-input mark collection — straysOf (Workflows.tsx:2204-2213) — workflow with staged files no edge carries — feeds `StrayMark`.
- Malformed-input mark collection — unreadableOf (Workflows.tsx:2220-2229) — workflow with malformed staged-input entries — feeds `UnreadableMark`.
- Attempts cell — never-ran absence — attemptsCell (Workflows.tsx:2237) — step unstarted — `NEVER_RAN`.
- Attempts cell — state-unread absence — attemptsCell (Workflows.tsx:2238) — step state unknown — `STATE_UNREAD`.
- Attempts cell — measured count with over-ceiling note — attemptsCell (Workflows.tsx:2240-2245) — step has a task — `"N of M"`.
- Attempts-over-ceiling computation — attemptsOver (Workflows.tsx:2260) — step exceeded `max_attempts`.
- Per-step row model for Table/Timeline/inspector — stepRows (Workflows.tsx:2284-2351) — table/timeline/inspector — combines look, times, attempts, cost, tokens, result, inputs, why, and sort keys in graph order.

## Step inspector panel — StepPanel, InspectorSlot, StepCardExtras, verdicts, merges

- Table/Timeline view selector for one workflow's steps — WorkflowSteps (Workflows.tsx:2399) — table view — Table when `view === 'table'`, else Timeline.
- Stage filter chip with removable clear control — WorkflowSteps (Workflows.tsx:2409-2418) — table view, opened from a band count — "stage N · word" chip + "X of Y steps", `removeLabel="Clear the stage filter"`.
- "No steps" empty state — WorkflowSteps (Workflows.tsx:2398) — workflow with zero steps.
- Sample-note head row on step table — WorkflowSteps (Workflows.tsx:2433) — table view, usage partially sampled — `SampleNote` in the table's head row.
- Workflow spend total row under the table — WorkflowSteps (Workflows.tsx:2435-2437) — table view — "Total" `Spend` summed from row figures.
- Timeline with a time axis — WorkflowSteps (Workflows.tsx:2442-2454) — timeline view — `axisOf` built from row times, now, and `created_at`.
- 1Hz clock scoped to the mounted view — WorkflowSteps/useNow (Workflows.tsx:2389, 2781-2788) — table/timeline view — ticks only while mounted.
- Visibility-tracking hook, default "not in view" — useSeen (Workflows.tsx:2465-2478) — used by the failure banner — IntersectionObserver-based.
- Inspector remounts per (workflow, step) — InspectorSlot (Workflows.tsx:2487) — step panel open — fresh `StepInspector` so attempt state resets on step change.
- Stale-selection guard — InspectorSlot (Workflows.tsx:2528) — step panel open, step vanished from reload — renders nothing instead of a stale inspector.
- "Workflow 1 of 1" sibling-scrubber fallback — InspectorSlot (Workflows.tsx:2533-2536) — no board siblings — scrubber shows disabled both-ways nav.
- Step's Changes link with file-change counts — InspectorSlot (Workflows.tsx:2544-2550) — step panel open, step has a task — links to `/workflows/:id/changes?step=...`.
- "Why it failed" cause panel — InspectorSlot (Workflows.tsx:2578-2592) — step panel open, failed step — full text, or "Said in the banner above the graph." if that banner is visible.
- Step card extras only on the workflow page — InspectorSlot (Workflows.tsx:2593) — page's step panel — `StepCardExtras` below the inspector body.
- "Stop step" action group — StepCardExtras (Workflows.tsx:2634-2654) — page step panel, task exists — `role="group" aria-label="Stop step"` wrapping a `StopRun` "Stop" button.
- Review verdict card — StepCardExtras (Workflows.tsx:2655) — page step panel, verdict recorded — `VerdictCard`.
- "Review verdict … not read yet" pending sentence — StepCardExtras (Workflows.tsx:2656-2663) — step reviewed but gating step hasn't run — `.wf-cell.is-absent` "not read yet".
- Merge step card (MS4) — StepCardExtras (Workflows.tsx:2664) — page step panel, structured merge data — `MergeStepCard`.
- Legacy merge checklist card — StepCardExtras (Workflows.tsx:2665) — merge task without MS4 card — `MergeCard`.
- Await-input human-in-the-loop card — StepCardExtras (Workflows.tsx:2668-2670) — step's runner_profile is AWAIT_INPUT_PROFILE and has a task — `AwaitInputCard`, reloads on answer.
- Review verdict pill (MERGE/NOT_YET/other tone) — VerdictCard (Workflows.tsx:2681-2691) — step panel, verdict present — `aria-label="Review verdict"`, shows `readBy` and optional file.
- Verdict severity groups (Blocker/Major/Minor/Not graded) — VerdictCard (Workflows.tsx:2693-2704) — verdict has findings — count and plain-text findings per group.
- Verdict footer disclaimer — VerdictCard (Workflows.tsx:2705-2711) — explains findings are the agent's own words; notes a dropped-finding count.
- Merge check glyphs (✓ ✕ ⏸ —) — CHECK_GLYPH (Workflows.tsx:2716-2721) — merge card — passed / failed / waiting / not_read.
- Merge outcome/refusal header — MergeCard (Workflows.tsx:2728-2736) — step panel, merge card — `aria-label="Merge checks"`; danger tone on refusal.
- Merge headline with refusal message — MergeCard (Workflows.tsx:2737-2742) — merge card with headline.
- Merge checklist with per-check state/message/code — MergeCard (Workflows.tsx:2743-2759) — merge card with checks — code shown only for failed/not_read.
- Merge footer: did this step merge the PR — MergeCard (Workflows.tsx:2760-2767) — three-way sentence (not recorded / merged / did not merge).

## Graph view — WorkflowGraph, stage bands, step nodes, minimaps, zoom

- 1Hz live clock scoped to the open canvas — useNow (Workflows.tsx:2781-2788) — graph/table/timeline/inspector open.
- Stage key / DOM id generation — stageKey (Workflows.tsx:2808-2810), stageDomId (Workflows.tsx:2815-2817) — graph stage bands — for the expansion store and `aria-controls`.
- Zoom control segmented buttons (Auto/Figures/Details/Names) — ZoomControl (Workflows.tsx:2853-2897) — graph view — eyebrow "Each step shows", descriptive titles per option.
- Zoom "Auto" option names the resolved tier — ZoomControl (Workflows.tsx:2869-2873) — graph view, zoom=auto — title states which tier Auto currently resolves to.
- Zoom tier labels/titles — TIER_LABEL/TIER_TITLE (Workflows.tsx:2905-2925) — graph view — Figures/Details/Names with full field-list descriptions.
- Zoom-dropped-fields notice — ZoomNotice (Workflows.tsx:2961-2980) — graph view, chosen/auto tier hides fields — states what's hidden and how to get it back.
- Horizontal minimap of the canvas — Minimap (Workflows.tsx:3021-3081) — graph view, canvas wider than viewport — `role="img"` SVG, click/drag to jump scroll position.
- Minimap "not yet measured" fallback label — Minimap (Workflows.tsx:3044-3046) — before layout measured.
- Minimap viewport rectangle overlay — Minimap (Workflows.tsx:3069-3077) — canvas wider than viewport.
- Minimap band failure highlighting — MiniMarks (Workflows.tsx:3087-3101) — a band contains a failed step — `.wf-mini-band.is-bad`.
- Minimap node tint by step state — MiniMarks (Workflows.tsx:3102-3113) — `.wf-mini-node` tinted via `lookClass(stepLook(...))`.
- Scroll-port resolver (nearest scrolling ancestor) — scrollPortOf (Workflows.tsx:3135-3142) — graph view.
- Scroll port bounding box — portBox (Workflows.tsx:3145-3151) — graph view.
- Vertical minimap (top-to-bottom) — VerticalMinimap (Workflows.tsx:3169-3281) — canvas taller than screen — sticky `role="img"` SVG map.
- Vertical minimap drag-to-scroll via pointer capture — VerticalMinimap (Workflows.tsx:3255-3272) — vertical minimap shown.
- Vertical minimap viewport rectangle overlay — VerticalMinimap (Workflows.tsx:3275-3277) — canvas taller than viewport.
- Vertical minimap "unmeasured" label variant — VerticalMinimap (Workflows.tsx:3244-3249) — before layout measured — visible-with-offset / fully-scrolled-off / unmeasured.
- Critical-path caption, unmeasured chain — CriticalPathNote (Workflows.tsx:3305-3338) — workflow running/not fully measured — names which steps lack measured duration; cycle-detection and ended-workflow wording.
- Critical-path caption, measured chain — CriticalPathNote (Workflows.tsx:3345-3353) — fully measured — "longest chain · Xm Ys run time" plus the chain of step ids joined by "→".
- Per-stage "entered state" timing probe — useEnteredAt (Workflows.tsx:3404-3443) — graph view, steps PARKED/LEASED/DISPATCHED — reads each waiting task's events page (capped at 24/poll) to compute when it entered its current state.
- Step card note shortening ("waiting on X" vs. "awaits parent(s)") — cardNote (Workflows.tsx:3466-3478) — graph view, note mentions DEPENDENCY_INCOMPLETE — names pending parents if they fit the card width, else falls back to generic text.
- Zoom strip composition (control + notice + minimap + edge key) — WorkflowGraph (Workflows.tsx:3878-3894) — graph view, zoomable or has edges.
- Critical path note placed outside the zoom strip — WorkflowGraph (Workflows.tsx:3897-3899) — graph view, has edges.
- "No steps" guard at the graph level — WorkflowGraph (Workflows.tsx:3823-3825) — zero dependency levels.
- Dependency-levels rail with sticky captions ("starts"/"then ×N") — WorkflowGraph (Workflows.tsx:3916-3923) — graph view — `<ol aria-label="Dependency levels">`.
- Single-scroller "fits-x" mode — WorkflowGraph (Workflows.tsx:3859, 3901) — canvas+rail fit the viewport — page scrolls instead of a nested scrollbar.
- Zoomable-strip visibility heuristic — WorkflowGraph (Workflows.tsx:3860-3864) — strip shown only if zoom overridden, tier isn't full "figures", there are collapsed bands, or canvas overflows the column.
- Graph narrows automatically when the step panel docks beside it — WorkflowGraph (Workflows.tsx:3584-3591) — step picked, zoom=auto — recomputes layout against a narrower column.
- Auto-open the picked step's folded stage on arrival — WorkflowGraph (Workflows.tsx:3617-3621, 3709-3718) — a pick arrives inside a collapsed/band stage — opens and commits the stage, distinguishing page-arrival pick vs. reader pick.
- Auto-scroll to roots on mount / to the opened stage — WorkflowGraph (Workflows.tsx:3735-3818) — mount, or a stage just opened — centers/aligns scroll with a "no card straddles the left edge" correction loop.
- Hover/focus edge lighting — WorkflowGraph (Workflows.tsx:3525, 3629-3631) — hovering/focusing a node — lit edges `is-lit`, others `is-faded`.
- Critical-path edges drawn heavier — WorkflowGraph (Workflows.tsx:3653-3656) — measured critical path.
- Live-browser card height reservation — WorkflowGraph (Workflows.tsx:3569-3576) — a RUNNING step on a live-browser profile — reserves `LIVE_CARD_H` so the thumbnail doesn't overlap.
- Settled-step stop-strip removal — WorkflowGraph (Workflows.tsx:3557-3564) — step's task is terminal — excludes it from reserving a Stop-control strip.
- Edge arrowhead marker, one shared per workflow — WorkflowGraph (Workflows.tsx:3939-3950) — graph view.
- Edge halo under-layer so crossing lines read as "under" cards/edges — WorkflowGraph (Workflows.tsx:3976-3985) — graph view.
- Edge strokes by kind + critical-path flag — WorkflowGraph (Workflows.tsx:3986-4009) — graph view — order/staged/declared classes, `is-critical`.
- Stage band wrapping for wide stages — WorkflowGraph (Workflows.tsx:4033-4070) — wide stage — disclosure container vs. bare fragment for narrow stages.
- Stage bands painted above nodes, below the sticky rail — WorkflowGraph (Workflows.tsx:4074-4088) — graph view.
- Vertical minimap alongside the scrollable canvas — WorkflowGraph (Workflows.tsx:4092) — graph view.
- Stage band toggle with full accessible sentence — StageBand (Workflows.tsx:4222-4254) — stage card (collapsed wide stage) — `aria-label` concatenates census sentence, hold clause, failure cause, picked-step note, and "Activate to collapse/draw all N steps."
- Stage band step count + id range — StageBand (Workflows.tsx:4237-4240) — "N steps" and `first … last` ids.
- Stage band state-mix bar — StageBand (Workflows.tsx:4241) — embeds `MixBar`.
- Stage band hold line — StageBand (Workflows.tsx:4242-4246) — leased/running or queued/parked steps present — e.g. "holds 3 slots · 2 waiting hold none · 1 not read".
- Stage band caret text (collapse/show all N) — StageBand (Workflows.tsx:4249-4253).
- Stage band failure/unread/pick/expanded styling — StageBand (Workflows.tsx:4174-4188) — `has-unread`, `has-failure`, `holds-picked`, `is-opened`.
- Stage band failed-first count chips, clickable to Table — StageBand (Workflows.tsx:4255-4274) — opens Table filtered to that stage+state when `onCount` is supplied.
- Stage band failure-cause line — StageBand (Workflows.tsx:4279-4283) — a failure present — commonest cause plus "(+N other causes)".
- Stage band named step chips with overflow — StageBand (Workflows.tsx:4288-4299) — up to `BAND_CHIPS` (5) chips (failed/holding/parked), "+N more", bare "picked" label when the picked step isn't shown.
- Stage band header stays readable while scrolled mid-band — StageBand (Workflows.tsx:4204-4209) — canvas scrolled — padding compensation so toggle/counts/chips aren't clipped at the viewport edge.
- Phone-width stacked stage list, replacing the canvas — WfPhoneStages (Workflows.tsx:4328-4364) — phone, below 561px.
- Phone stage header ("first · N steps" / "then · N steps") — WfPhoneStages (Workflows.tsx:4336-4338) — phone.
- Phone per-step pickable button, urgent steps named first — WfPhoneStages (Workflows.tsx:4339-4358) — phone — fills to `PHONE_NAMED` (2) named steps, `is-picked` when selected.
- Phone "+N more in this stage" overflow — WfPhoneStages (Workflows.tsx:4359) — phone.
- Hold-line text/title/said variants — holdLine (Workflows.tsx:4371-4381) — stage card / phone mix — null when nothing holds/waits.
- Mix-bar segment hover names — MixBar (Workflows.tsx:4400-4407) — stage card — each segment titled with count and state word; whole bar `aria-hidden` since counts beside it already say every figure.
- Stage failure-cause grouping, commonest first — stageCause (Workflows.tsx:4424-4455) — stage card with ≥1 failed step.
- Step "not started" presentation — present (Workflows.tsx:4472-4479) — step node, unstarted — glyph `◌`, "not started".
- Step "state unread" presentation — present (Workflows.tsx:4480-4487) — task not in read — glyph `?`, names the task id, clarifies unknown ≠ idle.
- Step "skipped by verdict" presentation — present (Workflows.tsx:4488-4501) — SUCCEEDED but gate-skipped — names the recorded verdict or "not recorded".
- Step "waiting for a member" presentation — present (Workflows.tsx:4502-4515) — parked waiting on a human — names the tenant and the "Now: …" expiry clause.
- Default step state presentation — present (Workflows.tsx:4516-4522) — ordinary state — lowercased word, "Agent `<id>`, attempt X of Y".
- Shared look for Table/Timeline/inspector — rowLook (Workflows.tsx:4528-4538) — any surface showing a step row — one `RowLook` (tone/word/title/mark/hue).
- Integrator "opens the PR" tag on the node — StepNode (Workflows.tsx:4685-4689) — figures tier, the step that opens the workflow's PR.
- Step state/duration row on the node — StepNode (Workflows.tsx:4716-4724) — graph node — mark + state word + waiting-for-member deadline + `StepTime` at figures tier.
- Step note line (failure/park reason), ellipsed with full title — StepNode (Workflows.tsx:4737-4741) — graph node with a reason.
- Step duration row at details tier — StepNode (Workflows.tsx:4747) — "agent not run" when skipped, else full `StepTime`.
- Step runner-profile meta row — StepNode (Workflows.tsx:4757) — any tier but names.
- Step figures list (ran/cost/tokens/ckpts) — StepNode (Workflows.tsx:4785-4800) — figures tier — four `NodeNum`s; "ran" becomes "agent / not run" when skipped.
- Step figures "from result" source line — StepNode (Workflows.tsx:4801) — figures tier — `NodeSource` names which figure came from the result rather than telemetry.
- Step dependency line, counted form — StepNode (Workflows.tsx:4834-4838) — many dependency files — "↑ N steps · M files", full list in title/aria-label.
- Step dependency line, listed form — StepNode (Workflows.tsx:4839-4858) — few dependency files — "↑ parent (file), parent2 …" via `depUnits`.
- Whole node card is the pick target — StepNode (Workflows.tsx:4863-4890) — graph node — entire card is one `<button aria-pressed>`, fills the inspector in place; nothing on the canvas navigates.
- Edge lighting on node hover/focus — StepNode (Workflows.tsx:4884-4887) — `onHover(stepId)` lights the node's edges.
- Full note revealed on focus (sibling span, not nested) — StepNode (Workflows.tsx:4899-4903) — node focused, has a note.
- Inline live-browser thumbnail — StepNode (Workflows.tsx:4921-4925) — step currently live — `LiveThumb` keyed by task id.
- Inline stop control — StepNode (Workflows.tsx:4926-4941) — step has a joined task — `StopRun` "Stop", drawn only once the task state was actually read.
- Dependency file count helper — depFileCount (Workflows.tsx:4950) — graph node.
- Edge key legend (runs after / file passed on / file expected, not arrived) — EdgeKey (Workflows.tsx:4986-5000) — graph view, any edge present — drawn with the canvas's own classes so the legend can't drift from the real rendering.
- Dependency file arrival mark — DepFile (Workflows.tsx:5017-5031) — graph node dependency line — plain for order-only, solid "is-staged" with byte size for arrived, dashed "is-declared" with a why-not-yet note otherwise.
- Duration/queued/parked/running text — StepTime (Workflows.tsx:5050-5056) — graph node — `dur.text` with `dur.note` as title.
- "Never started"/"finish not recorded"/"between attempts"/"so far" duration states — ranCell (Workflows.tsx:5142-5191) — figures tier.
- Skipped-agent "none"/"none yet" spend cell — noSpendCell (Workflows.tsx:5244-5253) — verdict-skipped or terminal-with-no-attempt.
- Reading-in-flight figures placeholder — figuresFor (Workflows.tsx:5273-5289) — attempt read pending.
- Usage-not-read/not-sampled/no-attempt-yet absences — figuresFor (Workflows.tsx:5326-5339) — four distinct reasons telemetry might be missing.
- Cost/tokens borrowed from the step's own result — figuresFor (Workflows.tsx:5301-5310, 5343-5349, 5356) — telemetry missing but result present — tagged "from result".
- Partial-attempt cost annotation — figuresFor (Workflows.tsx:5350-5355) — cost summed over only some attempts — "· K of N".
- Checkpoint count cell, always measured — figuresFor (Workflows.tsx:5367-5373) — distinguishes "none recorded" vs. "contents not recorded".
- Token breakdown cell — tokensOf (Workflows.tsx:5389-5392) — sums input/output/cache-read/cache-write tokens across attempts.
- Node figure cell rendering — NodeNum (Workflows.tsx:5411-5422) — figures tier — label + value, absent/reading styling.
- "From result" source note — NodeSource (Workflows.tsx:5444-5459) — figures tier.
- Workflow sample-coverage mark ("N/M sampled") — SampleNote (Workflows.tsx:5486-5511) — sampling partial — silent when every task was read; names the ceiling clause and any failed attempt reads.
- Workflow state mark fallback — WorkflowStateMark (Workflows.tsx:5524-5530) — no derivable state — amber ring `NamedMark` with the header's own word.
- "state unknown" wording fix — markWord (Workflows.tsx:5539-5541) — rewrites the API's "state not derived" to "state unknown".

## Workflow list — filters, search, sort, pagination, rows

- Segmented state tabs with live counts (Not ended/Finished/Failed/All) — WorkflowListFilters (Workflows.tsx:5612) — workflows list — switches the bucket filter; no count shown while loading; "Not ended" carries an explanatory title.
- Search box "Find a workflow or step" — WorkflowListFilters (Workflows.tsx:5633-5636) — list filter — free-text over workflow id, label, and step task ids (`listMatches`).
- Owner filter dropdown — WorkflowListFilters (Workflows.tsx:5642) — list filter — default "anyone"; option shows the name before `@`, full address as title.
- Profile filter dropdown — WorkflowListFilters (Workflows.tsx:5656) — list filter — default "any".
- Sort-order dropdown — WorkflowListFilters (Workflows.tsx:5669) — list filter — state/newest/oldest/failed, labels from `SORT_LABEL`.
- Whole toolbar disabled while the list read is in flight — WorkflowListFilters (Workflows.tsx:5609, 5615, 5638, 5644, 5658, 5672) — list, loading — nothing shifts layout once data lands.
- Loading skeleton list (6 skeleton rows) — WorkflowListSkeleton (Workflows.tsx:5700) — list, loading — disabled real toolbar + real table head + per-column bar widths, two-line name cell.
- List columns header — ListHead (Workflows.tsx:5563) — list — State/Workflow/Shape/Steps done/Runners/Cost/Owner/Submitted/Duration, `data-col` attributes.
- Pasted-workflow-id-not-in-read notice with link — WorkflowList (Workflows.tsx:5832-5838) — search box holds a full id not on the current page — "Open its page →" via `workflowHref`.
- Empty-results message, context-aware — WorkflowList (Workflows.tsx:5840-5853) — zero rows match — distinguishes "no workflow in this read" vs. "no match (older ones not searched)"; "Clear the filters" button when a filter is active.
- "Clear the filters" button — WorkflowList (Workflows.tsx:5849) — zero rows match — resets q/owner/profile.
- List note: read scope + unread-steps caveat — WorkflowList/CountNote (Workflows.tsx:5814-5822, 5831) — list — "of the N newest" vs. "all N", and how many rows' steps were read.
- List footer: scope, sort, page size, "Load 100 older" — WorkflowList (Workflows.tsx:5873-5886) — list — "`<rows>` of `<all>` read · `<sort label>` · 100 per read"; busy/exhausted/failed states shown inline.
- Load-older pagination, merged with the board's fresher first page — useOlderWorkflows (Workflows.tsx:5924) — list — `loadOlderWorkflows(next_page_token)`, idle/reading/failed states.
- Table row state mark cell — WorkflowListRow (Workflows.tsx:6097-6099) — one row — `WorkflowStateMark`.
- Row name/id + runner-mix line + current-step why line — WorkflowListRow (Workflows.tsx:6100-6143) — one row — name links via `workflowHref`, id shown under name when applicable, PR-wait tag, cancel-pending tag.
- Runner-mix responsive wrap-under-name — mixGoesUnder (Workflows.tsx:6005, used 6052/6113/6133) — medium viewport (900–1679px) — measures line width vs. 200px floor.
- Phone-width compact summary line — WorkflowListRow/phoneSummary (Workflows.tsx:6130-6132) — phone (≤560px) — `9/30 · 4✕ · 2h ago`, since done/failed/age columns are off-screen.
- Stage-shape glyph + text shape + "partial" chip — WorkflowListRow (Workflows.tsx:6144-6150) — one row — `StageGlyph`, `Shape`, partial-deps chip.
- Steps-done bar/fraction cell — WorkflowListRow/StepsDone (Workflows.tsx:6151-6152, 6291) — one row — progress bar by state hue, fraction, failed/cancelled/dead-lettered breakdown when terminal.
- Runners column (mix) — WorkflowListRow (Workflows.tsx:6154-6156) — one row, wide viewport.
- Cost cell — WorkflowListRow (Workflows.tsx:6157-6159) — one row — `Spend` short form.
- Owner cell — WorkflowListRow (Workflows.tsx:6160-6162) — one row — `ownerShort`, full address as title.
- Submitted cell — WorkflowListRow (Workflows.tsx:6163-6169) — one row — submission time, title includes first-step start phrase.
- Duration cell, "state unknown" treatment — WorkflowListRow (Workflows.tsx:6083-6090, 6170-6176) — state could not be derived — "began `<unit>` ago" instead of a "so far" claim.
- Row failing-row highlight — WorkflowListRow (Workflows.tsx:6065, 6093) — failed row — `is-failed` class.
- Row second line: running-step override of a stale "parked" reason — listRowWhy (Workflows.tsx:6231) — running workflow — shows "`<step>` running" when the first parked step's real blocker is itself holding capacity.
- Row second line: suppress duplicate "state unknown" text — listRowWhy (Workflows.tsx:6236) — unread/undetermined row — suppresses when the state mark's own title already says it.
- Row second line rendering per kind — RowWhyLine (Workflows.tsx:6248-6283) — one row — running/failed/failed-unread/parked/unread, bolds the step id, full sentence as title.
- Untrustworthy/partial steps-done bar (hatched) — StepsDone (Workflows.tsx:6294-6309) — partial/failed census — hatched (unread) + filled (succeeded-only) instead of a false full bar.
- Trustworthy steps-done with terminal outcome breakdown — StepsDone (Workflows.tsx:6310-6343) — ended workflow — "n/m" plus "· k failed · k cancelled · k dead_lettered".
- Owner/profile filter value preserved even when stale — withChosen (Workflows.tsx:5968, used 5785-5786) — filter set to a value no longer present among current rows — keeps it in the dropdown rather than silently resetting.
- Offer newest 5 workflows to the Recent switcher — WorkflowList (Workflows.tsx:5806-5811) — list — `offerNewestWorkflows` by created_at.

## Workflow page header and lifecycle — tabs, copy, cancel

- "Steps over time"/Graph/Table/Changes tabs — WfTabs (Workflows.tsx:6598), PAGE_TAB_LABEL (Workflows.tsx:6590), PAGE_TABS (Workflows.tsx:6582) — one workflow page — underline tabs as real addresses; Table tab shows step count; the `timeline` address is labelled "Steps over time" to avoid confusion with the platform Timeline screen.
- Page header row: state mark, PR link, failure-policy chip, cost chip, wait/cancel-pending tags — WorkflowHead (Workflows.tsx:6670-6701) — one workflow page — policy rendered in words ("if a step fails: keep going" / "stop the workflow").
- Copy workflow id button — CopyId (Workflows.tsx:6716-6735) — one workflow page — label toggles "Id copied"/"Copy refused", handles a missing/refusing clipboard API.
- Copy link button — CopyLink (Workflows.tsx:6742-6758) — one workflow page — copies `location.href`, label toggles "Link copied", swallows clipboard errors silently.
- Cancel workflow, typed confirm when capacity is held — CancelWorkflow (Workflows.tsx:6795-6887) — workflow not yet ended — "Stop workflow" button; `TypedConfirm` requiring the workflow id typed when any step is LEASED/DISPATCHED/STARTING/RUNNING (`holdsCapacity`).
- Cancel workflow, inline two-button confirm otherwise — CancelWorkflow (Workflows.tsx:6887) — no step holds capacity — `role="group" aria-label="Confirm cancelling this workflow"`, "Yes, stop the workflow" / "Keep it running".
- Cancel consequence preview text — cancelConsequence (Workflows.tsx:6760-6795) — cancel-confirm dialog — per-step-state outcome: not-started steps never start, waiting steps cancel now, running steps asked to stop and keep their slot until the worker exits, unread steps also asked to stop; always ends "Steps that finished keep their results."
- Cancel button hidden once terminal or (idle) while already pending — CancelWorkflow (Workflows.tsx:6823-6825) — terminal workflow, or cancel already pending — no disabled button drawn for nothing to cancel.
- Cancel-sent outcome message — CancelSent (Workflows.tsx:6915-6933) — after a stop request is recorded — "Stop recorded: N asked to stop, M had already ended." or plain "Stop recorded."
- Cancel-pending tag on header/row — WorkflowHead/WorkflowListRow (Workflows.tsx:6700, 6119) — cancel requested, not yet reflected in state — `CANCEL_PENDING_WORD` tag.
- Cancel failure message — CancelWorkflow (Workflows.tsx:6878, 6889-6893) — stop request failed — "The stop was not recorded: `<error>`".
- Workflow page skeleton — WorkflowPageSkeleton (Workflows.tsx:6523) — one workflow page, loading — count-note bar, head chips as bars, Copy id/Copy link disabled, no Cancel bar, real tabs with no step count.
- 404 not-found page message — WorkflowPage (Workflows.tsx:6462-6474) — id not found — "Not found · HTTP `<status>`" with a link back to the list.
- Workflow-outside-the-page notice — WorkflowPage (Workflows.tsx:6475-6481) — id outside the 100-newest page and its own read also failed — explains the list only reads the newest 100, with a link back.
- Auto-pick step card on first open (first failure, else waiting-for-member, else first live step) — firstStepToShow (Workflows.tsx:6630), used WorkflowPage (Workflows.tsx:6443-6444) — one workflow page, first open — won't reopen after being closed.
- Same-step scrubber board context fetched around the page's own workflow — useBoardAround (Workflows.tsx:6371-6395) — one workflow page — `loadWorkflowBoard(AROUND_LIMIT=50)` once per open, joined via `joinWorkflowPage`.
- Remember the opened workflow in the Recent(5) switcher — WorkflowPage (Workflows.tsx:6427-6432) — one workflow page, on open — `rememberWorkflow(id, derived, label)`.
- Tab change clears the stage/step-state Table filter — WorkflowPage (Workflows.tsx:6453-6454) — switching tabs — the filter belongs only to the Table view's band count.
- Stage/step-state band count opens the Table pre-filtered — WorkflowPage (Workflows.tsx:6455-6457) — Table tab — `onStageTable` sets tab=table plus the filter; `onClearFilter` clears it.
- Changes tab renders the diff viewer instead of the board — WorkflowPage (Workflows.tsx:6497-6499) — Changes tab — `WorkflowChangesTab` reading steps' patches.

## workflowlist.ts — address, buckets, sort, duration, row-why arithmetic

- THE ADDRESS IS THE STATE: every list filter and the open workflow's tab ride the URL query — parseWorkflowQuery/workflowQueryString (workflowlist.ts:66-140) — list and page — `?state=failed&owner=priya`, `/workflows/<id>?state=failed&owner=priya`; a copied link reproduces the exact view.
- Canonical query ordering (wf, tab, then filters) — workflowQueryString (workflowlist.ts:130-140) — any — unrecognised params dropped to default; an all-default list is the bare `/workflows`.
- Table's own stage+state filter param, written only together on the Table tab — WorkflowQuery.stage/.stepState (workflowlist.ts:80-88, 110-121, 134-137) — table view — `?stage=N&stepstate=word`.
- Back link carries only the list's own filters — listHref/filterQueryString (workflowlist.ts:143-161) — one workflow page — back link reproduces the originating list view.
- Workflow page link carries the list's filters — workflowHref (workflowlist.ts:164-168) — list row click — so the page's back link returns to the exact filtered list.
- Back-link label built from active filters — backLabel (workflowlist.ts:171-178) — one workflow page breadcrumb — `Workflows · Failed · owner: priya`.
- Derived-state-only rule (never the stored copy) — derivedStateOf (workflowlist.ts:185-189) — any bucket/sort computation — null unless the rollup is complete and `state_source === 'derived'`.
- Three-bucket classification (running/finished/failed) — bucketOf (workflowlist.ts:197-203) — segmented tabs, sort "state" — an undetermined state is Running, not Finished, because "it ended" is an unsupported claim; cancelled counts as Finished.
- "Keep polling" check across the whole list — anyRunning (workflowlist.ts:206-208) — list/page poll gate — true if anything is in the Running bucket.
- Non-segment filters (owner/profile/search) applied before the segment's own counts — matchesFilters (workflowlist.ts:211-220) — list filter — search matches workflow id, label, and every step id.
- Segment counts computed over the already-filtered rows — bucketCounts (workflowlist.ts:223-227) — segmented tabs — so counts reflect the other active filters.
- Owner/profile option lists derived from the rows actually read — ownersOf/profilesOf (workflowlist.ts:230-236) — list filter dropdowns.
- Steps-read completeness check — stepsRead (workflowlist.ts:245-248) — list note — true only if every step with a task has that task in the read; feeds "55 of 100 rows" caveat text.
- Per-workflow failed/dead-lettered step count for the "failed" sort — failedSteps (workflowlist.ts:251-258) — sort=failed.
- Default list order's eight state groups (live work, then parked/ready/queued, then succeeded, then failed/dead-lettered/cancelled, then undetermined last) — stateGroupOf (workflowlist.ts:268-293) — sort=state (default) — the triage reordering that once put 24 failures ahead of 1 running workflow.
- Four sort orders with a stable newest-first tiebreak — sortWorkflows (workflowlist.ts:303-322) — list sort control — state/newest/oldest/failed, ties never swap between two reads.
- Wall-clock workflow duration, submission to the last step's end, never guessed — workflowDuration (workflowlist.ts:348-388) — Duration column — "so far" while running; "not recorded"/"not read" when an end can't be measured rather than measuring to a wrong earlier end.
- Row second line: which step failed/parked and why — rowWhy (workflowlist.ts:405-436) — list row, failed/parked/unread workflow — cause is the first line of the task's `last_error` via `failureCause`; a failed step with no recorded error says so explicitly.
- Phone-width compact row line (`9/30 · 4✕ · 2h ago`) — phoneSummary (workflowlist.ts:447-460) — phone, 390px — done/failed count only drawn from a complete census; age always shown since submission.

Files covered: `WorkflowViews.tsx` (2098 lines), `WorkflowChanges.tsx` (1667 lines), `TimelineLanes.tsx` (2798 lines), under `apps/swarm-ui/src/`.

## WorkflowViews.tsx

- Step state mark (dot/icon) beside name — WfStepMark (WorkflowViews.tsx:79) — Graph view; Table view; Steps over time view; step selected — draws the state silhouette (running, parked, skipped, unknown/queued, etc.) in its state hue, optionally with the state word
- State tint class on rows/spans — tintClass (WorkflowViews.tsx:100) — Table view; Steps over time view — maps a step's hue to `t-<hue>` (or `t-unk` for an unread step) so row/span share the node's color pair
- "from result" source note — SourceNote / FROM_RESULT_WORD (WorkflowViews.tsx:144, 152) — Table view; step selected — small qualifier shown beside a cost/token figure that came from the step's result summary rather than attempt telemetry
- Graph/Timeline/Table view switcher — ViewControl (WorkflowViews.tsx:168) — Graph view; Table view; Steps over time view — segmented control to choose how one open workflow is drawn
- Stray-input warning — StrayMark (WorkflowViews.tsx:215) — Graph view; Table view — "N input(s) off-graph" note naming each staged file with no edge in the graph and where it came from
- Unreadable staged-input warning — UnreadableMark (WorkflowViews.tsx:247) — Graph view; Table view — "N input(s) unreadable" note counting staged-input entries that could not be parsed as files
- Step name picks into inspector — PickButton (WorkflowViews.tsx:277) — Table view; Steps over time view — toggle button (aria-pressed) selecting a step; clicking again deselects; never navigates
- Escape closes picked step via its own name — PickButton (WorkflowViews.tsx:296) — step selected — pressing Escape while focused on the opened step's name button closes the inspector
- Timeline "when each step waited and ran" view — WorkflowTimeline (WorkflowViews.tsx:331) — Steps over time view — one row per step with wait/run spans on a shared wall-clock axis, with inline inspector slot
- Timeline track accessible description — WorkflowTimeline (WorkflowViews.tsx:358) — Steps over time view — role="img" track whose aria-label states the step's whole timing sentence, including a cut-short wait explanation
- "Now" marker line on timeline — WorkflowTimeline (WorkflowViews.tsx:366) — Steps over time view, workflow still running — vertical now indicator at the current time on the axis
- Timeline legend/key — TIMELINE_KEY (WorkflowViews.tsx:385, 398) — Steps over time view — swatch list explaining "on parents", "waited", "parked", "ran", "running", "skipped by verdict"
- Timeline axis tick labels — Ticks (WorkflowViews.tsx:419) — Steps over time view; phone — ticks positioned by percentage, alternating labels hidden at ≤560px while keeping rule lines
- Broken-axis marker — Ticks (WorkflowViews.tsx:432) — Steps over time view, outlier wait present — glyph where the axis scale is clamped/cut
- Cut-short wait explanation text — cutSentence (WorkflowViews.tsx:445) — Steps over time view — sentence saying an outlier wait is drawn cut short and its real duration
- Wait/run span rendering — Spans (WorkflowViews.tsx:464) — Steps over time view — draws per-span bars (waiting/parents/ran/running), applies bad/tint/skip/parked/clamped states
- Minimum-visible span with length label — Spans (WorkflowViews.tsx:452, 498) — Steps over time view, very short run on long axis — ensures a 4px-minimum bar and prints its true duration beside it
- Clamped-span cut label — Spans (WorkflowViews.tsx:514) — Steps over time view — shows duration text at the point an outlier wait's drawn length was clamped
- Track placeholder word (no bar) — TrackMark (WorkflowViews.tsx:536) — Steps over time view, missing/absent time — prints a word positioned after the drawn time, flipping to the left past track midpoint
- Cost cell with partial-sum marker — CostCellView / PARTIAL_COST (WorkflowViews.tsx:564, 575) — Table view, step costed over only some attempts — shows "$0.4230" then "· 1 of 2" on its own styled segment
- Generic figure cell rendering — CellView (WorkflowViews.tsx:586) — Table view; step selected — shows value, absent-word, or "reading…" pending pulse; appends SourceNote for result-sourced figures
- "Reading…" pending cell state — CellView (WorkflowViews.tsx:597) — Table view, attempt read in flight — titled "The step's attempt read has not landed yet."
- Attempts-over-ceiling warning — AttemptsView (WorkflowViews.tsx:627, aria-label 633) — Table view, step exceeded its retry ceiling — bad-tone cell with accessible name stating how many attempts over the ceiling
- Table columns definition — COLUMNS (WorkflowViews.tsx:644) — Table view — Step, State, Why, Runner, Waited, Ran, Attempts, Cost, Tokens, Inputs, each sortable or not
- Why column auto-hide — tableColumns (WorkflowViews.tsx:780) — Table view, no step has a "why" — drops the Why column entirely and gives its width to Step
- Step column width sizing — stepColumnWidth (WorkflowViews.tsx:674) — Table view; phone — sizes Step column to its longest step id in mono width, capped at container width
- Figure column width sizing — figureColumnWidths (WorkflowViews.tsx:711) — Table view — each numeric column sized to its longest printed value up to a fixed px ceiling
- Cost column decimal padding — tableRows (WorkflowViews.tsx:763) — Table view — pads all dollar figures to the column's maximum decimal places without rounding
- "Never started" dash for inapplicable figures — tableRows (WorkflowViews.tsx:740) — Table view, step ended before starting — Waited/Cost/Tokens show "—" with an explanatory title instead of zero
- Waited cell computation and wording — waitedCell (WorkflowViews.tsx:792) — Table view — "on parents" while queued behind a dependency, "not recorded", or duration text with "so far" when still waiting
- Why cell — WhyCell (WorkflowViews.tsx:829) — Table view, step blocked/failed — one-line reason, full text in title, warn styling when action is needed
- Inputs file list — InputsCell (WorkflowViews.tsx:854, aria-label 892) — Table view, row's inputs expanded — one line per declared/stray file with size or not-yet-staged word, plus malformed/unreadable count
- Inputs summary toggle — InputsSummary (WorkflowViews.tsx:914) — Table view — "prompt" for a step reading nothing, else "N files · N unreadable" expand/collapse button (aria-expanded)
- Sortable table with sticky head — WorkflowTable (WorkflowViews.tsx:975) — Table view — sortable grid; sort state lives in the component; row tones for bad/unknown/skipped
- Column sort buttons with direction arrows — WorkflowTable (WorkflowViews.tsx:1054) — Table view — clicking a sortable head cycles sort, shows ▲/▼
- Expandable "Reads" row — WorkflowTable (WorkflowViews.tsx:1123) — Table view, Inputs toggled open — full-width sub-row showing the InputsCell detail
- Inline step-detail row under picked row — WorkflowTable (WorkflowViews.tsx:1133) — Table view, step picked — renders inspector content directly under the selected row
- Sideways-scroll "more columns" fade cue — useMoreRight / MoreRight (WorkflowViews.tsx:1159, 1185) — Table view; phone — right-edge mask-image fade shown while columns remain off-screen, re-measured on scroll/resize
- Never-attempted vs. no-attempt-yet wording — noAttempt (WorkflowViews.tsx:1208, 1213) — step selected — "never started" when task is terminal with zero attempts vs. "no attempt yet" while still pending
- Same-step sibling strip (limited window) — STRIP_MAX / stripWindow (WorkflowViews.tsx:1234, 1237) — step selected — shows up to 15 occurrences around the current one with "+N" counts on either side
- Attempt scrubber facts labelling — FACT_LABEL / FACT_HINT (WorkflowViews.tsx:1259, 1281) — step selected, attempt selected — renames raw keys to "lease generation", "exit code", "saved checkpoints", "this attempt's cost", "agent process"
- Deduped failure/error text — unsaid (WorkflowViews.tsx:1304) — step selected, failed step — avoids repeating the same failure reason twice; rewords a 0 exit code on a failed step as "ended cleanly"
- Workflow shape summary text — shapeWords / ShapeWords (WorkflowViews.tsx:1327, 1336) — step selected — "6 steps in 2 stages (5 → 1)"; wraps the parenthetical on its own line on narrow screens
- Folded "More about this attempt" facts — FactList (WorkflowViews.tsx:1351, 1353) — step selected, attempt selected — hides lease-generation jargon behind a details disclosure
- Fact list rendering with absent/measured states — FactItems (WorkflowViews.tsx:1369, aria-label 1376) — step selected — each fact as a labelled list item, absent facts get an aria-label note
- Scrub buttons staying focusable at ends — ScrubButton (WorkflowViews.tsx:1415, aria-label 1433) — step selected, scrubber at either end — uses aria-disabled instead of disabled so focus is never lost
- Arrow-key scrubbing — scrubKeys (WorkflowViews.tsx:1445) — step selected, scrubber group focused — ArrowLeft/ArrowRight step through attempts or sibling workflows without touching Tab order
- Step Inspector panel — StepInspector (WorkflowViews.tsx:1477, aria-label 1654) — step selected — side/inline panel with workflow head, attempt scrubber, sibling scrubber, merge/children slot, close button
- Attempt read lifecycle (reading/ready/failed) — StepInspector (WorkflowViews.tsx:1567) — step selected — calls loadAttempts, tracks "reading", "ready" (incl. empty), or "failed" with error detail
- Focus-follows-selection into scrubber — StepInspector (WorkflowViews.tsx:1608) — step selected, arriving via sibling navigation — focuses the newer/older scrub button once the pager exists
- Counted-vs-stepped attempt mismatch note — StepInspector (WorkflowViews.tsx:1632) — step selected, CI-parked merge step — explains when stepper's "run N of M" differs from the table's counted attempts (refunded CI waits)
- Escape closes inspector and refocuses step name — closeOnEscape (WorkflowViews.tsx:1643) — step selected — Escape calls onClose and refocuses the originating step-name button
- Inspector head: workflow or step heading — StepInspector (WorkflowViews.tsx:1654) — step selected — shows either step-as-heading or workflow label/id with state mark, optional start/submit times line
- Close ("×") button — StepInspector (WorkflowViews.tsx:1689) — step selected — "Stop inspecting this step" button ending the head's first line
- "Open agent →" link — StepInspector (WorkflowViews.tsx:1719) — step selected, step has a task — routes to the agent's live/terminal path depending on task state; absent when no task
- "Logs ›" link — StepInspector (WorkflowViews.tsx:1741) — step selected, step has a task — deep-links to the agent's Logs tab
- "This step's changes…" link — StepInspector (WorkflowViews.tsx:1753) — step selected, changes available — links to the workflow Changes tab filtered to this step, with file count or no-count wording
- "Why it failed is an older attempt's" note — StepInspector (WorkflowViews.tsx:1792) — step selected, scrubbed to a non-newest attempt that failed — clarifies the shown reason belongs to the final attempt, not the one in view
- Attempt scrubber control — StepInspector (WorkflowViews.tsx:1798, aria-label 1801) — step selected — "not started" / "reading" / "attempts unread" / no-attempt wording / ◀ run N of M ▶ with counted-attempts caption and FactList
- Same-step-in-other-workflows scrubber — StepInspector (WorkflowViews.tsx:1881, aria-label 1884) — step selected — no-sibling sentence, or ◀ workflow N of M ▶, shape label, and a decorative state-mark strip (ringed at current position)
- Workflow id display — StepInspector (WorkflowViews.tsx:1961) — step selected, no page head carrying the id — prints workflow id with copy-style Id component
- Sha abbreviation — Sha (WorkflowViews.tsx:1968) — Changes view, merge step selected — shows 7-char short sha with full sha as title
- CI wait reason wording — waitsOn (WorkflowViews.tsx:1977) — Changes view, merge step selected, waiting state — "pending: …", "GitHub has not computed mergeability yet", "no check has reported yet…", or "the pull request's checks"
- Merge step card — MergeStepCard (WorkflowViews.tsx:1998, aria-label 2058) — Changes view, merge step selected — renders state: waiting for CI, behind/updated (n of 12 cap), merged (by this step/already/not by this step, commit, issues closed), refused/merge failed, or not-merged-yet
- Merge card PR reference — MergeStepCard (WorkflowViews.tsx:2056) — Changes view, merge step selected — "#123" link to pull request or "pull request not recorded"
- Merge card "current CI wait began" timestamp — MergeStepCard (WorkflowViews.tsx:2085) — Changes view, merge step selected, parked for CI — relative time with explanatory title that the CI clock restarts after a merge-slot wait

## WorkflowChanges.tsx

- Changes count badge/summary for an agent — changesCount (WorkflowChanges.tsx:96) — Changes view — returns a measured count, "N+" for truncated, or a null count with a reason sentence (never a fake 0)
- Agent Changes pane with attempts preload — AgentChangesPane (WorkflowChanges.tsx:125) — Changes view — reads attempts before drawing, shows a pending "Reading this agent's attempts…" loading row
- Loading indicator while reading attempts — AgentChangesPane (WorkflowChanges.tsx:141) — Changes view, loading state — pending mark plus animated loading bar
- Credential-withheld state on agent changes — AgentChanges (WorkflowChanges.tsx:165) — Changes view, withheld state — heading "withheld · a file failed the credential scan", lists refused file names, never renders patch content
- No-git-summary / nothing-written-yet state — AgentChanges (WorkflowChanges.tsx:184) — Changes view, empty/unsummarised state — differentiates terminal ("no git summary") vs in-progress ("nothing written yet") wording
- Patch discarded over size cap state — AgentChanges (WorkflowChanges.tsx:200) — Changes view, omitted state — heading shows byte count when known, explains "discarded… NOT truncated"
- Zero-changes state — AgentChanges (WorkflowChanges.tsx:211) — Changes view, zero state — "no changes · nothing differed from the clone"
- Pull request link passthrough — AgentChanges (WorkflowChanges.tsx:218) — Changes view — forwards `git.pull_request` as `{url, label:"#number"}` to PatchChanges
- Base commit / work-tree lead line — AgentChanges (WorkflowChanges.tsx:227) — Changes view, patch shown — shows "base <sha> → work tree" when `git.base` present
- "Handed on, not committed" marker — AgentChanges (WorkflowChanges.tsx:234) — Changes view — marks a step's diff handed to a dependant rather than committed
- Single-patch viewer by manifest name — PatchChanges (WorkflowChanges.tsx:254) — Changes view, full-height diff — loads artifact content, supports retry, offset windows
- Patch loading indicator — PatchChanges (WorkflowChanges.tsx:289) — Changes view, loading — pending mark + loading bar while reading named patch
- Patch read error state with Retry — PatchChanges (WorkflowChanges.tsx:297) — Changes view, error state — shows HTTP status, error message, Retry button
- Patch not in bucket (absent) state — PatchChanges (WorkflowChanges.tsx:313) — Changes view, absent state — "patch not in the bucket"
- Patch unreadable/binary state — PatchChanges (WorkflowChanges.tsx:324) — Changes view, unreadable state — "patch is not text" or "artifact store could not be read"
- Empty patch state — PatchChanges (WorkflowChanges.tsx:335) — Changes view, zero state — "empty patch · The patch was written and is empty."
- Patch summary line with file name and byte size — PatchChanges (WorkflowChanges.tsx:342) — Changes view — "name · bytesLabel" mono text
- Masked-value count chip — PatchChanges (WorkflowChanges.tsx:353) — Changes view — shows redaction_count or "0 of N families", warn style when >0
- Partial/windowed patch marker — PatchChanges (WorkflowChanges.tsx:359) — Changes view, windowed patch — "partial" chip explaining byte offsets
- "Next window" / "first window" paging buttons — PatchChanges (WorkflowChanges.tsx:367) — Changes view, windowed patch — fetches next/initial offset of a large patch
- "What this window cut" note — PatchChanges (WorkflowChanges.tsx:403) — Changes view, windowed patch — explains the window's start was cut or which file is cut at the end
- No-file-in-window empty message — PatchChanges (WorkflowChanges.tsx:413) — Changes view, windowed patch, empty — "No file lies wholly inside this window… Read the next window, or download the whole patch."
- Copy window / Download whole patch actions — PatchChanges (WorkflowChanges.tsx:398) — Changes view, windowed patch — Copy window / Download whole patch passed to DiffView
- State-word chip vocabulary — STATE_WORD (WorkflowChanges.tsx:433) — Changes view — "none" / "not measured" / "not read" / "partial" chip labels
- Generic empty/error/zero state block — State (WorkflowChanges.tsx:446) — Changes view — mark icon + heading + description + optional action link, role="status"
- Agent's-own-Changes link from a step/column — agentChangesHref (WorkflowChanges.tsx:478) — Changes view, matrix column header — builds route for a step's own agent Changes tab
- Read-concurrency throttle (max 4 in flight) — MAX_READS_IN_FLIGHT (WorkflowChanges.tsx:519, 827) — Changes view, matrix — caps simultaneous attempt/patch reads at 4, shown via per-column "reading" pending marks
- Step filter and open-file encoded in the address — ChangesAt / changesAtOf / withChangesAt (WorkflowChanges.tsx:522, 536) — Changes view — reads/writes `?step=` and `?file=` query params (empty string treated as absent)
- Files × steps matrix — ChangesMatrix (WorkflowChanges.tsx:971) — Changes view — main grid: files down rows, step columns, cell shows +/− counts
- Step filter chip bar — ChangesMatrix (WorkflowChanges.tsx:1074) — Changes view, matrix toolbar — "All steps" chip plus one chip per step, aria-pressed toggling column filter
- Matrix summary line (files/steps counts) — ChangesMatrix (WorkflowChanges.tsx:1084) — Changes view, matrix toolbar — "N files across M steps", pluralized
- "File lists not collected for any step" fallback — ChangesMatrix (WorkflowChanges.tsx:1088) — Changes view, matrix toolbar, all-unknown state — avoids showing a false "0 files"
- "+" partial-count notation — ChangesMatrix (WorkflowChanges.tsx:1093) — Changes view, matrix toolbar — appends "+" to file count when some step's list is incomplete/unknown
- "file lists known for X of N steps" note — ChangesMatrix (WorkflowChanges.tsx:1103) — Changes view, matrix toolbar — says how many step lists are actually in hand
- Masked-count chip at matrix level — ChangesMatrix (WorkflowChanges.tsx:1108) — Changes view, matrix toolbar — total masked redactions across read patches, warn style
- Pull-request external link in matrix bar — ChangesMatrix (WorkflowChanges.tsx:1113) — Changes view, matrix toolbar — "#123 ↗" opens PR in new tab
- "What this page shows" scope note — ChangesMatrix (WorkflowChanges.tsx:1125) — Changes view, matrix — distinguishes all-steps vs single-step-filtered scope, points to "Agent's changes ›" for whole patch
- "No steps" empty state — ChangesMatrix (WorkflowChanges.tsx:1141) — Changes view, no steps — zero-mark State: "This has no steps, so nothing changed any file."
- File × step table — ChangesMatrix (WorkflowChanges.tsx:1144) — Changes view, matrix table — table with File column header and one th per step
- Column header with step label / integrate marker — ChangesMatrix (WorkflowChanges.tsx:1148) — Changes view, matrix header — "integrate · <label>" for the PR-opening step, plus ColumnSay sub-line
- "Agent's changes ›" per-column link — ChangesMatrix (WorkflowChanges.tsx:1159) — Changes view, matrix header, drawn/started step — links to that step's own agent Changes tab
- Row file-name button to select/open a file — ChangesMatrix (WorkflowChanges.tsx:1172) — Changes view, matrix row — clicking a path sets `file` query param, highlighted row via `is-on` class
- "named by the review" tag on a row — ChangesMatrix (WorkflowChanges.tsx:1175) — Changes view, matrix row — marks files referenced by review findings, sorted first
- Per-cell data-label for stacked/phone layout — ChangesMatrix (WorkflowChanges.tsx:1180) — Changes view; phone — data-label names the step so a stacked cell still says whose it is
- Column "changed nothing" sub-line — ColumnSay (WorkflowChanges.tsx:878) — Changes view, matrix column — zero mark + "changed nothing"
- Column "withheld" sub-line — ColumnSay (WorkflowChanges.tsx:884) — Changes view, matrix column — unread mark + "withheld" (credential refusal)
- Column "checking" sub-line — ColumnSay (WorkflowChanges.tsx:890) — Changes view, matrix column — pending mark while attempts are read
- Column "not started" sub-line — ColumnSay (WorkflowChanges.tsx:892, aria-label 892) — Changes view, matrix column — plain "not started" text
- Column "not in this read" sub-line — ColumnSay (WorkflowChanges.tsx:894) — Changes view, matrix column — absent mark + "not in this read"
- Column "patch / file list" dual-fact sub-line — ColumnSay (WorkflowChanges.tsx:900) — Changes view, matrix column, unsummarised state — separately labels "patch: not measured" and "file list: not collected/nothing yet"
- Column file-count sub-line with partial "+" — ColumnSay (WorkflowChanges.tsx:917) — Changes view, matrix column — "N files" or "N+ files", with omitted/handed-on/pending/failed marks and a Retry button
- Matrix cell rendering (count/none/withheld/unknown) — Cell (WorkflowChanges.tsx:944, aria-label 963) — Changes view, matrix cell — "+N −N"/"binary" count, "·" for measured none, "withheld" text, hatched unknown cell with reason
- Review findings summary header — ReviewFindings (WorkflowChanges.tsx:1249) — Changes view, findings panel — "N review findings", "N pinned", "N from an earlier patch", "N not placed"
- Review finding list items with severity + location — ReviewFindings (WorkflowChanges.tsx:1258) — Changes view, findings panel — severity label, summary text, pinned/earlier/unplaced/checking/uncompared location phrasing
- "pinned · <step> · <file> · <side> <line>" open-file button — ReviewFindings (WorkflowChanges.tsx:1266) — Changes view, findings panel — clicking jumps matrix to that step/file
- "open file" button for an unpinned-but-rowed finding — ReviewFindings (WorkflowChanges.tsx:1278) — Changes view, findings panel — opens the finding's file without pinning
- Placement explanation phrases — placementWords (WorkflowChanges.tsx:1294) — Changes view, findings panel — "from an earlier patch…", "not placed: …", "checking: …", "not compared: …" sentences
- Empty-rows explanation ("changed no files") — EmptyRows (WorkflowChanges.tsx:1308) — Changes view, matrix with zero rows — distinguishes a measured zero vs withheld vs still-unknown, with cause breakdown
- "changed nothing"/"withheld"/"never started" cause tally — unknownCauses (WorkflowChanges.tsx:1364) — Changes view, matrix empty-rows message — builds "5 steps ended without a git summary; 1 step never started" sentence
- File-by-step composed diff viewer — FileByStep (WorkflowChanges.tsx:1407) — Changes view, open file — stacks each step's hunks for one file in one DiffView, tagged by step
- "changed by <steps>" summary line — FileByStep (WorkflowChanges.tsx:1486) — Changes view, open file — lists which steps' labels touched the open file
- Missing-hunk explanations per step (pending/unread/partial) — FileByStep (WorkflowChanges.tsx:1426, 1467) — Changes view, open file, meta panel — "reading its patch", "its patch was not read: …" with Retry, "lies past the first window… read it on the step's own Changes tab"
- "Where these hunks come from" meta note — FileByStep (WorkflowChanges.tsx:1458, aria-label 1459) — Changes view, open file — explains line numbers are per-step and "which step wrote each line… not recorded"
- "Copy hunks" action — FileByStep (WorkflowChanges.tsx:1502) — Changes view, open file — copies the composed multi-step patch text
- Disabled/refused download for composed file — FileByStep (WorkflowChanges.tsx:1503) — Changes view, open file — download.refused message: "not a patch that applies… each step's own Changes tab downloads its patch"
- Step-tagged hunk pins (review findings on diff) — FileByStep (WorkflowChanges.tsx:1452, 1500) — Changes view, open file diff — passes pins/stepOf to DiffView so findings show inline per hunk
- Local (non-URL) address fallback — useLocalAt (WorkflowChanges.tsx:1514) — Changes view, embedded without routing — matrix still works via component state when no at/onAt passed
- Workflow Changes tab — WorkflowChangesTab (WorkflowChanges.tsx:1527, aria-label 1545) — Changes view — builds step columns from workflow.steps, renders ChangesMatrix with PR info
- Run Changes tab — RunChangesTab (WorkflowChanges.tsx:1552) — Changes view — aggregates main workflow plus CI fix rounds into one matrix
- "No workflow yet" state — RunChangesTab (WorkflowChanges.tsx:1559, aria-label 1561) — Changes view, pre-plan state — "The run's workflow is created when its plan is approved."
- Loading indicator while reading run's workflow — RunChangesTab (WorkflowChanges.tsx:1570, aria-label 1572) — Changes view, loading — pending mark + loading bar
- Fix-round column labeling — RunChangesTab (WorkflowChanges.tsx:1582) — Changes view, matrix columns — "fix 1 · <stepId>" for CI fix rounds, keyed round:stepId
- "Try again" re-read of the run — RunChangesTab (WorkflowChanges.tsx:1556, 1640) — Changes view, failed-read state — button triggers a fresh useRunWorkflows read
- "Workflow not found"/"not yours to read" state with navigation — RunChangesTab (WorkflowChanges.tsx:1608, aria-label 1611) — Changes view, 404/403 state — absent mark, links "Back to the run" and "Workflows"
- "Steps not read" failed state — RunChangesTab (WorkflowChanges.tsx:1633, aria-label 1635) — Changes view, generic read-failure state — unread mark, per-workflow error text, "Try again" button
- Partial-failure banner above a still-rendered matrix — RunChangesTab (WorkflowChanges.tsx:1656, aria-label 1656) — Changes view, stale data shown — "<Workflow> could not be read: …, so its columns are the last read."

## TimelineLanes.tsx

- Lanes/Outcomes page tab nav — TimelinePages (TimelineLanes.tsx:182, aria-label 184) — Graph view (Lanes); Steps over time view (Outcomes) — two links switching between the Lanes and Outcomes strips, "current page" marked, title shows the other page's matching range
- Page title and refresh control — PageHead (TimelineLanes.tsx:1206) — Steps over time view — shows "Timeline" title plus a RefreshControl with ticking age and 60s cadence
- Auto-polling every 60s — TimelineLanesScreen (TimelineLanes.tsx:633, 699) — Steps over time view — re-reads the span each minute via usePoll, paused while idle/hidden
- Idle-pause / resume banner — TimelineLanesScreen (TimelineLanes.tsx:698, 1212) — Steps over time view, idle — RefreshControl shows "Paused · resume"; resume both resumes polling and refreshes
- Span/lane count note — CountNote (TimelineLanes.tsx:1223) — Steps over time view — shows reach/range words and "N of M lanes" count under the head
- "Made on Outcomes" cross-link note — TimelineLanesScreen (TimelineLanes.tsx:1228) — Steps over time view, arrived via Outcomes link — note plus "Open it on Outcomes" link
- Span segmented control — TimelineLanesScreen (TimelineLanes.tsx:1236) — Steps over time view — picks preset spans (LANE_SPANS), clears zoom window
- Earlier pan button — TimelineLanesScreen (TimelineLanes.tsx:1244, aria-label 1244) — Steps over time view — "‹" button, moves window half a width earlier
- Zoom out button — TimelineLanesScreen (TimelineLanes.tsx:1247, aria-label 1247) — Steps over time view — "−" halves zoom (factor 2)
- Zoom in button — TimelineLanesScreen (TimelineLanes.tsx:1250, aria-label 1250) — Steps over time view, zoomed — "+" doubles zoom (factor 0.5)
- Later pan button with "up to now" disable — TimelineLanesScreen (TimelineLanes.tsx:1253, aria-label 1253) — Steps over time view, window at now — disabled when win.until >= anchor - 60000
- Drag-to-zoom on axis — Axis (TimelineLanes.tsx:1719, 1768) — Steps over time view, zoomed/dragging — pointer-drag brush on the axis track, shows "drag to zoom: <range>" live, commits on release if longer than DRAG_MIN_PX
- Phone filter sheet toggle — TimelineLanesScreen (TimelineLanes.tsx:1262) — phone — "Filters · N" button toggles a collapsible filter sheet, counted by lanesFiltersSet
- Phone legend/key toggle — TimelineLanesScreen (TimelineLanes.tsx:1277) — phone — "Key" button toggles the Legend panel
- Escape closes filter sheet / legend — TimelineLanesScreen (TimelineLanes.tsx:668) — phone, sheet or key open — Escape folds whichever is open and returns focus to its toggle button
- State filter picker — TlPicker (TimelineLanes.tsx:1289, 1551) — Steps over time view, filters — checklist of ALL_STATES, shows selected states or "all"
- Profile filter picker — TlPicker (TimelineLanes.tsx:1298) — Steps over time view, filters — checklist of profiles seen in lanes, with reading/failed states (no bogus "all 0")
- Workflow filter dropdown — TimelineLanesScreen (TimelineLanes.tsx:1305) — Steps over time view, filters — select of workflow ids, labelled with wfNames when known
- Kind segmented filter (all/steps/standalone) — TimelineLanesScreen (TimelineLanes.tsx:1316) — Steps over time view, filters — "workflow steps" label override
- Group-by select — TimelineLanesScreen (TimelineLanes.tsx:1323) — Steps over time view, filters — groups lanes by LANE_GROUPS (workflow/profile/none)
- "Way back" zoom chip — TimelineLanesScreen (TimelineLanes.tsx:1338) — Steps over time view, zoomed — Chip showing the span zoomed from, click restores it
- Outcome strip summary — OutcomeStrip (TimelineLanes.tsx:2428) — Steps over time view — one-line success rate + finished/succeeded/failed/cancelled breakdown, link to Outcomes
- Outcome strip loading/error states — OutcomeStrip (TimelineLanes.tsx:2488) — Steps over time view, strip loading/failed — "Reading outcomes…" / "Outcomes not read (...)" without blocking lanes
- Outcome strip mismatched-window note — OutcomeStrip / decidedWindow (TimelineLanes.tsx:2412, 2496) — Steps over time view, strip window differs from lane window — "outcomes — not this span, read for <window>"
- Outcome strip "no agent decided" state — OutcomeStrip (TimelineLanes.tsx:2500) — Steps over time view, no decided agents — dash + explanation
- Fenced-generations and parks counts — OutcomeStrip (TimelineLanes.tsx:2542) — Steps over time view — counts with partial/"counting…"/"not counted on N of M lanes" states while events are still reading
- Outcomes-for-this-span link — OutcomeStrip (TimelineLanes.tsx:2470, 2584) — Steps over time view — deep link to Outcomes page with matching query
- Coverage footnote under strip — OutcomeStrip (TimelineLanes.tsx:2590) — Steps over time view — explains ledger vs lanes window coverage, link to help topic
- Suggest-Outcomes note for wide spans — sliversNote (TimelineLanes.tsx:166, 1360) — Steps over time view, span > 7 days — "Over N days the bars are slivers…" + Open Outcomes link
- Legend / key — Legend (TimelineLanes.tsx:1649) — Steps over time view — color/shape key: held, waiting, parked, fenced, cancel asked, ended, failed/dead-lettered
- Attempts read failure state — TimelineLanesScreen (TimelineLanes.tsx:1369) — Steps over time view, attempts read error — alert with "Try again" button
- Initial loading skeleton — TimelineLanesScreen (TimelineLanes.tsx:1378) — Steps over time view, first load — "Reading attempts for <range>…" with skeleton bars
- Empty span state — TimelineLanesScreen (TimelineLanes.tsx:1384) — Steps over time view, no lanes at all — explains waiting work draws no bar, links to Agents › Waiting
- No lanes match filters state — TimelineLanesScreen (TimelineLanes.tsx:1391) — Steps over time view, filtered to zero — "Clear filters" button resets state/profile/wf/kind/lane
- Stale/re-reading overlay — TimelineLanesScreen (TimelineLanes.tsx:1399, 1469) — Steps over time view, refresh in flight — dimming + "Reading the span again… the lanes above are the last read."
- Time axis with tick labels — Axis / axisLabels (TimelineLanes.tsx:237, 1682) — Steps over time view — day/hour-aware tick labels, hides ticks overlapping "now" label, measures track px
- "now" marker on axis — Axis (TimelineLanes.tsx:1767) — Steps over time view, window ends at present — right-edge "now" tick
- Gridlines aligned to ticks — gridlineImage (TimelineLanes.tsx:471, 1404) — Steps over time view — CSS gradient drawn from tick percentages, applied per lane track
- Lane group headers — GroupHead (TimelineLanes.tsx:1845) — Steps over time view, grouped by workflow/profile/standalone — title, state mark, step/agent count note, retry button on workflow read failure
- Workflow bracket/span highlight — blockSpan / bracketStyle (TimelineLanes.tsx:1801, 1841, 1946) — Steps over time view, grouped by workflow — bracket spanning the workflow's lanes' earliest-to-latest activity, clipped to track end
- Workflow steps note — stepsNote (TimelineLanes.tsx:1969) — Steps over time view, workflow group — "k of n steps have attempts in this span · state"
- Workflow steps gap note — stepsGap (TimelineLanes.tsx:1983) — Steps over time view, workflow group with missing steps — "no lane: step1 (not started), step2 (no attempt yet)…"
- Workflow read retry link — GroupHead (TimelineLanes.tsx:1888) — Steps over time view, workflow read failed — "steps not read: rate-limited/<reason> · retry" button
- Workflow 429 backoff queue — readQueue / withBackoff (TimelineLanes.tsx:568, 597, 878) — background — throttles to 4 concurrent reads, retries 429s after 1s/3s/9s, batches ids
- Settled workflow caching — workflowSettled (TimelineLanes.tsx:1522, 863) — background — a workflow whose steps all ended is not re-read on refresh
- Lane rows (per-agent track) — LaneRow (TimelineLanes.tsx:2043) — Steps over time view — name, state mark, held duration, note, pills/segments, marks
- Lane name formatting — laneName (TimelineLanes.tsx:1827) — Steps over time view — step's agent name, else title/profile · short id
- "Not run (verdict X)" skipped label — LaneRow (TimelineLanes.tsx:2082, 2154) — Steps over time view, step skipped by gate verdict — small note instead of a held-duration badge
- Lane note (profile, attempt counts, read failures) — laneNote (TimelineLanes.tsx:1995) — Steps over time view — "profile · N attempts" / "never ran · no attempt" / events read failure text
- "read its events again" retry link — LaneRow (TimelineLanes.tsx:2160) — Steps over time view, lane events read failed — retries that lane's events fetch
- Crowded-attempts merged pill — crowdPills / MergedPill (TimelineLanes.tsx:333, 2266) — Steps over time view, many attempts close together — single numbered pill, expandable attempt list on hover/focus/tap
- Merged pill attempt list popover — LaneRow (TimelineLanes.tsx:2229, aria-label 2230) — Steps over time view, merged pill expanded — list under the track listing each attempt ("gen N · state · from → to")
- Segment/pill rendering (hold/cut/park/wait) — Seg (TimelineLanes.tsx:2323, aria-label 2302) — Steps over time view — colored bar per segment kind with words shown if wide enough, title tooltip always
- Lane end/mid marks (succeeded/failed/dead-lettered/cancelled/cancel-requested) — LaneRow (TimelineLanes.tsx:2205, aria-label 2214) — Steps over time view — clickable/keyboard-activatable mark icons opening the lane detail, "cancel asked" tag shown inline
- Lane layout collision avoidance — laneLayout / markLefts (TimelineLanes.tsx:293, 386) — Steps over time view — bars/marks pushed left from the right edge so nothing overlaps or runs off-track
- Click lane name to open detail — LaneRow (TimelineLanes.tsx:2126) — Steps over time view, step selected — toggles the selected lane's detail panel, aria-pressed
- Lane detail panel (attempts/parked/wall time/cost) — LaneDetail (TimelineLanes.tsx:2616, aria-label 2645) — Steps over time view, step selected — cards for Attempts summary, Fenced note, Parked note, Wall time, Cost, "Open in Agents" button
- Detail panel close button and focus return — LaneDetail (TimelineLanes.tsx:2635, 2650); TimelineLanesScreen (TimelineLanes.tsx:1450) — Steps over time view, step selected — × button and Escape close it, focus returns to the lane's name button
- Detail panel events list — LaneDetail (TimelineLanes.tsx:2715) — Steps over time view, step selected — newest-first event rows with time, mark, word, generation tag
- "Older events" pagination in detail — LaneDetail (TimelineLanes.tsx:2739) — Steps over time view, step selected, more events exist — button follows page token, shows "Reading…" / failure text
- Event row rendering — EventRow (TimelineLanes.tsx:2775) — Steps over time view, step selected — time, colored mark icon, event word, fence gen-transition detail, park reason detail, generation tag if not current
- "Lane not in this read" note — TimelineLanesScreen (TimelineLanes.tsx:1498) — Steps over time view, selected lane fell out of span — explanatory paragraph
- "More attempts in this span" partial pagination banner — TimelineLanesScreen (TimelineLanes.tsx:1473, aria-label 1474) — Steps over time view, attempts page not exhausted — count + dash for unknown total + "Read the next page" button
- Attempt page coverage footnote — TimelineLanesScreen (TimelineLanes.tsx:1485) — Steps over time view — explains pages read, "an attempt created before the span is not on this read", step-without-attempt count, events coverage, filter scope, timezone
- Attempts read pagination follow (SPAN_PAGES_READ cap) — TimelineLanesScreen (TimelineLanes.tsx:716) — background — auto-follows up to 10 pages, then requires manual "Read the next page"
- Drawn-reach short-read indicator — drawnReach (TimelineLanes.tsx:1511, 799, 1224) — Steps over time view, read stopped short — "newest N attempts, <reach>–<until> of <since>–<until>"
- Task page back-paging (TASK_PAGES_READ) — TimelineLanesScreen (TimelineLanes.tsx:807) — background — pages back through tasks until span covered or cap hit
- Single-task fallback reads (MISSING_TASK_READS cap) — TimelineLanesScreen (TimelineLanes.tsx:974) — background — reads tasks missed by paging individually, up to 40
- Never-ran workflow step lanes — TimelineLanesScreen (TimelineLanes.tsx:1075) — Steps over time view, workflow step with zero attempts — drawn as an empty lane ("never ran yet · no attempt")
- Lane summary count by grouping — laneSummary (TimelineLanes.tsx:485, 1410) — Steps over time view — "N workflows · M standalone" / "N lanes in M profiles" / "N lanes"
- Lazy per-lane events loading (IntersectionObserver) — LaneRow (TimelineLanes.tsx:2041, 2071) — Steps over time view — a lane's events page is only requested once its row scrolls into view
- Selected lane always reads its events — LaneDetail (TimelineLanes.tsx:2632) — Steps over time view, step selected — reads regardless of visibility
- Ended-lane events cached across refresh — TimelineLanesScreen (TimelineLanes.tsx:1006) — background — a terminal lane's already-read events page is kept on the minute's re-read
- Workflow filter options list names — TimelineLanesScreen (TimelineLanes.tsx:1169) — Steps over time view, filters — workflow dropdown and group heads show workflowLabel, falling back to raw id
- Zoom window math anchored to "now" — zoomWindow (TimelineLanes.tsx:450) — Steps over time view, zoom in/out — keeps end at now if window already does, else zooms about the midpoint capped at now
- Pan window math capped at "now" — panWindow (TimelineLanes.tsx:504) — Steps over time view, earlier/later — shifts by half width, never past now
- "Way back" state retained through repeated zooms — TimelineLanesScreen (TimelineLanes.tsx:1186) — Steps over time view, zoomed — keeps the original pre-zoom span as the chip target
- URL/query persistence of view (span/since/until/filters/lane) — TimelineLanesScreen (TimelineLanes.tsx:636) — Steps over time view — parseLanesView/serializeLanesView round-trip the full view state through the view/onView hash query
- Timezone-aware rendering — TimelineLanesScreen / Axis (TimelineLanes.tsx:1100, 1751, 1494) — Steps over time view — axis, strip and footnote all state "times <tz>"
- Pointer-driven axis drag zoom overlay — Axis (TimelineLanes.tsx:1739) — Steps over time view, dragging — shows a highlighted brush region with live "drag to zoom: <range>" label
- Click vs. drag threshold on axis — Axis (TimelineLanes.tsx:170, 1736) — Steps over time view — drags under 4px are ignored as a click
- Outcome strip deadline / timeout — STRIP_DEADLINE_MS (TimelineLanes.tsx:615, 1128) — Steps over time view, outcomes slow to answer — after 20s shows "no answer after 20 s" even if lanes are drawn
- Both-ranges tooltip on page nav link — bothRanges (TimelineLanes.tsx:2393, 1221) — Graph view (page nav) — hover title on "Outcomes" tab showing both pages' exact ranges when they differ
- Page-age / refresh age claim — TimelineLanesScreen (TimelineLanes.tsx:700) — Steps over time view — ties RefreshControl's displayed age to this screen being the active page
- Workflow group link to workflow page — GroupHead (TimelineLanes.tsx:1934) — Steps over time view, workflow read ok — group title links to the workflow page
- "Agent not read" / unread group fallback — GroupHead (TimelineLanes.tsx:1910) — Steps over time view, task read failed for a lane — "Agent not read" group with lane count

