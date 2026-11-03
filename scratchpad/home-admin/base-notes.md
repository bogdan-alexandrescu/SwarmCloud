<!-- MOCK-HOOK FILE -->
# Home redraw: the base scaffold (step 'foundation', 2 November 2026)

Mock-ups only. Nothing here is committed; the whole scaffold is `home-redraw-base.patch`
(apply from the repository root: `git apply home-redraw-base.patch`). Every added line
is marked `// MOCK-HOOK` (`/* MOCK-HOOK */` in CSS); every new file opens with a MOCK-HOOK FILE line.

## Switches (read once per page load, from the address)

| switch | values | what it does |
|---|---|---|
| `?homeset=` | `base`, `today`, `desk`, `place` (any value turns the hard-case fixtures on) | Present: every read below answers the hard case. `today`: the console's real pages, on the hard case. Any other value: the console's main slot draws that direction's screen whatever the route; the left menu (Spine) and header stay the console's own. |
| `?homescreen=` | `home`, `overview`, `people`, `person`, `settings`, `workspaces` (default `home`) | which screen of the direction |
| `?homestate=` | `rest`, `answer`, `answered`, `open`, `changed` (default `rest`) | which state; passed to every screen as `state`. Base draws `open` only on Home (the "What these count" details open); the others render rest. |
| `?homeperson=` | an email | the person screen's subject (default the long-named contractor) |
| cookie `swarm_qa_persona` | `member`, `platform-admin`, `workspace-admin` | who the fixture build answers as (api.ts `readFixturePersona`; fixtures.ts reads the same cookie) |

Without `?homeset` nothing changes: every hook is behind `HOMESET !== null`.
The live browser refuses `Storage.setCookies`, so the render kit writes the cookie with
`document.cookie` on `/help` and then loads the frame's address (see verification.md).

## Files

* `apps/swarm-ui/src/homeredraw/fixtures.ts` — the hard case and the switches.
  * `HOMESET`, `HOMESCREEN`, `HOMESTATE`, `HOME_TAKEOVER` (homeset set and not `today`), `HOME_PERSONA`.
  * `HOME_WORKSPACES` — the 6 group workspaces (`eng` = engineering, keeps today's fixture id so the
    workspace picker logic still selects it; `design`, `data-platform` (long group address), `support`,
    `research`, `sales-ops`), each with `default_role` and `setup` (steps done of 7, for a direction's own
    "furthest behind" ordering).
  * `HOME_ADMINISTERED` — platform admin: all six; workspace admin: `eng`, `design`; member: none.
  * `HOME_ME` — platform admin `bogdan@example.com` ("Bogdan", personal `u-bogdan`); workspace admin
    `ana.moreau@example.com` (`u-ana`); member `kenji.sato@example.com` (`u-kenji`).
  * `HOME_PEOPLE` — the 60 people who signed in (`HomePerson`: email, name or null, personal workspace,
    workspace state, role per workspace, platform_admin, github, last_active). Includes the long name
    "Maximiliana Aleksandra Kowalczyk-Wiśniewska" with a 54-character address, 2 workspace requests
    (Priya Raman, tomas.garcia with no display name), a new person with no workspace (Hana Okafor, state `none`).
  * `OVERRIDES` (internal) — the 9 roles of their own; `HOME_OVERRIDE_NOT_IN_GROUP` — the one kept for
    someone not in the group yet (lukas.becker in engineering, "starts next month").
  * `homeActingTenant(chosen)` — the acting workspace: the picked one if the persona may pick it, else
    `eng` for an admin, the personal workspace for the member.
  * `homesetRead(template, url, chosen)` — the `readAs` routes: `/v1/profile`, `/v1/workspace/roles`,
    `/v1/workspace/join-link` (on), `/v1/workspace/members`, `/v1/access/members`, `/v1/admin/people`
    (platform: `PeopleDoc` with 60 rows, 2 workspace requests + 1 loan request from Kenji Sato, 2 lendable
    accounts, 2 admins, audit; workspace admin: `ScopedPeopleDoc` over eng and design; member: refused),
    `/v1/admin/workspaces/roles`, `/v1/admin/roles/person`, `/v1/admin/workspaces/{tenant_id}/roles`.
    Anything else falls through to today's `FIXTURE_READS`.
  * `homesetMe`, `homesetTenantChoices` (`/v1/tenants/me`, `/v1/tenants/mine`).
  * `homesetTasks(base)` — `/v1/tasks` (and every state read built on it): clones of today's fixture rows
    per state, so every field keeps its real shape. Admin personas: RUNNING 30, LEASED 1, DISPATCHED 1,
    STARTING 2 (= 34 holding capacity), READY 71 + PARKED 15 (= 86 waiting; 6 parked on provider quota,
    7 on CI, 2 on the caller's owner questions), FAILED 5, SUCCEEDED 48, CANCELLED 3; one RUNNING task holds
    for a person at its browser (`human_wait`). Member: 2 running, 1 starting, 1 ready, 6 succeeded, nothing
    waiting on them. Never QUEUED or DEAD_LETTERED (no task document holds them).
  * `homesetStats`, `homesetCapacity` (the first pool at its ceiling), `homesetLeases` (one fresh lease per
    capacity-holding task), `homesetApprovals` (one run-plan approval waiting on the admin personas; none for
    the member), `homesetPeopleSetup` (Config setup's "People" step counted from the overrides),
    `homesetMembers` (engineering let 2 people in by join request: HOME_PEOPLE[10] and [11]).
* `apps/swarm-ui/src/homeredraw/HomeRedraw.tsx` — the screens and the switch strip.
  * `HomeRedraw({ go })` — drawn by App.tsx in `<main className="work">` when `HOME_TAKEOVER`.
  * `DIRECTIONS` — `{ base, desk, place }`, each `{ screens: Record<HomeScreen, (p: ScreenProps) => ReactNode>, fromBase }`.
    A direction step replaces its own entry's `screens` and sets `fromBase: false`. Until then desk and place
    draw base with a line saying so.
  * `ScreenProps` — `{ go, state }`. `HOME_SCREENS`, `HOME_STATES`, `HOME_DIRECTIONS`.
  * `useRead(load)` — a read as a hook (null until it lands; pass a stable function).
  * `useWaiting()` — what waits on the signed-in person: `questions` (PARKED on owner questions they
    submitted, `ownerQuestionsHold`), `handoffs` (`needsYou`), `failed` (their FAILED tasks), `approvals`,
    `requests` (`waitingRequests` from People.tsx; platform admin only). Each null until read: draw a dash.
  * Base screens, each from the console's existing components:
    * home — Waiting on you (tiles + one line per item, links out) with Automate's `WaitingOnYouCard`; The
      deployment (Holding capacity, Waiting, Failed `StatTile`s, a details toggle); `SetupCard`.
    * overview — `OverviewScreen` exactly as App.tsx draws it (setup card, Waiting on you, every region).
    * people — `WorkspacePeopleScreen` (Config › People and roles) then, for an admin, `PeopleScreen`
      (Admin › People and roles: Waiting for you, the grid, Manage drawer, Admins, audit, Workspace roles card).
    * person — `PersonWorkspaceRoles` (a person's roles in every group workspace; platform admin only, the
      others are told the read would be refused).
    * settings — `WorkspaceSettingsScreen` parts `settings`, `setup` and (platform admin only) `new`.
    * workspaces — `WorkspacesYouAdminScreen`.
* `apps/swarm-ui/src/styles/homeredraw.css` — `.hr-frame`, `.hr-stack`, `.hr-switch`, `.hr-tiles`, `.hr-list`,
  `.hr-note`; tokens only (`--line`, `--surface-2`, `--text-dim`).
* `apps/swarm-ui/src/homeredraw/features.md` — the inventory (also in artifacts).

## Hooks in existing files (all `MOCK-HOOK`, all behind `HOMESET !== null`)

* `src/api.ts`: import; `loadCapacity`, `loadStats`, `loadLeases` wrap today's fixture; `fixtureTasks`
  returns `homesetTasks(...)`; `fixtureActingTenant` and `fixtureMe` answer for the acting workspace;
  `loadMyTenants`; `loadApprovals`; `fixtureOnboarding`'s `people` step; `readAs` asks `homesetRead` first
  (one existing line changed, marked).
* `src/People.tsx`: import; `loadWorkspaceMembers` serves `homesetMembers` (today's fixture serves none).
* `src/App.tsx`: imports; the main slot's ternary starts with `HOME_TAKEOVER ? <HomeRedraw go={go} /> :`.

## Known gaps a direction should know (not hidden)

* `/v1/onboarding` keeps today's fixture body (alice@example.com, 4 of 7 done, Enable orgs failed) for every
  workspace, so Workspaces you admin shows the same setup progress for each; `HOME_WORKSPACES[].setup` carries
  the intended per-workspace progress for a direction's own drawing.
* Overview's other reads (providers, accounts, workflows, spend's attempts) keep today's fixtures; their
  "Needs a look" problems (throttled provider, an account to sign in again, a workflow not judged) still show.
* Ages read against the fixture clock (19 October 2026, api.ts FIXTURE_CLOCK) while some cards read against
  the real clock, so a few ages say "13d ago" as today's fixture console does.
* Base honours `?homestate=open` on Home only.
