# Console finish: what it draws today (read from the code, 2026-11-02)

## Exported components and their props

### AdminSetup.tsx
- `stripSteps(state: SetupState): StripStep[]` — exported function, builds the strip's 7 steps.
- `SetupStrip({ state }: { state: SetupState })` — exported component, the strip (`cfg-strip`).
- `SetupLead({ state, now }: { state: SetupState; now?: number })` — exported component, the lead sentence (`AdminLead`).
- `AdminSetupPage({ state, now, onChanged }: { state: SetupState; now?: number; onChanged?: () => void })` — exported component, the whole page body for one read.
- `AdminSetupScreen({ now, callback }: { now?: number; callback?: string })` — exported component, routes `/admin/setup` or the GitHub callback.
- `AdminSetupHealthScreen({ now }: { now?: number })` — exported component, `/admin/setup/health`.
- `AdminSetupCallbackScreen({ search }: { search: string })` — exported component, `/admin/setup/github-app/callback`.
- Not exported: `StepMark`, `SetupRead`, `SetupFirstRunScreen`, `workspaceState`, `interface StripStep`, `STRIP_CLASS`, `CLI_STAGES`, `FAILED_WORKSPACE`.

### AdminSetupSteps.tsx
- `PLUGIN_MARKETPLACE`, `SETUP_REFRESH_MS`, `VERIFY_CHECKS`, `ONE_PERSON_LINE`, `ADMIN_GROUPS_URL`, `WAITING_FOR_PASS_3`, `SELF_CHECK_RETRY_MS` — exported constants.
- `releaseChannel(release: string | null): string`, `pluginMarketplace(channel: string): string`, `pluginLine(hostname: string, release: string | null): string`, `spanWords(seconds: number): string` — exported helper functions.
- `isOnePerson(state: SetupState): boolean`, `firstWorkspace(state: SetupState): FirstWorkspace | null`, `accountSignedIn(state, a: SetupAccount): boolean`, `signedInCount(state: SetupState): number`, `smokePassed(state: SetupState): boolean`, `smokeHold(smoke: SetupSmoke | null, signedIn: boolean): StepState | null`, `pass3Ran(state: SetupState): boolean`, `consoleDone(state: SetupState): boolean`, `selfCheckDue(state: SetupState): boolean` — exported predicates, all exported.
- `verifyWords(v: SetupVerify, nowMs: number): { progress; elapsed; expected; deadline }` — exported.
- `useSetupState(intervalMs?: number): { result: Result<SetupState>; reload: () => void }` — exported hook.
- `interface FirstWorkspace { workspace; progress; grant; smoke; pool_accounts }`, `interface Fact { state; name; text }`, `type StepState = 'done'|'in_progress'|'failed'|'todo'` — exported types.
- `Facts({ items, label }: { items: Fact[]; label?: string })` — exported component, one `ob-list` of facts.
- `StepFailure({ cause, fix, onRetry, busy, retryLabel = 'Retry' })` — exported component, a failure block with Retry.
- `SendToTeam({ state }: { state: SetupState })` — exported component, side card.
- `CapacityCard({ sizing }: { sizing: SetupSizing | null })` — exported component, side card.
- `InstallerCard({ state, now }: { state: SetupState; now?: number })` — exported component, side card.
- `AdminSetupSteps({ state, now, onChanged }: { state: SetupState; now?: number; onChanged?: () => void })` — exported component, the whole checklist + its two side cards above it.
- `GitHubAppCallback({ search }: { search?: string })` — exported component, the GitHub App callback body.
- Not exported (internal row builders and widgets): `signInRow`, `cloudRow`, `githubRow`, `GitHubAppPanel`, `SignInPages`, `SignInClientForm`, `SignInReplace`, `TeamForm`, `GroupRefusal`, `teamPeople`, `grantFact`, `smokeFact`, `SmokeRetry`, `teamFacts`, `memberFacts`, `InviteControl`, `personalRow`, `teamRow`, `TeamRetry`, `accountFact`, `AccountLinkForm`, `accountsRow`, `addonsRow`, `VerifyFinish`, `runStep`, `useRunState`, `runFact`, `ConsoleChecks`, `verifyRow`, `sendRow`, `Checklist`, `useAction`, `openTabNow`, `postManifest`, `useNow`, `useAutoSelfCheck`.

### AdminSetupTeam.tsx
- `teamBody(f: { group; name; admin; invites }): { group; display_name?; first_admin?; invites }` — exported function, the form-to-API-body mapping.
- `teamAddLine(f): string` — exported function, the `swarm team add` CLI line for the form's current values.
- `AdminSetupTeam({ teams, onChanged }: { teams: SetupTeam[]; onChanged?: () => void })` — exported component, the "Add a team" flow.
- Not exported: `arg`, `teamFact`.

### SetupLink.tsx
- `REDEEM_ROUTE`, `PENDING_KEY`, `OWN_SIGN_IN_WINDOW`, `REDEEM_RETRY_MS`, `SIGN_IN_RETRY_MS`, `SIGN_IN_RETRIES` — exported constants.
- `redeemSetupLink(token: string, value?: string): Promise<Result<unknown>>` — exported function, `POST /v1/setup/links/redeem`.
- `linkRefusal(e: ApiError): { line: string; retry: boolean }` — exported function, turns an API error into the page's wording.
- `SetupLinkScreen({ token }: { token: string | null })` — exported component, `/admin/setup/link/<token>`.
- Not exported: `interface PoolLink`, `type Opened`, `openedFrom`, `valueKind`, `readPending`, `writePending`, `OpenLink`, `VALUE_WORDS`, `ValueForm`, `Written`, `type SignIn`, `PoolSignIn`.

## Strip (`SetupStrip`, `stripSteps` — AdminSetup.tsx)
- Compact header: "Setup {done} of {steps.length}" — `SetupStrip` (AdminSetup.tsx) — appears always, phone-width compact view.
- Compact next line: "Every step done" or "Next: {next.label}, {next.line}" — `SetupStrip` (AdminSetup.tsx) — appears when `next===null` / `next!==null`.
- Step 1 "Cloud": line `by swarm install, {cloudDone} of {CLI_STAGES.length} stages` — `stripSteps` (AdminSetup.tsx) — state `done` when all 11 CLI stages are `done`, else `in_progress`.
- Step 2 "Sign-in client", not read: "not read yet" — `stripSteps` — `sign_in_client===null`.
- Step 2 "Sign-in client", applied: `{client_id ?? 'the client'} on IAP` — `stripSteps` — `sign_in_client.state=='applied'` (strip state `done`).
- Step 2 "Sign-in client", saved: `saved {client_id}, waiting for the infra pass` — `stripSteps` — `sign_in_client.state=='saved'` (strip state `in_progress`).
- Step 2 "Sign-in client", waiting: "not yet: three Google pages, then its id and secret" — `stripSteps` — `sign_in_client.state=='waiting'` or null-but-present (strip state `todo`).
- Step 3 "GitHub App", not started: "not yet" — `stripSteps` — `github_app===null || !github_app.started`.
- Step 3 "GitHub App", self-check failed: `self-check failed: {app.self_check.reason}` — `stripSteps` — `github_app.self_check.state=='failed'` (strip state `failed`).
- Step 3 "GitHub App", not installed: `created, install it on {org}` — `stripSteps` — `github_app.installed_on===null`.
- Step 3 "GitHub App", done: `installed on {installed_on}` — `stripSteps` — `installed_on!==null && self_check.ok===true`.
- Step 3 "GitHub App", waiting pass 3: `installed on {installed_on}, waiting for swarm install's infra pass 3, which gives swarm-api the App's id` — `stripSteps` — `self_check.state=='waiting_for_infra_pass_3'`.
- Step 3 "GitHub App", otherwise: `installed on {installed_on}, self-check next` — `stripSteps` — installed, no self-check result yet.
- Step 4 "First team"/"First workspace", none yet: "your personal workspace, filed on the next read" / "not yet" — `stripSteps` — `firstWorkspace(state)===null`, label flips on `isOnePerson(state)`.
- Step 4, in progress/failed/done: `{team.workspace}{, ws}{, smoke state}` — `stripSteps` — team or personal workspace filed; `failed` when workspace state is `failed`/`denied`; `done` once smoke is `succeeded` (or n/a before any signed-in account).
- Step 5 "Claude accounts", before team: "after the first team" — `stripSteps` — `team===null && !onePerson`.
- Step 5 "Claude accounts", none made: "not yet" — `stripSteps` — `state.accounts.length===0`.
- Step 5 "Claude accounts", counted: `{signed} of {accounts.length} signed in` — `stripSteps` — accounts exist; `done` once `signed>0`.
- Step 6 "Verify", not started: "handed over to swarm install" / "after the console steps" — `stripSteps` — `v===null`, split on `state.finished`.
- Step 6 "Verify", passed: `passed, {done} of {total} checks` — `stripSteps` — `v.result=='passed'` (strip state `done`).
- Step 6 "Verify", failed: `failed at {v.check ?? 'a check'}` — `stripSteps` — `v.result=='failed'` (strip state `failed`).
- Step 6 "Verify", running: `{v.check ?? 'starting'}, {done} of {total}` — `stripSteps` — verify in progress.
- Step 7 "Send to your team", locked: "unlocked when verify passes" — `stripSteps` — `v?.result!=='passed'`.
- Step 7 "Send to your team", unlocked: "the plugin line is ready" — `stripSteps` — `v?.result=='passed'` (strip state `done`).
- Next-step small text: `Next: {step.line}` inside the active `<li>` — `SetupStrip` — on the step equal to `next`.
- Count footer, all done, never verified: "every step done and verified" — `SetupStrip` — `next===null && ready_at===null`.
- Count footer, all done, verified: `swarm install saw this at {time}` — `SetupStrip` — `next===null && ready_at!==null`.
- Count footer, CLI present: "the CLI is waiting on this page" — `SetupStrip` — `next!==null && state.cli_running`.
- Count footer, CLI absent: "swarm install is not running: run it in a terminal" — `SetupStrip` — `next!==null && !state.cli_running`.

## Lead (`SetupLead` — AdminSetup.tsx)
- Kicker: "Setup is complete" or `Step {n} of {total} · next` — `SetupLead` — all steps done vs. one pending.
- All done, hostname known: title `SwarmCloud is ready at {hostname}`, body "Every step done and verified. Send the plugin line to your team; Admin › Setup › Health shows the hourly doctor's findings from here on, and swarm doctor checks this machine and the deployment from a terminal." — `SetupLead` — `steps.findIndex(not done)===-1` and `hostname!==null`.
- All done, no hostname: title "SwarmCloud is ready" — `SetupLead` — same, `hostname===null`.
- Cloud step active: title `swarm install is doing the cloud part: {n} of 11 stages`, body "This page finishes the people part once swarm install hands over to it." — `SetupLead` — next step key `cloud`.
- Sign-in client, not read: title "Sign-in client: not read yet", body "This deployment's swarm-api does not report the sign-in client, so this page cannot say whether it is saved; the row below says the same. {cli}" — `SetupLead` — `sign_in_client===null`.
- Sign-in client, saved: title `Sign-in client: {step.line}`, body "swarm install's infra pass adds it to IAP's programmatic clients and continues on its own; the GitHub App comes next. {cli}" — `SetupLead` — `sign_in_client.state=='saved'`.
- Sign-in client, waiting: title "Sign-in client: create the Desktop client on Google's pages and save its id and secret here", body "No Google API creates it, so the row below links Branding, Audience and Create client in order and says what to set on each; swarm install waits for the save and continues on its own. {cli}" — `SetupLead` — `sign_in_client.state=='waiting'`.
- GitHub App: title `GitHub App: {create it from the manifest | step.line}`, body "swarm install did the cloud part; this page finishes the people part. {cli}" — `SetupLead` — next step key `github`.
- First team/workspace, through to smoke: title `{First workspace|First team}: {step.line}`, body "Its smoke agent failed: the row below says why and retries it, and the hand-over to swarm install waits for it to succeed. {cli}" (smoke failed) or "Its smoke agent runs on the first signed-in Claude account, and the hand-over to swarm install waits for it to succeed. {cli}" (else) — `SetupLead` — team/personal workspace exists and (not one-person or workspace ready) and not failed.
- First workspace, one person, not through: title `First workspace: {step.line}`, body "One-person install: your personal workspace is the first workspace, so no team or invites are needed; the Claude pool accounts come next. {cli}" — `SetupLead` — `isOnePerson(state)` and workspace not ready yet.
- First team, no team yet: title "First team: name the Workspace group that becomes the first workspace" (or `First team: {step.line}` when failed), body "Its members are the team; the Claude pool accounts come next and are owned by it. {cli}" — `SetupLead` — team (not one-person) and not through.
- Claude accounts: title `Claude accounts: {step.line}`, body "Each subscription's owner signs in once, through a one-time link this page makes (the page opens it for you when the account is your own); {your personal workspace's|the first team's} smoke agent runs on the first one. {cli}" — `SetupLead` — next step key `accounts`.
- Verify, waiting on smoke: title "Verify waits for the first workspace's smoke to succeed", body "The hand-over to swarm install is offered once it has. {cli}" — `SetupLead` — `v===null && finished===null && !consoleDone(state)`.
- Verify, ready to hand over: title "Every console step is done: hand over to swarm install" or "Verify: handed over to swarm install", body "swarm install runs smoke, GKE proof, race, Scheduler targets, digests and four end-to-end runs." or "Run swarm install in a terminal: it runs verify." — `SetupLead` — `v===null`, split on `finished`/`cli_running`.
- Verify, failed: title `Verify failed at {v.check ?? 'a check'}`, body "swarm install printed which part failed and why; fix it, then run swarm install again." — `SetupLead` — `v.result=='failed'`.
- Verify, running: title `Verify: {step.line}`, body `About {spanWords(left)} left. Send to your team unlocks when it passes.` or "Send to your team unlocks when it passes." — `SetupLead` — verify running; first form while time remains, second once `expected_end` has passed.
- Send: title "Send to your team", body "Unlocked when verify passes." — `SetupLead` — next step key `send`.

## Checklist rows (`AdminSetupSteps` — AdminSetupSteps.tsx)
### Sign-in client row (`signInRow`)
- Not read: state `todo`, line "Not read yet: this deployment's swarm-api does not report the sign-in client, so this page cannot say whether it is saved" — appears when `sign_in_client===null`.
- Applied: state `done`, line `{saved} · on IAP's programmatic-clients list: sc signs in with it`, body `SignInReplace` button "Replace the client" (ghost) — appears when `state=='applied'`.
- Saved: state `in_progress`, line `Saved {saved}; waiting for swarm install's infra pass to add it to IAP's programmatic clients`, body `SignInReplace` — appears when `state=='saved'`.
- Waiting: state `todo`, line "Three Google pages, in order, then the Desktop client's id and secret here: swarm install waits for them and continues on its own", body `SignInPages` + `SignInClientForm` — appears when `state=='waiting'`.
- `SignInPages` link 1: "Branding" → links.branding, text "app name SwarmCloud and a support email you manage (your own address or a group you own); save." — always inside the waiting/replace body.
- `SignInPages` link 2: "Audience" → links.audience, text "choose Internal, so only your organisation's accounts can sign in." — same.
- `SignInPages` link 3: "Desktop client" → links.create_client, text "Create client: application type Desktop app, name SwarmCloud sc. Keep its dialog open: Google shows the secret only there, and you copy both values into the form below." — same; shows "(page not served)" instead of a link when the href is null.
- `SignInClientForm` field "Client id", placeholder `…{CLIENT_ID_SUFFIX}`, hint "The Desktop client's id, from the dialog Google shows after Create" — in waiting/replace body.
- `SignInClientForm` field "Client secret" (type password, uncontrolled), hint "Stored in Secret Manager as a new version; never shown again, here or anywhere" — same.
- `SignInClientForm` button "Save", busy "Saving", disabledReason "Type the client id" / "Type the secret" — same.

### Cloud row (`cloudRow`)
- Done: title "Project, infrastructure, identity, edge and secrets", state `done`, line `swarm install did the cloud part: stages 0-8, 10 and 13{, finished {time}}` — appears when all 11 CLI stages are `done`.
- In progress: state `in_progress`, line `swarm install has done {n} of 11 cloud stages; it hands over to this page when they are done` — appears otherwise. No body/action; `by` is "swarm install".

### GitHub App row (`githubRow`, `GitHubAppPanel`)
- Not started: state `todo`, line `One button: GitHub creates the App on {org} from a manifest with its permissions filled in, and sends you back here; its secrets go straight into Secret Manager`, act button "Create the GitHub App" busy "Opening GitHub" — appears when `github_app===null || !started`.
- Manifest sent: state `in_progress`, line "Waiting for GitHub to send you back here with the new App. If you closed that tab, start again", act same create button — appears when `app.id===null`.
- Self-check failed: state `failed`, line `{slug} is installed on {org}, but its self-check failed`, body facts + `StepFailure` cause = `self_check.reason` or "GitHub did not answer as the App", fix text "check the App still exists on {org} and is installed there; if its private key was revoked, create the App again here. Retry runs the self-check again." with Retry button — appears when `self_check.state=='failed'`.
- Installed pending grant: state `in_progress`, line `Created and its secrets stored; install it on {org}`, act `ButtonLink` "Install on {org}" (primary, opens GitHub's installations/new) + ghost button "Check again" busy "Checking" — appears when `installed_on===null`.
- Waiting on self-check: state `in_progress`, line `Installed on {installed_on}; this page runs the self-check now` or `…; waiting for swarm install's infra pass 3, which gives swarm-api the App's id`, act button "Run the self-check" disabledReason "After infra pass 3" — appears when `self_check===null || state=='waiting_for_infra_pass_3'`.
- Done: state `done`, line `{slug} · installed on {installed_on} · self-check passed`, act ghost button "Re-check" — appears when self-check passed.
- `GitHubAppPanel` fact "Created from the manifest": `{slug} (app id {id}) on {org}`, state `done` — shown once `app.id!==null`.
- `GitHubAppPanel` fact "Secrets stored": "********", state `in_progress`/`done` — same.
- `GitHubAppPanel` fact "Install on {org}" / "Installed on {org}": "this page checks every 10 s until GitHub shows the installation" / "GitHub shows the installation" — same.
- `GitHubAppPanel` fact "Self-check": "the App's key signs and GitHub answers as the App" (passed) / the failure reason (failed) / "swarm install's infra pass 3 gave swarm-api the App's id; this page runs the self-check by itself" (pass 3 ran, not yet checked) / `WAITING_FOR_PASS_3` text (not yet) — shown once `installed_on!==null`.

### First team row / personal workspace row (`teamRow`, `personalRow`, `TeamForm`, `TeamRetry`)
- No team yet (not one-person): title "First team", state `todo`, line "A Workspace group becomes the first workspace: the same call swarm team add makes" or `…, {PUBLISHING_WAIT}`, body `TeamForm` — appears when `state.teams[0]===undefined && !isOnePerson`.
- `TeamForm` field "Workspace group", hint "The group swarm install created, with you as its owner; its members are the team. Another group? " or "A Google Workspace group you are a member of; its members are the team. No group yet? ", aside link "Create group in Google Admin" → `ADMIN_GROUPS_URL`, placeholder "engineering@your-domain" — inside the no-team body.
- `TeamForm` field "Workspace name", hint "Shown in the console; the group's name when left empty", placeholder "engineering" — same.
- `TeamForm` field "First workspace admin", hint "Administers the team's workspace", placeholder "name@your-domain" — same.
- `TeamForm` field "Invite", hint "Each is added to the group on its members page in Google Admin, which opens when you create the team; the team's row shows them as joined once the Groups API sees them", placeholder "one or more emails" — same.
- `TeamForm` button "Create the team", busy "Creating", disabledReason "Waits for infra pass 3" / "Name the Workspace group first" — same.
- `GroupRefusal` button "Add yourself to the group in Google Admin" — appears when the create fails with reason `installer_not_member`.
- `GroupRefusal` button "Create or find the group in Google Admin" — appears when reason `group_unreadable`.
- Team exists: title "First team", state `failed`/smoke-held/`done`, line `{workspace} from {group}{ · admin {admin}} · {joined} of {n} in the group`, body `teamFacts` + `SmokeRetry` + `InviteControl` (+ `TeamRetry` if failed) — appears when `state.teams[0]` exists.
- `teamFacts` fact "Tenant record": `{workspace} · from {group}, checked by membership per registered group`, state `done` — always.
- `teamFacts` fact "Namespace, quota, verify-keys and worker identity": "swarm-workspace-apply has not reported yet" or `swarm-workspace-apply: {ws}`, state in_progress/failed/done by `workspace_state` — always.
- `teamFacts` fact "First admin": `{first_admin}` or "not named" — always.
- `teamFacts`/`personalRow` shared fact "Repository grant" (`grantFact`): "{whose}'s grant over the repositories the GitHub App installation on {org} covers; read on the next refresh" (grant null) / "{whose} may write to the {n} repositories the App installation{ on {account}} covers: {repos}" (granted) / "the App is installed{ on {account}} on no repository yet: add one to the installation on GitHub, and the grant follows on the next refresh" (`waiting_for_repositories`) / "waits for the GitHub App to be installed on {org}" (else).
- `teamFacts` fact "Team git slot": "served by the GitHub App; a one-time link lets its owner enter a token instead" or `one-time link for {owner}, {expires}` — always.
- `InviteControl` facts per invitee: "is in the group" (done) / `waiting for {email} to join{ (the Groups API did not answer this read)}` (in_progress) — appears when the team has members or pending invites.
- `InviteControl` field "Invite someone", hint `Opens {group}'s members page in Google Admin, where you add them; this row shows them as joined once the Groups API sees them`, placeholder "name@your-domain" — always.
- `InviteControl` button "Invite", busy "Inviting", disabledReason "Enter their email first"; `ButtonLink` "Open the group in Google Admin" — always.
- `TeamRetry`: `StepFailure` cause `swarm-workspace-apply reported {ws} for {workspace}`, fix "the decision says why it was denied; Retry asks for the team's workspace again." (denied) / "…Retry waits for the next refresh." (id not read yet) / "…Retry runs the workspace apply again." — appears when workspace state is `failed` or `denied`.
- Personal (one person), not filed: title "First workspace", state `in_progress`, line `One-person install: your personal workspace becomes the first workspace, {PUBLISHING_WAIT}` or "…; swarm-api files it on this page's next read{ (last read: {reason})}", body `Facts` "First team and invites" → `notNeeded` text — appears when `isOnePerson(state) && (personal===null || personal.workspace===null)`.
- Personal, filed: title "First workspace", state failed/smoke-held/done, line `{ONE_PERSON_LINE} {workspace}` (ready) or `One-person install: your personal workspace {workspace} is {ws ?? 'being set up'}` — appears when personal workspace exists; body facts: "Personal workspace" `{workspace} · {owner}`, namespace fact (as team), "First team and invites" skipped fact, "Repository grant" (as above, "your personal workspace"), "Smoke agent" (below) + `SmokeRetry`.

### Claude accounts row (`accountsRow`, `AccountLinkForm`)
- Before first team: state `todo`, line "Comes after the first team: each pool account is owned by it, so its owner must be a member of the first team's group" — appears when `teams.length===0 && !isOnePerson`.
- No accounts yet: line `Each subscription's owner signs in once, through a one-time link this page makes, into {your personal workspace's|the first team's} pool` — appears when `accounts.length===0`.
- Accounts counted: state done/in_progress/todo, line `Pool: {signed} of {n} signed in` — appears when accounts exist.
- `accountFact` signed in: `{owner} · signed in {ago} · in {pool}`, state `done` — per account once `accountSignedIn`.
- `accountFact` link expired: `{owner}'s one-time link expired; send a new one`, state `failed` — per account once its link's `link_expires` has passed.
- `accountFact` waiting: `waiting for {owner} to open their one-time link and sign in{, expires in …}`, state `in_progress` — per account otherwise.
- `AccountLinkForm` field "Account label", placeholder "lowercase, e.g. ravi" — always in the row's body.
- `AccountLinkForm` field "Its owner", hint "You: a one-person install's pool takes your own subscription" (one person) / "You, or a member of the first team's group" (team), plus "; your sign-in opens in a new tab" (self) / "; the link opens for them only, once, within 24 h" (other) — always.
- `AccountLinkForm` waiting hint, e.g. `Waiting for the workspace {workspace} (swarm-workspace-apply: {ws}): a sign-in link is made once it is ready` — appears when the first workspace is not yet `ready`.
- `AccountLinkForm` button "Sign in my Claude account" (self) / "Make a sign-in link" (other), busy "Opening"/"Making", disabledReason "Workspace not ready" / "Name the account and its owner first" — always.
- `AccountLinkForm` status text `Your sign-in for {label} opened in a new tab: sign in to Claude there, and this page sees the account at its next read.` — appears right after the installer signs themself in.
- `AccountLinkForm` `CodeBlock` title `One-time link for {owner}, shown once: send it to them`, Copy/Copied button — appears right after a link is minted for someone else.

### Smoke agent (`smokeFact`, `SmokeRetry` — inside the team/personal row)
- Fact "Smoke agent", waiting for first account: "waits for a pool account (the next step): it needs one to run", state `todo` — appears when `smoke===null && !signedIn`.
- Fact "Smoke agent", about to start: "starts now that a pool account is signed in", state `todo` — appears when `smoke===null && signedIn`.
- Fact "Smoke agent", running/succeeded/failed: `{smoke.state}{ · {smoke.detail}}`, state in_progress/done/failed — appears once a smoke record exists.
- `SmokeRetry`: `StepFailure` cause `The smoke task failed{: {detail}}`, fix "its task's page says why; once that is fixed, Retry starts the smoke again. The hand-over waits for it to succeed.", button "Retry the smoke" — appears when `smoke.state=='failed'`.

### Add-ons row (`addonsRow`)
- Row: title "Add-ons", state `todo`, by "optional", line "Off by default; core runs without them. Enabling one is a terminal step: it needs a Terraform switch" — always present.
- Body fact per add-on: name = `addon.name`, text = `<code>{addon.enable}</code>`, state `todo` — appears when `state.addons.length>0`.

### Verify row (`verifyRow`, `ConsoleChecks`)
- Not started, waiting on console: state `todo`, line `Runs once every console step above is done; swarm install checks {VERIFY_CHECKS}` — appears when `v===null && !consoleDone(state) && finished===null`.
- Not started, ready to hand over: line `Every console step is done: hand over, and swarm install checks {VERIFY_CHECKS}` — appears when `v===null && consoleDone(state) && finished===null`.
- Not started, handed over, CLI present: line `The console steps are finished; swarm install starts verify on its next read: {VERIFY_CHECKS}` — appears when `v===null && finished!==null && cli_running`.
- Not started, handed over, no CLI: line `The console steps are finished; run swarm install in a terminal to verify: {VERIFY_CHECKS}` — appears when `v===null && finished!==null && !cli_running`.
- Passed: state `done`, line `Verify passed: {done} of {total} checks{, in {elapsed}}` — appears when `v.result=='passed'`.
- Failed: state `failed`, line `Verify failed at {progress}`, body `StepFailure` cause `The {check} check failed`, fix "swarm install printed which part failed and why; fix it, then run swarm install again: it re-runs verify from the failed check." — appears when `v.result=='failed'`.
- Running: state `in_progress`, `next=true`, line = bold `{check} · {done} of {total}` plus ` · {elapsed}`, ` · {about N left / N past expected end}`, ` · {deadline in N / N past deadline}` — appears while verify runs with no result yet.
- `ConsoleChecks`, no runs posted: fact "Console task and workflow", text `swarm-api starts them on this page's next read, as {whose}{, once it has its repository grant}` (handed over) or `start by themselves when you hand over, as {whose} on a repository of its grant` (not yet) — appears when `verify_runs` has no task/workflow id.
- `ConsoleChecks`, runs posted: fact "Console task" / "Console workflow", linked to the run, text `{state word, lowercased}{ · on {repository}}` or "reading its state" or "its state could not be read: {error}" — appears once ids exist.
- `ConsoleChecks`, retries left: text `{it starts again when you hand over | swarm-api starts it again on this page's next read}: {left} of 2 fresh starts left` — appears when a run failed and has fresh starts remaining.
- `ConsoleChecks`, retries exhausted: button "Run the console checks again", disabledReason `It failed after 2 fresh starts, so none is started again: read that run for why`, text `{left} of 2 fresh starts left` — appears when a failed run has spent both `VERIFY_RUN_RETRIES`.

### Send to your team row (`sendRow`)
- Locked: title "Send to your team", state `todo`, line "Send to your team is unlocked when verify passes" — appears when `verify.result!=='passed'`.
- Unlocked: state `done`, line "The plugin line and the console URL, below" — appears when `verify.result=='passed'`.

### Hand over (`VerifyFinish`, inside the verify row's action slot)
- Button "Hand over to swarm install", busy "Handing over" — `VerifyFinish` (AdminSetupSteps.tsx) — appears when `state.finished===null && consoleDone(state)` (every console step done, not yet handed over); calls `finishSetup`.

## Banners
- "Admin › Setup is for platform admins" (tone warn) / "The install record could not be read" (tone bad), action button "Read again" — `SetupRead` (AdminSetup.tsx) — appears when `result.status=='error'`, split on `error.kind=='admin_required'`.
- "No install record yet" (tone info), body "swarm install writes it when it starts. Run swarm install in a terminal to begin." — `SetupRead` (AdminSetup.tsx) — appears when `result.status=='empty'`.
- "swarm install is not running" (tone warn), body "Run swarm install in a terminal to continue: the App's switches and verify need it. The steps on this page stay usable meanwhile." — `AdminSetupSteps` (AdminSetupSteps.tsx) — appears when `!state.cli_running`.
- "This page needs the code GitHub sends back" (tone warn), body "It opens after GitHub creates the App from Admin › Setup. Create the GitHub App again from there." — `GitHubAppCallback` (AdminSetupSteps.tsx) — appears when the callback URL has no `code`/`state`.
- "GitHub's code could not be exchanged" (tone bad), actions "Back to Admin › Setup", body `{message}. The code works once and expires within an hour; create the GitHub App again from Admin › Setup.` — `GitHubAppCallback` (AdminSetupSteps.tsx) — appears when `convertGitHubApp` answers `error`/`empty`.
- "This is not a whole setup link." (tone bad), body "Only part of it reached this page: open the link from the admin's message again, or ask them for a new one." — `SetupLinkScreen` (SetupLink.tsx) — appears when `token=********
- Refusal banner, title = `linkRefusal(error).line` (tone bad) — `OpenLink` (SetupLink.tsx) — appears on a terminal redeem refusal (404/422-unkeyed/403 `forbidden`), standalone with no retry button.
- Same refusal banner shown inline above the "Open the link" button (tone bad) — `OpenLink` (SetupLink.tsx) — appears when the refusal is retryable (`linkRefusal(...).retry===true`, e.g. a 5xx).
- "Your workspace is still being set up." (tone info), body `The link is unused, and this page opens it by itself as soon as the workspace is ready ({message}).` — `OpenLink` (SetupLink.tsx) — appears on a 409 from the redeem call; retried every `REDEEM_RETRY_MS`.
- Refusal banner inside `ValueForm`, title = `linkRefusal(error).line` (tone bad) — `ValueForm` (SetupLink.tsx) — appears when storing a `team-git`/`addon-secret` value is refused.
- `Stored{ in {secret}}.` (tone info), body "This link is now used. Admin › Setup sees the value at its next read; you can close this page." — `Written` (SetupLink.tsx) — appears once a `team-git`/`addon-secret` link's value is stored.
- `{label} is in the {tenant} workspace's Claude pool.` (tone info), body "Admin › Setup sees it at its next read; you can close this page." — `PoolSignIn` (SetupLink.tsx) — appears when `at.kind=='done'`.
- `The sign-in did not start: {error.message}` (tone bad), body "Your link is already used, and this page keeps what it opened: start the sign-in again." — `PoolSignIn` (SetupLink.tsx) — appears when `at.kind=='start_failed'`.
- `The sign-in did not finish: {failed.message}` (tone bad), actions "Start over", body "Paste the code again, or start over for a fresh sign-in page." — `PoolSignIn` (SetupLink.tsx) — appears when `at.kind=='open' && at.failed!==null`.

## Side cards
### Send to your team (`SendToTeam` — AdminSetupSteps.tsx)
- Card title "Send to your team", action `Chip` "verify passed" or faint `Chip` "locked" — appears always; chip reflects `verify.result=='passed'`.
- Locked body: "Send to your team is unlocked when verify passes: the line below works only on a deployment verify has proven." — appears when `!passed`.
- No hostname body: "The deployment's hostname is not set on swarm-api (INSTALL_HOSTNAME), so there is no URL to send yet." — appears when `passed && hostname===null`.
- Unlocked body: lead "One line for a teammate's terminal; the plugin asks for nothing else. It reads the rest from https://{hostname}/.well-known/swarmcloud.", `CodeBlock` title "Plugin install" with the `pluginLine(...)` text and Copy/Copied button, facts "Console" → `https://{hostname}`, "Then" → "sc login, then /sc:submit in Claude Code", "Day two" → "Admin › Setup › Health" — appears when `passed && hostname!==null`.

### Capacity (`CapacityCard` — AdminSetupSteps.tsx)
- Card title "Capacity", foot "Chosen in swarm install's first stage; read-only here." — always.
- Body "The sizing is not recorded yet: swarm install asks for it, one person or a team, in its first stage." — appears when `sizing===null`.
- Fact "For": "One person" / "A team" / `A team of {n} people` — appears when `sizing!==null`.
- Fact "Ceiling": `{n} agents at once across the deployment` — appears when `pool_limits.global!==null`.
- Fact "Per workspace": `{n} agents at once per workspace` — appears when `pool_limits.default_tenant!==null`.
- Fact "Accounts to add": `{n} Claude subscription account(s)` — appears when `sizing.pool_accounts!==null`.
- Fact "Budget": `${budget_usd} a month` — appears when `sizing.budget_usd!==null`.
- Fact "Service ceilings": `{name} {n} instance(s) · …` — appears when `service_max_instances` has entries.

### The installer (`InstallerCard` — AdminSetupSteps.tsx)
- Card title "The installer", foot `Install record: Firestore swarm-install/{env}{ · {release}}` — always.
- Fact "swarm install": `finished, verify passed, {ago}` (done) / `waiting on this page · seen {beat ?? 'just now'}` (in_progress) / `not running{ · last seen {beat}}` (failed) — split on `engine.ready_at` and `state.cli_running`.
- Fact "Release": `{engine.release}` or "not recorded yet" — split on whether the engine document exists.

### Health (inline `Card`, not a component — AdminSetup.tsx `AdminSetupPage`)
- Card title "Health", foot "The hourly doctor's checks, read-only, on the live deployment; swarm doctor checks this machine and the deployment from a terminal." — always, laid out beside `InstallerCard`.
- Body: "Once setup is done, the scheduled doctor's findings and each one's repair are on " + link "Admin › Setup › Health" (`/admin/setup/health`) — always.

## GitHub App callback page (`GitHubAppCallback`, routed by `AdminSetupCallbackScreen` — AdminSetup.tsx / AdminSetupSteps.tsx)
- PageHead title "GitHub App" — `AdminSetupCallbackScreen` — always, at `/admin/setup/github-app/callback`.
- No code/state in the URL: Card "GitHub App", Banner "This page needs the code GitHub sends back" (see Banners), button "Create the GitHub App again" → `/admin/setup` — appears when `params.code===null || params.state===null`.
- Exchanging: Card "GitHub App", text "Exchanging GitHub's one-time code and storing the App's secrets in Secret Manager…" — appears while `convertGitHubApp` is loading (`result===null || result.status=='loading'`).
- Exchange failed: Card "GitHub App", Banner "GitHub's code could not be exchanged" (see Banners) — appears when `result.status=='error'|'empty'`.
- Success: Card "GitHub App created", fact "Created from the manifest" → `{slug} (app id {id})`, fact "Secrets stored" → "the private key, the client secret and the webhook secret went from GitHub's answer straight into Secret Manager; none is shown here or anywhere", fact "Next" → "install the App on your organisation: Admin › Setup has the install button, watches for the installation, and GitHub sends you back there when it is done", button "Continue in Admin › Setup" → `/admin/setup` — appears when the exchange succeeds; the code is tracked in the module-level `POSTED` set so it is never re-posted by the same tab.

## One-time link page (`SetupLinkScreen` and its children — SetupLink.tsx)
- PageHead title "Your setup link" — `SetupLinkScreen` — always, at `/admin/setup/link/<token>`.
- Card "Open your link", lead "This page opens a one-time link an admin made for you in Admin › Setup. The link is read once, when you open it, and is not kept in the address: open the link from the admin's message again." — `SetupLinkScreen` — appears when `token=******** and no pending pool link in `sessionStorage`.
- Banner "This is not a whole setup link." — `SetupLinkScreen` — appears when `token=******** (see Banners).
- `OpenLink`, idle/retryable: Card "Open your setup link", lead "An admin made this link for you in Admin › Setup. It works once: opening it signs your Claude subscription into the team's pool, or asks for the one value it was made for.", button "Open the link" busy "Opening…"/"Waiting for the workspace…" — appears when `token` is a non-empty string and no terminal refusal yet; auto-opens itself once if `window.name==OWN_SIGN_IN_WINDOW`.
- `ValueForm`, team-git: Card "Store the team's GitHub token", lead "The value goes straight from this page into its one secret in Secret Manager. It is never shown again, and the link is used only once it is stored.", field "The team's GitHub token" (password), button "Store it" busy "Storing…" — appears when the redeem answers a 422 naming `team-git`.
- `ValueForm`, addon-secret: ******** "Store the add-on's secret", field "The add-on's secret" (password) — appears when the redeem answers a 422 naming `addon-secret`; same lead and button as above.
- `Written`: Banner `Stored{ in {secret}}.` — appears once a team-git/addon-secret value is stored (see Banners).
- `PoolSignIn`, idle/starting/start_failed: Card "Sign in your Claude account", facts "account" → `{label}`, "workspace" → `{tenant}`, lead `Your link is open. Sign in with the Claude subscription the admin asked you for; the account lands in the {tenant} workspace's pool under the name {label}.`, status text "Your workspace is finishing its setup; the sign-in starts again by itself in a few seconds." (retrying on a 403), button "Start the sign-in" / "Start the sign-in again" busy "Starting…" — appears once the redeem answers `pool-account`; retries `beginAccountSignIn` up to `SIGN_IN_RETRIES` times, every `SIGN_IN_RETRY_MS`, on a 403.
- `PoolSignIn`, open: Card "Sign in your Claude account", step text "1. Open Claude's sign-in page and sign in with the subscription the admin asked you for." with link "Open the Claude sign-in page" (`auth.authorize_url`, new tab), step text "2. Claude then shows a code. Paste it here.", field "The code Claude shows", pasted-code hint (`pastedCodeHint`), button "Finish the sign-in" busy "Signing in…" — appears once `beginAccountSignIn` succeeds.
- `PoolSignIn`, done: Card "Your Claude account is signed in", facts "account"/"workspace", banner `{label} is in the {tenant} workspace's Claude pool.` — appears once `finishAccountSignIn` succeeds; clears the `PENDING_KEY` sessionStorage entry.

## Add a team later (`AdminSetupTeam` — AdminSetupTeam.tsx)
- Card "Add a team", foot "The same call swarm team add makes: the team's record, then swarm-workspace-apply makes its namespace, quota, verify-keys and worker identity." — appears whenever `state.teams.length>0` (rendered below the main checklist by `AdminSetupPage`).
- Field "Workspace group", hint "A Google Workspace group; its members are the team", placeholder "research@your-domain" — always in the form.
- Field "Workspace name", hint "Shown in the console; the group's name when left empty", placeholder "research" — always.
- Field "First workspace admin", hint "Administers the team's workspace", placeholder "name@your-domain" — always.
- Field "Invite", hint "Each is added to the group on its members page in Google Admin (swarm-api adds no one); they count as joined once the Groups API sees them", placeholder "one or more emails" — always.
- Button "Add the team", busy "Adding", disabledReason "Name the Workspace group first" — appears when `group.trim().length<3`.
- Error block: `{error.message}` plus "Only a platform admin can add a team." — appears when `createSetupTeam` answers `error` (the extra line only for `kind=='admin_required'`).
- Status text `{group} was asked for: its progress shows under Teams as swarm-workspace-apply reports it.` — appears right after a successful submit (`asked!==null && error===null`).
- Card "From a terminal", lead "The same steps, from the machine swarm install runs on. It follows the form as you type.", `CodeBlock` title "Same steps" showing the live `teamAddLine(form)` text, Copy/Copied button — always, mirrors the form live.
- Card "Teams", `Facts` list of every existing team via `teamFact`: name = `team.workspace`, text joins `{group}`, `admin {first_admin}`, `workspace {ws}` or "workspace apply not reported yet", `smoke {state}{, detail}` or "smoke agent not started"; state `failed` if workspace `failed`/`denied` or smoke `failed`, `done` if workspace `ready` and smoke `succeeded`, else `in_progress` — always, one row per team in `state.teams`.
