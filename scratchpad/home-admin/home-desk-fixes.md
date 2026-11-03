# fix-desk: the review's 20 findings and what changed

Direction "A personal Home and a people page across workspaces" (desk). Mock-ups only: nothing committed, no pull request.
Every change is in the files the build's variants.patch already adds or marks: `homeredraw/desk/Desk.tsx`, `homeredraw/fixtures.ts`, `styles/homeredraw-desk.css` (MOCK-HOOK FILEs) and MOCK-HOOK lines in `App.tsx` and `Spine.tsx`. All 19 frames were re-rendered, light and dark.

| # | Finding | What changed | Frames |
|---|---|---|---|
| 1 | "Finished today" drawn twice and disagreeing (tile 56, band 0) | The lifecycle band is no longer drawn on desk's Overview (CSS), and its left-menu jump goes with it; the tiles are the one place (notes.md says so, OV-28..33 remapped). | 6, 7, 8, 9, 18 |
| 2 | "Pools at their ceiling 3 of 20" counted a 0/0 pool | A pool with no allowance is not at a ceiling (`l > 0` in the tile and in the opened pool); the fixture leaves exactly one pool (global) full. Tile reads "1 of 20 · global". | 6-9, 18 |
| 3 | "What waits on it" newest first though said oldest first | Rows sorted oldest first; ages to the minute (7 h 3 min, 6 h 59 min, 6 h 55 min ...) so the order can be seen. | 7 |
| 4 | "waiting on scan-terraform (report)" ×7 and ×6; "eligible in now." | The parked fixture rows no longer clone the template's per-item wait text, so the line reads "waiting for CI ×7 · provider quota ×6"; quota parks carry a future `next_eligible_at`, so it reads "The first becomes eligible in 49m." | 6-9, 18 |
| 5 | Token cost footer cut mid-word at 1280 | Footer items wrap inside the card; measured 291/291 (was 306/291). | 9 (and all Overview) |
| 6 | Phone Needs a look links were bare arrows | Below 560px the link words stay visible on desk's Overview; measured 28-151px each (was 1px). | 18 |
| 7 | Caption said 7 items of four kinds, page counts 8 with "At a browser" | The agent at a browser is now Bogdan's own (fixture), and the caption names all eight items and five kinds. | 1, 2, 17 |
| 8 | "31 of yours waiting" vs breakdown summing to 29 | One count and one breakdown from the same list: the breakdown now includes "2 for your answer"; 24 + 3 + 2 + 2 = 31. The header chips link to Your work. | 1, 2, 17 |
| 9 | After the answer, Your work 12 vs header 11; agent "ready to run" | Your work counts what runs (12 = header's 12, the handoff agent now Bogdan's); the answered agent shows first marked "answered · waiting for room, first in line" with "1 back in line" beside the count; the breakdown moves it from "your answer" to "room in a pool" (25 + 3 + 2 + 1 = 31). Caption says "out of Parked, first in line". | 3 |
| 10 | New person: two of three Start ways live | All three are disabled buttons with the reason as their title, plus the line under them. | 5 |
| 11 | New person's menu showed "Your workspace · copy id" and KS | Spine's workspace tile reads "No workspace yet · request yours from Home"; the profile mark is HO with her address. | 5 |
| 12 | Phone Recently finished titles in a 59px cell | On a phone each row stacks: title on its own line (measured 313px), then state, pull request, age. | 17 |
| 13 | Bare codes after titles | Each reads "agent hs003" with a tooltip "agent id task_hs003: tells apart agents that share a title". | 1-4, 7, 17 |
| 14 | Document title "Overview" on Home | The tab names the desk screen drawn: "Home · SwarmCloud", "People and roles · SwarmCloud", "Workspace settings · SwarmCloud". | all |
| 15 | Mei Chen and fatima.haddad lacked "not in group yet" | Both are fixtures given a role before joining; their cells carry "not in group yet", the drawer says "own role · not in group yet, kept till then", and Recent admin actions reads "made Mei Chen admin in design (was member) · kept; applies once in the group". Caption of 12 says so. | 10, 11, 12, 19 |
| 16 | Drawer GitHub a bare dash | "— not known until they sign in" (or "— the people read sent no GitHub state"; "not connected" for none). | 11 |
| 17 | Member's roles named nobody | Under "Your roles in group workspaces": "Who holds a role of their own in engineering" (Ana Moreau admin, Oluwaseun viewer, lukas.becker viewer · not in group yet), "Every platform admin · admin", "Everyone else in engineering's group holds the default, member", read from `GET /v1/workspace/roles`, which any viewer may read (notes.md, routes). | 14 |
| 18 | Workspace admin's switcher never shown; Config setup strip not drawn | The Config setup strip is drawn on top of Workspace settings; a workspace admin of three or fewer sees the switcher's list open, so frame 15 shows Ana's two (design, furthest behind; engineering) and her own. Caption updated. | 15, 16 |
| 19 | notes.md frame numbers off by one for 9-16 | Every citation corrected (scoped read 13; switcher and Add 16, absent in 15; three roles 10, 13, 14; Recent admin actions 12; grid 10). | notes.md |
| 20 | NAV-03..08, NAV-10 mapped to undrawn places | The Config panel now draws today's Config rows (GitHub access, Repositories, Secrets, MCP servers), the setup meter, People and roles and Workspace settings, and for a platform admin an Admin heading with the seven Platform pages and the line "People and roles and Workspaces you admin moved to Config". notes.md gains "Left undrawn" (NAV-04 badges, NAV-07 badges, NAV-08 In Automate) and each NAV row says which frames draw it. | 10-16, notes.md |

## How it was rendered

The live browser refuses cookie writes over CDP and viewport changes, so the persona cookie is set with `document.cookie` on the dev server's origin, 1440x900 frames are the browser's own viewport, and the 1512x862, 1280x800 and 390x844 frames are rendered in a same-origin iframe of exactly that size (screenshot clipped to it). Dark frames are the same page with `data-theme="dark"`, saved as `<name>-dark.png`.

## Checked

- `npx tsc -b --noEmit` in apps/swarm-ui: clean.
- `npx vitest run --pool=forks --poolOptions.forks.singleFork src/__tests__/qa.g1.shell.test.tsx src/__tests__/qa2fix04.spine.test.tsx src/__tests__/tenant.switcher.test.tsx src/__tests__/config.strip-checklist.test.tsx`: 4 files, 55 tests passed.
- Every added line in a tracked file carries MOCK-HOOK (`git diff -U0 | grep '^+' | grep -vc MOCK-HOOK` → 0).
- Measured in the live page: 1280 footer 291/291; phone link words 28-151px; phone finished titles 313px.
- Looked at every frame: all light frames at full size for Home, Overview, People, Settings; dark as contact sheets of all 16 desktop frames plus the three phone frames.

## Not changed

Left as found (not in the verdict): the left menu's "On this page" for the new person still lists Your work and Recently finished, which that page does not draw; the Finished today tile counts from the stats read, which is not a today-only count.
