# Checklist beside a working panel (`?finish=checklist`)

## The idea

Everything stays on one screen. A status line across the top says what swarm install is doing at this moment, read from the install record: waiting, applying a pass, running verify (with time left), not running (with when it was last seen), or finished with its ready line. A compact checklist on the left has one line per step. Each line shows its mark, its name, who acts now (You, A teammate, Google, GitHub, swarm install, The console) and since when. Waits on other people stay listed under their step: Lea's invite while Google Groups catches up, Lea's link with the hours left on it. Beside the checklist sits the selected step's working panel. It shows who the step is waiting on (who, what, since, how long it usually takes), then today's controls for that step, unchanged. The panel moves by itself to the next step **you** can act on. Pick another step and it stays there until you click "Follow the next step again". It is built for someone who leaves and comes back over an afternoon: at a glance they see what moved, who has it now and since when.

## + What it gets right

+ Who has each step, and since when, is always on screen. Nobody has to open a step to learn whether it waits on them (frames 6, 7, 9).
+ Waits on other people never hide the work you can do. The panel follows the next step *you* can act on, so while Google, GitHub or a teammate holds one step, the panel is already on the next (frames 3 and 5: the client is applying and the panel is on the GitHub App; the self-check waits and the panel is on the first team).
+ Coming back hours later costs one look. Lea's unused link shows "21 h left on the link" under Claude accounts, and the status line names who is being waited on (frame 7).
+ The terminal is one sentence at the top, from the record: seen 20 s ago; not running, last seen 14 min ago, run swarm install again; verify at check 3 of 8 with about 9 min left; the ready line itself (frames 9 to 12).
+ Nothing is ticked by hand. Every mark, actor and time comes from `GET /v1/admin/setup`. A time the record does not hold says "since: not recorded" (the smoke agent's start, frames 7 and 8), never "now" or 0.
+ Every step's own controls are today's components (AdminSetupSteps.tsx rows), so every rule in the brief holds unchanged: the secret field, the `.apps.googleusercontent.com` check, the manifest post, the invite that opens Google Admin, the one-time link, Hand over offered only once the smoke agent has succeeded, Send locked until verify passes.
+ One person: the step reads "Your workspace", and the list says plainly that there is no team step and no invites (frame 14).

## ! What it costs the owner

! **Eight lines, not seven.** The smoke agent gets its own line (6) between Claude accounts and Verify. Today the strip holds the first team's step open until the smoke succeeds. Here the team is done once its workspace is ready, and the smoke has its own mark, actor and Retry. The counts read "N of 8" (every frame; the smoke's own failure is frame 8). The other direction and today's strip count 7.
! **A dense first screen.** The checklist, the panel and the status line all show at once. On a 1280 by 800 laptop the "This install" facts start below the fold (frame 16). On a phone the panel comes first and the checklist sits below it (frame 17), so the phone shows "1 of 8 done · 7 left" in the status line but not the list until the person scrolls.
! **Usual times are the page's own estimates, not the record's.** "Usually 2 to 4 min", "usually about 3 min" and "usually 5 to 10 min" are constants in ChecklistFinish.tsx (the panel's "Waiting on" box, frames 3, 5, 6 and 7). Serving them would take a route field (below). Until then, they are words the page cannot prove.
! **A shared piece exported.** AdminSetupSteps.tsx now also exports `ConsoleChecks` (one `// MOCK-HOOK` line). The verify panel draws the console task and workflow under its own failure line, so the cause is not drawn twice (frames 10 and 11).
! **The Claude sign-in on Lea's page still asks her to paste the code Claude shows** (frames 13 and 18). That is today's `PoolSignIn` (SetupLink.tsx): Claude's subscription sign-in gives no redirect back. The direction keeps it unchanged. It is the one paste step left in the flow. It is between Claude's page and this one, never with a terminal.
! **The 1512 by 862 frame's PNG is scaled.** The live browser gate allows only its presets (1440 by 900, 390 by 844) and refuses a viewport override or a separate Chromium. So frames 15 and 16 were laid out in an iframe at exactly 1512 by 862 and 1280 by 800. The 1512 PNG is that layout scaled to 1440 by 821 (0.952). Its HTML is the exact layout, at 1512 by 862.
! The persona shown in the console's own chrome is the fixture build's (alice, platform admin), not Ana. The fixtures' persona names are outside this direction.

## Routes the server would need (drawn here, not served today)

- `GET /v1/admin/setup` → `engine.verify.detail`: verify's one-line cause. Today swarm install prints it only in the terminal. Frame 11 draws it from a fixture constant (`VERIFY_DETAIL`, used only at the verify-failed moment).
- `GET /v1/admin/setup` → `teams[].smoke.started_at`: when the smoke agent started. Without it the page says "since: not recorded" (frames 7 and 8).
- Optional: `GET /v1/admin/setup` → a `typical_s` per wait (sign-in pass, pass 3, workspace build, smoke, Groups). This would replace the page's "usually" constants.
- `GET /v1/admin/setup` → `accounts[].link_made_at`: frame 7's "since" time for Lea's link is read as `link_expires` minus 24 h. That holds only while links last exactly 24 h.

## Where every item lives

The scaffold's features.md was not in console-finish-base.patch (it holds code only: ConsoleFinish.tsx, consolefinish.css, moments.ts and the hooks), so it is not in this bundle. This map follows the brief's list, "What every direction must cover", item by item.

| Item | Where |
|---|---|
| Arrival: what is done, what is left, about how long | Status line ("1 of 8 done · 7 left · usually 20 to 60 min in all"); Cloud line "done · 11 of 11 stages"; the panel on the first step (frames 1, 15, 16, 17) |
| Sign-in client: three Google pages, the id, a hidden secret | Panel of step 2: today's `SignInPages` and `SignInClientForm` (frames 1 and 2); applying: the "Waiting on swarm install" box (frame 3) |
| GitHub App, and GitHub's callback page | Panel of step 3 (frame 3); the callback is drawn inside the same layout with today's `GitHubAppCallback` (frame 4); installed, with the self-check waiting on pass 3 (frame 5) |
| First team (or one person's own workspace) | Panel of step 4 (frame 6, picked by hand); one person: "Your workspace", no invites (frame 14) |
| Invites watched in Google Groups | A sub-line under step 4, "Google · lea@example.com", plus the panel's "Waiting on" box; today's `InviteControl` opens Google Admin's members page (frame 6) |
| Claude accounts and a teammate's one-time link | Panel of step 5: today's `AccountLinkForm`; Lea's unused link stays under step 5 with the hours left (frame 7) |
| The link page as the teammate sees it | Lea's own page: her part in three lines beside today's `SetupLinkScreen`, opened by her click (frames 13 and 18) |
| Smoke agent, its failure, Retry | Step 6 line and panel: cause, fix, "Retry the smoke agent" (frames 7 and 8) |
| Hand-over | Step 7's panel holds today's "Hand over to swarm install", offered only by `consoleDone` (frame 9) |
| Verify running, failed, ready | Status line plus step 7's panel (frames 10, 11, 12) |
| Send to your team, locked until verify passes | Step 8: "locked until verify passes"; then the one plugin line with Copy (frame 12) |
| Add-ons, optional and off the path | Footer of the checklist ("optional, off; turned on from the terminal"), and listed under "This install › Capacity in full, and the add-ons" |
| Progress from the record, never by hand | Every mark; the panel foot says so |
| Who is waited on, since when, how long usually | List chips plus the panel's "Waiting on" box |
| Is the terminal still there | Status line, every frame |
| Unknown shown as unknown | "since: not recorded", "not recorded" in "This install" |
| Terminal closed: what still works, the one thing to do | Status line, red (frame 9); the hand-over note in step 7 |
| Coming back hours later with a link unused | Frame 7 |
| Capacity, the installer, Health | "This install" box under the panel: sized for a team of 5, $1,000 a month, 2 accounts, ceiling, release, the Health link; Capacity in full behind a disclosure |
| Phone, 390 wide | Frames 17 and 18 |
