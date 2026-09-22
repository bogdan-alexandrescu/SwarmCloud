# Prose migration table — every prose block in `apps/swarm-ui/src`, classified

**What this is.** The execution list for §B7 of
[`ui-audit-and-build-prompt.md`](ui-audit-and-build-prompt.md) — the owner
directive to take the prose out of the app, put help in a dedicated Help
section, and leave a `?` affordance where an explanation used to be.

**What this is not.** No component is changed by this document. It is analysis
for a later wave. A parallel lane owns `<HelpCard>`, `help.ts`, the Help
section and `AgentDetail.tsx` as the exemplar; `AgentDetail.tsx` rows below are
recorded for topic-id reuse and cross-reference, **not** for the executing wave
to touch.

**The rule that makes this safe** (§B7.1, restated because it is the only thing
here that can be got dangerously wrong):

> The **FACT** stays on the surface, always, and stays impossible to miss. The
> **EXPLANATION** moves into the `?` card.
>
> Acceptance test for any panel after the change: can a reader tell, **without
> hovering anything**, that a number is missing rather than zero? A silent icon
> where a sentence used to be does not satisfy this directive.

---

## 1. The counts — how much prose is there really

Measured at `22d806a`, over `apps/swarm-ui/src/*.tsx` plus the rendered string
constants in `types.ts` and `fetch.ts`.

### The universe

| What | Count | How it was counted |
|---|---:|---|
| `<p>` elements | **236** | `grep -o '<p[ >]' src/*.tsx \| wc -l` |
| `className="muted…"` occurrences | **108** | `grep -o 'muted' src/*.tsx \| wc -l` |
| — of which sit **on a `<p>`** (already inside the 236) | 84 | `grep -o '<p className="muted[^"]*"'` |
| — of which are a `<span>` / `<div>` / `<dd>` / `<i>` (**additional**) | 24 | |
| `<section className="section legend">` blocks | **6** | Accounts, AgentDetail, Capacity, Profiles, QuotaDetail, Runtimes |
| — `<dt>`/`<dd>` prose entries inside them | 29 | 8 + 7 + 3 + 2 + 4 + 5 |
| Rendered explanatory string constants outside JSX | 26 | `types.ts` ×10, `fetch.ts` ×16 (13 of those are 2–7 word headings) |
| **The three greps together** | **315** | 236 + 24 + 29 + 26 |
| **Distinct source locations actually classified below** | **363** | see the note under the classification table |
| `?` affordances today | **0** | |
| Help screen today | **none** | |
| `title=` native tooltips (hover-only already) | **94** | see §7 — these fail the acceptance test today |

### The classification

Counted out of the table below itself, by
`grep -c '| <CLASS> |' docs/web-ui/prose-migration-table.md`, so these numbers
and the rows cannot disagree.

| Class | Rows | Share |
|---|---:|---:|
| `FACT-STAYS` | 191 | 45% |
| `EXPLANATION-MOVES` | 163 | 39% |
| `KEEP-AS-IS` | 66 | 16% |
| `DELETE` | 3 | 1% |
| **Total rows** | **423** | |

**423 rows over 363 distinct source locations.** 59 locations carry more than
one row; **56 of those are splits** — one `FACT-STAYS` row and one
`EXPLANATION-MOVES` row at the same `file:line` — and the other 3 carry two
facts or a fact and a value. Splitting is the point of the exercise: each of the
56 is a place where deleting the paragraph would take an honesty marker out
along with its justification.

363 is larger than the 315 measured above because the table also classifies
prose-bearing elements that neither a `<p>` count nor a `muted` count catches:
empty-state `<h3>` headings, `.acct-why` and `.provenance` captions, the
`dsp-*` consequence spans, and four `title=`/constant references called out by
line. Every row is a real location; the 315 is what the two greps see.

**Only 3 `DELETE` rows.** That is the most surprising number here and it is not
a mistake: this UI's prose is almost never decorative. It was written against a
specific failure — a failed read rendered as an empty one — and nearly all of it
is either a fact or a justification for a fact. The clean-up is therefore
*relocation*, not deletion, and anyone hoping the 236 paragraphs are mostly
padding should stop here.

### Volume by file — locations before, and locations still visible after

"After" counts a location as surviving if any of its rows is `FACT-STAYS` or
`KEEP-AS-IS`.

| File | `<p>` | extra muted | legend entries | rows | locations | after |
|---|---:|---:|---:|---:|---:|---:|
| `Accounts.tsx` | 75 | — | 8 | 125 | 103 | 65 |
| `AgentDetail.tsx` *(exemplar lane)* | 37 | 11 | 7 | 77 | 70 | 49 |
| `Dispatch.tsx` | 13 | 5 | — | 23 | 23 | 18 |
| `Overview.tsx` | 16 | — | — | 27 | 22 | 20 |
| `Runtimes.tsx` | 7 | 4 | 5 | 19 | 18 | 7 |
| `Submit.tsx` | 12 | — | — | 16 | 13 | 11 |
| `SubmitWorkflow.tsx` | 7 | 3 | — | 14 | 12 | 11 |
| `Shell.tsx` | 10 | — | — | 14 | 11 | 11 |
| `PlatformCounts.tsx` | 10 | — | — | 13 | 10 | 8 |
| `types.ts` (rendered strings) | — | — | — | 10 | 10 | 4 |
| `Profiles.tsx` | 5 | — | 2 | 9 | 9 | 5 |
| `Holders.tsx` | 6 | — | — | 9 | 6 | 6 |
| `Workflows.tsx` | 7 | 1 | — | 10 | 8 | 8 |
| `App.tsx` | 6 | — | — | 8 | 8 | 5 |
| `AttemptTimeline.tsx` | 6 | — | — | 7 | 7 | 5 |
| `Activity.tsx` | 5 | — | — | 7 | 6 | 4 |
| `Capacity.tsx` | 3 | — | 3 | 7 | 6 | 1 |
| `QuotaDetail.tsx` | 0 | — | 4 | 7 | 4 | 3 |
| `Blockers.tsx` | 4 | — | — | 6 | 4 | 3 |
| `AdminSettings.tsx` | 3 | — | — | 5 | 4 | 2 |
| `fetch.ts` (rendered strings) | — | — | — | 4 | 4 | 4 |
| `Agents.tsx` | 2 | — | — | 4 | 3 | 3 |
| `ErrorBoundary.tsx` | 2 | — | — | 2 | 2 | 2 |
| `DataSources.tsx`, `Liveness.tsx`, `main.tsx` | 0 | 0 | 0 | 0 | 0 | 0 |
| **Total** | **236** | **24** | **29** | **423** | **363** | **255** |

---

## 2. How to read a row

| Column | Meaning |
|---|---|
| **Line** | 1-indexed line of the element's opening tag at `22d806a` |
| **Text** | the rendered text, JSX expressions shown as `{…}`, truncated to ~90 chars |
| **Class** | exactly one of the four |
| **Topic / marker** | for `EXPLANATION-MOVES`, the stable topic id; for `FACT-STAYS`, the visible marker that must survive |

Markers that count as "visible without hovering", in the order of preference
§B7.1 sets out: **an em dash**, **a hatched or dotted track**, **a count of
what is missing**, **a struck total**, **a short bold clause on the surface**.
A `?` glyph alone is never a marker.

`ACTION` in the Topic column marks **actionable explanation** — see §6. It is
not a fifth class; it is a constraint on where the explanation may be put.

`⚠︎contract` marks a prose block containing a hardcoded platform or frozen-contract
value — see §5.

---

## 3. Per-file tables, biggest win first

### 3.1 `apps/swarm-ui/src/Accounts.tsx` — 125 rows over 103 locations

The credential pool, and by volume the single biggest win: **84 `<p>` and muted
blocks plus an 8-entry legend plus a four-part inlined help essay** at
`3125–3285`. It is also the file where a careless migration does the most
damage, because roughly a third of its prose is the outcome of a write whose
result is genuinely ambiguous, and half of that is actionable.

#### The pool table and an account's row

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 378 | "The read succeeded and returned nothing — this is a real zero, not a failed query." | FACT-STAYS | keep sentence 1 verbatim as the empty-state line |
| 378 | "Until an account exists, every task whose runner profile names a subscription provider parks…" | EXPLANATION-MOVES | `park-on-missing-credential` |
| 433 | "{…} rows returned · 5H and 7D are the provider's own windows, reported by workers · a figure…" | FACT-STAYS | keep "{n} rows · ~ projected" |
| 433 | "…a figure marked ~ is projected, not measured" | EXPLANATION-MOVES | `projected-not-measured` |
| 572 | five-cell utilisation bar cells | KEEP-AS-IS | — |
| 684 | "window reset, awaiting a new reading" | FACT-STAYS | `.acct-why` caption, already a marker |
| 804 | "lent to you; not yours to change" | FACT-STAYS | `.acct-why` caption |
| 811 | "advisory; the lease is the authoritative record of who holds what" | FACT-STAYS | shorten to "advisory" |
| 811 | "…the lease is the authoritative record of who holds what" | EXPLANATION-MOVES | `advisory-vs-lease` |
| 820 | "no worker has reported a rate-limit reading for this account" | FACT-STAYS | em dash in the cell + this caption |
| 828 | "past the 30 minutes a reading is trusted for" | FACT-STAYS | ~ mark + caption ⚠︎contract |
| 865 | "{…} owns this account and has lent it to you… Those routes answer 404, no such account…" | EXPLANATION-MOVES | `lent-account` (fact survives at 804) |
| 890 | "The provider has reported no windows for this account, so 5H, 7D and CLEARS are all unmeasured…" | FACT-STAYS | shorten to "no windows reported — 5H/7D/CLEARS unmeasured, not zero" |
| 900 | "The provider defines its own window names and may add one at any time. These arrived in the…" | EXPLANATION-MOVES | `provider-defines-windows` |

#### Refresh-now

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 1209 | "Runs the same code path the sweep runs, for this account, and reports what happened…" | EXPLANATION-MOVES | `refresh-now-probe` |
| 1225 | "This failed on a write… the credential may have been exchanged anyway… reload this screen before…" | FACT-STAYS | ACTION — ambiguous write, required action |
| 1247 | `{verdict.body}` | KEEP-AS-IS | value |
| 1248 | "refreshed={…} reason={…} …the access token it reports is ALREADY EXPIRED, so pods will fail on it" | FACT-STAYS | struck/red expiry clause stays inline |

#### Lending and state

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 1328 | "This account serves {…} only. No other tenant's pod can reach its credential." | EXPLANATION-MOVES | `lending` |
| 1362 | "{…} owns this account, so it is not sent as a loan: an account cannot be lent to itself…" | EXPLANATION-MOVES | `lending` |
| 1368 | "There is no tenant picker here because the tenant list could not be read: {…}" | FACT-STAYS | keep clause 1; the control is absent and must say why |
| 1368 | "That endpoint is admin-only, so a non-admin genuinely cannot list tenants. Nothing is broken…" | EXPLANATION-MOVES | `admin-gate-not-failure` |
| 1383 | "{…} — {…} The lending list was not changed." | FACT-STAYS | "not changed" clause |
| 1444 | "The reason is stored with the change and shown in the STATE column, because the next person…" | EXPLANATION-MOVES | `state-change-reason` |
| 1472 | "Moved to {…}." | KEEP-AS-IS | — |
| 1474 | "{…} — {…} The state was not changed." | FACT-STAYS | "not changed" clause |

#### The sign-in, and its eleven outcomes

This is the densest concentration of ambiguity in the product: eleven distinct
outcome panels at `1734–2059`, every one of which distinguishes *what the
platform did* from *what this page can prove it did*. Every `FACT-STAYS` row
here is load-bearing.

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 1734 | "Do not sign in again until you have looked… What is missing from the answer is the account's name…" | FACT-STAYS | ACTION — keep "the account exists; its name is missing from the answer" |
| 1734 | "201 is the answer it gives once the account has been written, and it deletes the pending record…" | EXPLANATION-MOVES | `signin-201-no-name` |
| 1741 | "Reload the pool and look for the label you typed. If it is there, this worked…" | EXPLANATION-MOVES | ACTION `signin-verify-by-reload` — must stay on the surface, see §6 |
| 1765 | reload the pool / start over | KEEP-AS-IS | controls |
| 1780 | "Nothing refused this — nothing answered it… Nothing here measures which, so nothing here will…" | FACT-STAYS | the whole point of the panel |
| 1787 | "Reload the pool and look first, because that is measurable and this is not…" | EXPLANATION-MOVES | ACTION `signin-verify-by-reload` |
| 1796 | reload the pool | KEEP-AS-IS | control |
| 1808 | "This sign-in was held past its deadline… nothing was created and nothing was changed." | FACT-STAYS | keep the bold clause |
| 1808 | "The platform deletes a pending record once it is too old, and it does that before it tries…" | EXPLANATION-MOVES | `signin-expired` |
| 1820 | "start over above asks the platform for a new sign-in; the button below does the same thing." | DELETE | restates two controls already on screen |
| 1827 | start over | KEEP-AS-IS | control |
| 1839 | "This sign-in is over, and it may well have worked… this page cannot tell you from here whether…" | FACT-STAYS | outcome-unknown clause |
| 1839 | "The platform is not holding a record for it any more, and the ordinary reason a record is missing…" | EXPLANATION-MOVES | `signin-record-gone` |
| 1850 | "Reload the pool and look for the label before you start another sign-in…" | EXPLANATION-MOVES | ACTION `signin-verify-by-reload` |
| 1872 | reload the pool / start over | KEEP-AS-IS | controls |
| 1886 | "That code belongs to a different sign-in… this sign-in is untouched and still open" | FACT-STAYS | keep bold clause |
| 1886 | "The platform compares the part after the # with the sign-in this page started… a second tab…" | EXPLANATION-MOVES | `signin-state-mismatch` |
| 1902 | "The code was refused, and this sign-in is still open." | FACT-STAYS | keep |
| 1902 | "The platform deletes the sign-in only once an account exists, so a code that was mistyped…" | EXPLANATION-MOVES | `signin-code-refused` |
| 1915 | "…it does not know whether the record is still there, and it does not know whether an account…" | FACT-STAYS | unrecognised-refusal clause |
| 1926 | "Reload the pool and look first, because that is measurable and this is not…" | EXPLANATION-MOVES | ACTION `signin-verify-by-reload` |
| 1933 | reload the pool | KEEP-AS-IS | control |
| 1978 | `<div class="ctl-empty">` wrapper | KEEP-AS-IS | structure |
| 1980 | "The API answered HTTP {…} for {…} itself. That is what an API which has not been given the…" | FACT-STAYS | keep "HTTP {n} for the sign-in route itself" |
| 1980 | "…Both steps exist in this repository — quota-broker serves them and swarm-api proxies them…" | EXPLANATION-MOVES | `signin-route-missing` |
| 1989 | "Nothing was created and nothing was changed. The pool above loaded, so your sign-in…" | FACT-STAYS | keep sentence 1 |
| 1994 | `{error.code} · {…}` | KEEP-AS-IS | value |
| 2010 | `{error.message}` | KEEP-AS-IS | value |
| 2011 | `{…} · HTTP {…} · {…}` | KEEP-AS-IS | value |
| 2039 | "The platform did not say how long it holds this sign-in, so there is no countdown here rather…" | FACT-STAYS | keep "no deadline was given" where a countdown would be |
| 2039 | "…the code is single-use and expires quickly" | EXPLANATION-MOVES | `signin-code-single-use` |
| 2049 | "The platform said it would hold this sign-in for {…}, and that has passed on this browser's clock." | FACT-STAYS | keep |
| 2049 | "The field is still live because the platform owns the clock and this one may be ahead of it…" | EXPLANATION-MOVES | `signin-clock-skew` |
| 2059 | "This platform holds the sign-in for another {…}. The code itself is single-use and expires…" | EXPLANATION-MOVES | `signin-code-single-use` |
| 2091 | "Which organisation you sign in as comes from the cookies this browser already holds… A private…" | EXPLANATION-MOVES | ACTION `second-browser-application` |
| 2186 | "This browser would not give the page access to the clipboard, which it refuses outside a secure…" | FACT-STAYS | keep "clipboard refused — copy the link by hand" |
| 2186 | "…it is the same value, and nothing is wrong with the sign-in" | EXPLANATION-MOVES | `clipboard-secure-context` |
| 2193 | the sign-in URL | KEEP-AS-IS | value |
| 2279 | "2 · Sign in to Claude" | KEEP-AS-IS | step heading |
| 2281 | "This opens Claude's own sign-in page in a new tab… this platform never sees your password…" | EXPLANATION-MOVES | `signin-is-anthropics-page` |
| 2324 | "This browser refused to open the tab, which is what a popup blocker or an extension does…" | FACT-STAYS | keep "popup blocked — the sign-in is still open; use the link" |
| 2336 | "The sign-in link has been withdrawn. It carries the same state that the reopen button above…" | FACT-STAYS | keep sentence 1 |
| 2348 | "3 · Paste the code that page shows you" | KEEP-AS-IS | step heading |
| 2349 | "When you have signed in, Claude prints a short code… Paste all of it — the platform splits it…" | EXPLANATION-MOVES | ACTION `signin-paste-the-code` |
| 2381 | `{hint}` | KEEP-AS-IS | value |
| 2382 | submit button | KEEP-AS-IS | control |

#### The add / re-auth form

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 2498 | `<div class="ctl-empty">` wrapper | KEEP-AS-IS | structure |
| 2499 | "Adding an account is unavailable until your tenant is named" | FACT-STAYS | heading is the marker |
| 2500 | "An account is owned by exactly one tenant, and the API decides which from your verified sign-in…" | EXPLANATION-MOVES | `account-owned-by-one-tenant` |
| 2506 | "This is a gap in what was read, not a fault in the platform. It says nothing about the accounts…" | FACT-STAYS | keep |
| 2568 | "Owned by {…}. This is a sign-in, not a paste… What it receives is stored write-only…" | EXPLANATION-MOVES | `sign-in-not-paste` + `credential-split` |
| 2593 | "This sign-in is reserved for {…}… The platform is holding those alongside the sign-in, so they…" | EXPLANATION-MOVES | `signin-holds-label-and-lending` |
| 2643 | "{…} fixed, and the only kind this pool handles: a Claude subscription… There is deliberately no…" | EXPLANATION-MOVES | `subscription-only-no-api-key` |
| 2645 | "fixed, and the only kind this pool handles: a Claude subscription…" (span) | DELETE | duplicate of 2643 in the same panel |
| 2667 | "Lowercase letters, digits and dashes… at most 40 characters" | FACT-STAYS | field constraint stays beside the input |
| 2667 | "…it becomes part of a Secret Manager name and a Kubernetes annotation. The platform checks it…" | EXPLANATION-MOVES | `account-label-rules` |
| 2682 | "{…} already exists in this pool. Signing in under that label REPLACES its credential…" | FACT-STAYS | ACTION — destructive-outcome warning |
| 2704 | "Isolation is the default. Naming a tenant here lets that tenant's pods mount this account's…" | EXPLANATION-MOVES | `lending-narrows-isolation` |
| 2711 | Start the sign-in | KEEP-AS-IS | control |
| 2732 | "There is no Try again on this failure because the label field above is empty…" | FACT-STAYS | keep "no Try again — the label field is empty" where the button would be |
| 2732 | "…the platform checks the label before it hands back a sign-in link" | EXPLANATION-MOVES | `account-label-rules` |

#### The outcome panels

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 2771 | "Two secrets were written: the pair, which only quota-broker reads, and the access token…" | EXPLANATION-MOVES | `credential-split` |
| 2771 | "That access token expires in {…}, and that is not a deadline for you" / "The platform did not…" | FACT-STAYS | expiry, or its absence |
| 2794 | "…its state was put back to AVAILABLE as a second, separate call, and that call succeeded." | FACT-STAYS | keep |
| 2794 | "…the same rule that stops a credential replacement silently un-pausing an account somebody paused" | EXPLANATION-MOVES | `reauth-does-not-unpause` |
| 2802 | "This account is STILL in REAUTH_REQUIRED, so nothing will be assigned to it… Use move to…" | FACT-STAYS | ACTION — required follow-up after a partial success |
| 2824 | "It appears in the pool with no reading yet — that is unmeasured, not idle." | FACT-STAYS | em dash + clause |
| 2830 | "Its readings were kept… Signing in replaces the credential and nothing else…" | EXPLANATION-MOVES | `signin-keeps-readings` ⚠︎contract (30 minutes at 2834) |
| 2849 | "It is in {…}, and AVAILABLE is the only state an agent is started on." | FACT-STAYS | ACTION |
| 2849 | "…replacing a credential does not un-pause an account somebody paused" | EXPLANATION-MOVES | `reauth-does-not-unpause` |
| 2860 | done | KEEP-AS-IS | control |
| 2915 | `<div class="ctl-empty">` wrapper + "This screen cannot sign in for this account" | FACT-STAYS | heading is the marker |
| 2917 | "Its provider is {…}, and the sign-in this platform performs produces a {…} credential…" | EXPLANATION-MOVES | `subscription-only-no-api-key` |
| 2925 | "Nothing about the account has changed, and nothing here failed." | FACT-STAYS | keep |
| 2976 | "The refresh token behind {…} is gone or unreadable, and only a person can replace it." | FACT-STAYS | ACTION `reauth-required` |
| 2976 | "…keeping the id, the readings, the lending list and the state. Nothing here needs…" | EXPLANATION-MOVES | `reauth-required` |
| 3073 | "Removes the account from the pool. The credential in Secret Manager is retained, not deleted…" | EXPLANATION-MOVES | `account-removal-is-reversible` |
| 3073 | "{n} agents currently hold this account. Move it to DRAINING first…" | FACT-STAYS | ACTION — count of affected agents |
| 3106 | "type the label to arm this" | KEEP-AS-IS | typed-confirmation caption |
| 3109 | "{…} — {…} The account was not removed." | FACT-STAYS | "not removed" clause |

#### The inlined help essay, `3125–3285` — moves wholesale

Four numbered `<h3>` sections with a paragraph each. This is not annotation of
anything on screen; it is a help article that happens to be rendered under a
table. **All of it moves to `#help/accounts`,** and this alone removes ~1,150
rendered words from the screen.

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 3132 | "1 · Adding an account is a sign-in" | EXPLANATION-MOVES | `sign-in-not-paste` |
| 3133 | "Name it, press Start the sign-in, open the page it gives you… That is the whole procedure." | EXPLANATION-MOVES | `sign-in-not-paste` |
| 3185 | "What the platform does with what it receives: two secrets… That split is the security boundary…" | EXPLANATION-MOVES | `credential-split` |
| 3195 | "2 · How refreshing works" | EXPLANATION-MOVES | `credential-refresh-sweep` |
| 3225 | "3 · When an account goes REAUTH_REQUIRED" | EXPLANATION-MOVES | `reauth-required` |
| 3226 | "It means the refresh token is gone or unreadable. The broker stops trying on purpose…" | EXPLANATION-MOVES | `reauth-required` |
| 3265 | "4 · Why a credential with no refresh token is refused" | EXPLANATION-MOVES | `refresh-token-required` |
| 3266 | "A sign-in normally yields a pair… the platform refuses it and stores nothing, with a 422…" | EXPLANATION-MOVES | `refresh-token-required` |
| 3274 | "The refusal is the feature… Being refused costs you one error message now." | EXPLANATION-MOVES | `refresh-token-required` |

#### `Legend()` at 3289 — 8 entries

| Line | `<dt>` | Class | Topic / marker |
|---|---|---|---|
| 3292 | "The columns are the ones `cs status` prints, on purpose" | EXPLANATION-MOVES | `accounts-table-shape` |
| 3299 | "~ means projected, not measured" | EXPLANATION-MOVES | `projected-not-measured` ⚠︎contract (30 minutes) |
| 3299 | *the distinction itself* | FACT-STAYS | the `~` glyph in the cell, already rendered |
| 3308 | "An em dash is not zero" | EXPLANATION-MOVES | `absent-vs-zero` |
| 3308 | *the distinction itself* | FACT-STAYS | **no bar is drawn at all** for an unmeasured cell (`acct-unmeasured`, 529) — the marker already exists and must not be regressed |
| 3315 | "CLEARS is the binding window, not the five-hour" | EXPLANATION-MOVES | `binding-window` |
| 3321 | "There is no amber band, deliberately" | EXPLANATION-MOVES | `no-amber-band` |
| 3329 | "Adding a second account needs a second browser application" | EXPLANATION-MOVES | ACTION `second-browser-application` |
| 3337 | "Four states, and they are not degrees of one thing" | EXPLANATION-MOVES | `account-states` |
| 3346 | "'Agents on it' is advisory" | EXPLANATION-MOVES | `advisory-vs-lease` |

---

### 3.2 `apps/swarm-ui/src/AgentDetail.tsx` — 77 rows over 70 locations — **exemplar lane owns this file**

Recorded for topic reuse only. Do not execute these rows; the lane building
`<HelpCard>` migrates this file as its worked example.

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 272 | `<p>{children}</p>` — `Note` primitive | KEEP-AS-IS | primitive |
| 648 | `{why}` — full park/failure reason | KEEP-AS-IS | value |
| 669 | "Written by the reconciler, which found this attempt in a state it could not repair." (×3 variants) | EXPLANATION-MOVES | `error-origin` |
| 761 | "Fewer documents than the task counts" | FACT-STAYS | heading is the marker |
| 763 | "The task records {…} and {…} came back. The rest are missing, not absent — every figure below…" | FACT-STAYS | count of what is missing |
| 799 | `AttemptLegend` — 7 entries, below | | |
| 802 | "The requested figure is a ceiling, not a target" (`requests == limits`) | EXPLANATION-MOVES | `requests-equal-limits` |
| 808 | "The workspace is a slice OF memory" | EXPLANATION-MOVES | `workspace-is-memory` |
| 816 | "`oom_near_miss` is the claim; the colour is not" | EXPLANATION-MOVES | `oom-near-miss` |
| 825 | "CPU is never sampled" | EXPLANATION-MOVES | `cpu-never-sampled` |
| 825 | *the distinction itself* | FACT-STAYS | the **hatched cpu track** at 1092 — already the marker |
| 832 | "A null spend figure is not a zero" | EXPLANATION-MOVES | `spend-null-not-zero` |
| 839 | "Token cost is the only cost that exists" | EXPLANATION-MOVES | `cost-is-token-cost-only` |
| 845 | "What is INSIDE a checkpoint is not recorded" | EXPLANATION-MOVES | `checkpoint-contents` |
| 918 | "not recorded — it started {…} and no finish time was ever written, so how long it ran is unknown" | FACT-STAYS | em dash + caption |
| 931 | "— never dispatched, so no execution was ever named" | FACT-STAYS | em dash + caption |
| 1087 | "The deployment answering this UI predates GET /v1/resource-classes, so the ceiling…" | FACT-STAYS | keep "ceiling unknown to this page" |
| 1087 | "The measured side below is unaffected and is real." | EXPLANATION-MOVES | `resource-class-route-missing` |
| 1092 | "The bars are hatched with no fill rather than drawn empty — an empty bar would claim 0% used." | EXPLANATION-MOVES | `absent-vs-zero` (the hatch stays) |
| 1100 | "The catalogue has no class called {…}" | FACT-STAYS | heading is the marker |
| 1104 | "The task names a resource class the platform no longer publishes, so there is nothing to compare…" | EXPLANATION-MOVES | `resource-class-renamed` |
| 1160 | "This attempt is over, so the memory figure is the last heartbeat reading before it stopped…" | FACT-STAYS | "last heartbeat, not final" clause |
| 1160 | "The final high-water mark is written at the end of an attempt…" | EXPLANATION-MOVES | `peak-memory-high-water` |
| 1181 | "This attempt ended with no peak memory recorded… the figure is an em dash rather than a zero" | FACT-STAYS | em dash |
| 1229 | "The {…} runner does not report tokens or cost at all. That is an absence of measurement…" | FACT-STAYS | em dash + clause |
| 1229 | "Spend is parsed out of the runner's result and written when the attempt ends…" | EXPLANATION-MOVES | `spend-null-not-zero` |
| 1308 | "Resumed from {…}, written by attempt {…} · {…} restored. This attempt did not start from an empty…" | KEEP-AS-IS | one-line caption of a real value |
| 1322 | "This attempt's document lists no checkpoint." | FACT-STAYS | keep |
| 1346 | "not on the attempt document" | FACT-STAYS | caption |
| 1352 | `{…}` | KEEP-AS-IS | value |
| 1407 | "The event read failed and a checkpoint's uri is only ever recorded on its event… Whether it…" | FACT-STAYS | "unknown" clause |
| 1421 | "The task's restore pointer is {…}, which no checkpoint listed above matches." | FACT-STAYS | keep |
| 1421 | "Either its event is off this page or it was written by an attempt whose document did not come…" | EXPLANATION-MOVES | `restore-pointer` |
| 1473 | "Everything here describes the LAST attempt only." | FACT-STAYS | keep |
| 1473 | "The result summary is written once, at terminal state; the earlier attempts' output was never…" | EXPLANATION-MOVES | `result-summary-last-attempt-only` |
| 1540 | "This attempt uploaded no artifacts. The read succeeded — the list is empty, not missing." | FACT-STAYS | keep |
| 1578 | "{n} skipped for exceeding the size cap, so this list is incomplete: {…}" | FACT-STAYS | count of what is missing |
| 1624 | "No log stream was uploaded for this attempt. A stream that failed to upload is absent from this…" | FACT-STAYS | keep |
| 1633 | copy-gsutil control | KEEP-AS-IS | control |
| 1648 | "{…} — not a uri, so there is nothing to fetch" | FACT-STAYS | em dash + caption |
| 1663 | "These are the files uploaded when the attempt ended. Nothing streams while an agent is running…" | EXPLANATION-MOVES | `no-log-tail` |
| 1699 | "The attempt read failed, so whether any attempt document carries a typed spend figure is unknown…" | FACT-STAYS | "unknown" clause |
| 1704 | `<dl class="kv">` | KEEP-AS-IS | data |
| 1742 | "Chosen at submission and stored on the task. It decides what happens to the agent's work once…" | EXPLANATION-MOVES | `dispatch-strategies` |
| 1800 | "The change could not be read from the workspace: {…}. This says nothing about whether the agent…" | FACT-STAYS | keep |
| 1813 | "none {…}" | KEEP-AS-IS | value |
| 1822 | "{…} · +{…} −{…}" | KEEP-AS-IS | value |
| 1836 | "· list truncated" | FACT-STAYS | truncation marker |
| 1854 | "discarded at {…} bytes — over the cap. It was not truncated: a truncated patch applies cleanly…" | FACT-STAYS | keep clause 1 |
| 1854 | "…a truncated patch applies cleanly and silently drops the rest of the change" | EXPLANATION-MOVES | `patch-cap` |
| 1860 | "none — nothing differed from the clone" | FACT-STAYS | em dash + caption |
| 1870 | "· {…}" | KEEP-AS-IS | value |
| 1885 | PR number + state chip | KEEP-AS-IS | value |
| 1890 | "· already existed, reused" | FACT-STAYS | caption |
| 1895 | "One commit on this branch was made by the worker, not the agent: the agent left changes…" | EXPLANATION-MOVES | `worker-commit` |
| 2004 | "Pushed, and no pull request — by request" | FACT-STAYS | heading is the marker |
| 2006 | "This step is a contributor in an integrate {…} workflow… Do not open one from this branch…" | EXPLANATION-MOVES | ACTION `dispatch-strategies` |
| 2023 | "The branch is pushed; no pull request was opened" | FACT-STAYS | heading is the marker |
| 2025 | "The work reached the forge on {…}. Only the pull-request call did not complete, so opening one…" | EXPLANATION-MOVES | ACTION `pr-call-failed` |
| 2046 | "Nothing was pushed — this dispatch asked for collect" | FACT-STAYS | heading is the marker |
| 2048 | "collect is the default strategy: the agent's work is harvested into this task's patch…" | EXPLANATION-MOVES | `dispatch-strategies` |
| 2066 | "Not published: this token cannot write to the repository" | FACT-STAYS | heading is the marker |
| 2068 | "The forge was asked and refused write access… granting the tenant's credential write scope…" | EXPLANATION-MOVES | ACTION `forge-write-scope` |
| 2081 | "No pull request, and no reason was recorded" | FACT-STAYS | heading is the marker |
| 2083 | "The worker writes publish_reason on every path, including the successful ones, so its absence…" | EXPLANATION-MOVES | `publish-reason-absent` |
| 2162 | `{known.body}` | KEEP-AS-IS | value |
| 2172 | "Not published, and the reason is git's own" | FACT-STAYS | heading is the marker |
| 2174 | "The push did not land. A rejected push usually means something else moved the branch…" | EXPLANATION-MOVES | `push-rejected` |
| 2227 | "{…} vCPU · {…} GiB memory, of which the workspace may take {…} GiB" / "the sizing behind this…" | KEEP-AS-IS | one-line caption of real values |
| 2255 | "What {…} runs — its image, command, timeout and checkpoint interval — is in the frozen…" | EXPLANATION-MOVES | `runner-profile-by-name` |
| 2266 | "This task's input has no prompt string." | FACT-STAYS | keep |
| 2282 | "The task document carried no metadata field at all — which is different from being submitted…" | FACT-STAYS | keep |
| 2287 | "Submitted with no metadata. The read succeeded and the object is empty — a real zero." | FACT-STAYS | keep |
| 2429 | "This page stops before the end of the run" | FACT-STAYS | heading is the marker |
| 2431 | "The route orders events oldest-first, caps the page server-side and returns no page token…" | EXPLANATION-MOVES | `events-no-page-token` |
| 2438 | "The newest event here is {…}, {…}." | KEEP-AS-IS | value |
| 2445 | "One page, oldest first… a page that looks complete is not evidence that it is." | EXPLANATION-MOVES | `events-no-page-token` |
| 2460 | "No event on this page belongs here — the page ends before them, or none were written." | FACT-STAYS | keep |

---

### 3.3 `apps/swarm-ui/src/Overview.tsx` — 27 rows over 22 locations

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 133 | "The API answered a sign-in page instead of data. Nothing on this screen is a reading of the…" | FACT-STAYS | keep |
| 186 | scope · provenance · refresh | KEEP-AS-IS | control strip |
| 810 | "reading… / absent / not recorded" | FACT-STAYS | em dash / caption primitive |
| 885 | `<p>{children}</p>` primitive | KEEP-AS-IS | primitive |
| 961 | "A task clears every pool in its list at once, so its ceiling is the minimum across them…" | EXPLANATION-MOVES | `pools-all-at-once` |
| 977 | "for tenant {…} — not a platform figure · {n} pools read" | FACT-STAYS | scope marker; keep |
| 1080 | pools-refusing / paused chips | KEEP-AS-IS | value |
| 1406 | "{n} of {n} attempt reads failed, so their spend is in none of these figures." | FACT-STAYS | count of what is missing |
| 1406 | "One of them failed with: {…} — the other {n} may have failed for other reasons…" | EXPLANATION-MOVES | `partial-read` |
| 1428 | "{n} attempts across the {n} most recently created tasks that have run" | FACT-STAYS | sample-size marker |
| 1446 | "summed {…} · this figure does not re-poll — it moves only when you press refresh…" | FACT-STAYS | keep "does not re-poll" |
| 1446 | "…unlike the capacity, task, lease, provider and account reads, which re-run every 20 seconds" | EXPLANATION-MOVES | `poll-cadence` ⚠︎contract — "20 seconds" duplicates `20_000` at `Overview.tsx:108` |
| 1451 | "{n} attempts carry no cost figure and are counted as unmeasured, not as zero" | FACT-STAYS | keep |
| 1451 | "· GCP infrastructure cost is not recorded anywhere on this platform" | EXPLANATION-MOVES | `cost-is-token-cost-only` |
| 1611 | "The read succeeded and returned nothing. Agents on a subscription profile have no account…" | FACT-STAYS | keep sentence 1 |
| 1611 | "…to run against until one is added" | EXPLANATION-MOVES | `park-on-missing-credential` |
| 1668 | "sign in again" / "never polled" | FACT-STAYS | ACTION — caption on the account row |
| 1724 | "{n} accounts · showing the {n} that most need looking at" | FACT-STAYS | truncation marker |
| 1724 | "· utilisation is of the BINDING window, never an average of the two · ~ marks a figure…" | EXPLANATION-MOVES | `binding-window` + `projected-not-measured` |
| 2165 | "Nothing is wrong that this platform records" / "All {n} checks ran and all {n} came back clear." | FACT-STAYS | keep both — an all-clear must name its basis |
| 2167 | "This is an all-clear from successful reads, not from silence." | EXPLANATION-MOVES | `all-clear-basis` |
| 2176 | "Nothing wrong in the {n} check(s) that ran" | FACT-STAYS | heading is the marker |
| 2183 | "{n} of {n} could not run, so this is not an all-clear — it is a partial one…" | FACT-STAYS | count of what is missing |
| 2220 | "clear: {…} — {…}" | KEEP-AS-IS | value |
| 2230 | "still reading: {…}" | KEEP-AS-IS | value |
| 2235 | "{n} check(s) could not run, so this list is incomplete: {…}" | FACT-STAYS | count of what is missing |
| 2253 | "**{headline}** — {detail}" | KEEP-AS-IS | value |

---

### 3.4 `apps/swarm-ui/src/Dispatch.tsx` (+ its strings in `types.ts`) — 23 rows

The consequence copy is the one place in the product where prose is the
*control*, not commentary on it — §B7 must not flatten it into a `?`.

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 94 | `<legend>` "how this work gets merged" | KEEP-AS-IS | fieldset legend |
| 121 | "{headline}" — "No pull request. Nothing is pushed." / "Up to {n} pull requests — one per step." | FACT-STAYS | **the number on the option is the control.** Never moves |
| 125 | "Not available for a single task: it names a final step that receives the other steps' patches…" | EXPLANATION-MOVES | `integrate-needs-final-step` |
| 144 | `{errors.strategy}` (422 text, `role="alert"`) | FACT-STAYS | server refusal |
| 146 | `<label>` "what carries work between steps" | KEEP-AS-IS | label |
| 161 | `{CARRIER_DETAIL[carrier]}` — "Intended to carry a step's work to the next one as…" | EXPLANATION-MOVES | `dispatch-carrier` |
| 165 | `{CARRIER_NOTE}` — "Recorded on the task and returned by the API, but no worker code reads it yet." | FACT-STAYS | **must not move.** A control that does nothing must say so on the surface |
| 166 | `{errors.carrier}` | FACT-STAYS | server refusal |
| 179 | "The repository the agent clones. Without one the agent starts in an empty workspace…" | EXPLANATION-MOVES | `repository-url` |
| 188 | "carrier {…} has to push, so the API will refuse this without a repository URL." | FACT-STAYS | pre-refusal warning |
| 194 | `{errors.repository_url}` | FACT-STAYS | server refusal |
| 214 | `{c.headline}` | FACT-STAYS | the consequence, in numbers |
| 215 | `{c.detail}` | EXPLANATION-MOVES | `dispatch-strategies` |
| 236 | "As drawn, {…} is the only step nothing depends on, so it is the step that would open it." | FACT-STAYS | live DAG preview |
| 246 | "As drawn, no step is final — every step is depended on by another. The API validates the graph…" | FACT-STAYS | live DAG preview |
| 253 | "As drawn, {n} steps are final ({…}). One pull request needs exactly one final step…" | FACT-STAYS | live DAG preview |
| 283 | dispatch fact chip | KEEP-AS-IS | value |
| 288 | dispatch fact chip | KEEP-AS-IS | value |
| 296 | dispatch fact chip | KEEP-AS-IS | value |
| 312 | "{n} · {n} upstream task(s), in the order they are applied" | KEEP-AS-IS | value |
| 324 | "Nothing, by request. The patch is in this task's artifacts." | FACT-STAYS | keep |
| 348 | "This API did not report a dispatch" | FACT-STAYS | heading is the marker |
| 350 | "task_to_api sends dispatch on every task, filling the defaults for tasks that predate the…" | EXPLANATION-MOVES | `dispatch-absent-is-old-api` |

---

### 3.5 `apps/swarm-ui/src/Submit.tsx` — 16 rows over 13 locations

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 217 | `{bad.runner_profile}` (422, `role="alert"`) | FACT-STAYS | server refusal |
| 220 | `<textarea>` | KEEP-AS-IS | control |
| 221 | "Opaque to the platform: handed to the profile's agent, validated only for size." | EXPLANATION-MOVES | `input-is-opaque` |
| 222 | `{bad.input}` | FACT-STAYS | server refusal |
| 234 | submit button | KEEP-AS-IS | control |
| 240 | "The API refused this submission and did not say which field: {…}" | FACT-STAYS | keep |
| 248 | "This failed on a write. If it failed after reaching the API the task may exist anyway — check…" | FACT-STAYS | ACTION — ambiguous write |
| 273 | "{…} on {…} · provider {…} · costs {n} weighted unit(s) in each of its {n} pools…" | FACT-STAYS | keep the figures |
| 273 | "…every one of which must admit it in the same transaction" | EXPLANATION-MOVES | `pools-all-at-once` + `units-not-agents` |
| 279 | "No pool in this profile's list is configured, so nothing caps it right now." | FACT-STAYS | keep |
| 279 | "How much room is left could not be measured: {n} of its pools could not be read" | FACT-STAYS | **never render 0 here** — the inline comment at 279 says why; that comment is the source for the topic |
| 279 | *(the reasoning in the comment)* | EXPLANATION-MOVES | `room-unknown-not-zero` |
| 321 | "Not in the response, so not counted: {…}." | FACT-STAYS | count of what is missing |
| 331 | "That is not a running agent: only LEASED, DISPATCHED, STARTING and RUNNING hold capacity…" | EXPLANATION-MOVES | `capacity-states` ⚠︎contract — the four state names are `CONCURRENCY_STATES` |
| 336 | `{task.id}` | KEEP-AS-IS | value |
| 341 | "The scheduler was woken by this submission." / "…it will pick this up on its next pass." | FACT-STAYS | keep |

---

### 3.6 `apps/swarm-ui/src/Runtimes.tsx` — 19 rows over 18 locations

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 78 | "A caller picks a runtime by name and supplies nothing else — no image, no command, no size…" | EXPLANATION-MOVES | `runner-profile-by-name` |
| 83 | "This is the dispatch topology, not a machine inventory. No route in this platform serves nodes…" | EXPLANATION-MOVES | `not-a-machine-inventory` |
| 98 | "{n} runtime(s) · the whole catalogue this response carried, not a page of it" | FACT-STAYS | completeness marker — keep |
| 178 | "The catalogue below loaded, so which runtimes land on which backend is trustworthy. The load on…" | FACT-STAYS | keep clause 1 + "columns are dashes, not zeros" |
| 178 | "…The columns that would have carried it are dashes, not zeros." | EXPLANATION-MOVES | `absent-vs-zero` |
| 219 | "Grouped by the backend each runtime resolves to. The full pool board — every family, not just…" | EXPLANATION-MOVES | `declared-vs-resolved-backend` |
| 420 | "· declared {…}, resolved by the platform" | FACT-STAYS | caption |
| 449 | "Nothing in this response separates it from the rest of the catalogue — same backend, same image…" | EXPLANATION-MOVES | `what-sets-it-apart-is-arithmetic` |
| 479 | "This runtime consumes no external provider's quota, so it works on a tenant that has registered…" | EXPLANATION-MOVES | `runtime-needs-no-provider` |
| 490 | "— the catalogue names this provider and lists no environment variable for it. That is what the…" | FACT-STAYS | em dash + keep "not a failed read" |
| 507 | "· names only, never values" | FACT-STAYS | caption |
| 606 | "Every size below is the one the runtime itself reported… What is missing is a class that no…" | FACT-STAYS | keep the "cannot contain" clause |
| 645 | "nothing routes here" | FACT-STAYS | caption |
| 659 | "Weight is what one agent of a class adds to every pool it has to clear, which is why capacity…" | EXPLANATION-MOVES | `units-not-agents` |
| 672 | legend: "Nothing here is written down in this client" | EXPLANATION-MOVES | `catalogue-from-route` |
| 680 | legend: "Declared backend and resolved backend" | EXPLANATION-MOVES | `declared-vs-resolved-backend` |
| 689 | legend: "Workspace comes out of memory" | EXPLANATION-MOVES | `workspace-is-memory` + `requests-equal-limits` |
| 697 | legend: "Credential names, never values" | EXPLANATION-MOVES | `credential-names-not-values` |
| 704 | legend: "'What sets it apart' is arithmetic, not commentary" | EXPLANATION-MOVES | `what-sets-it-apart-is-arithmetic` |

---

### 3.7 `apps/swarm-ui/src/Shell.tsx` — 14 rows over 11 locations

`Shell` is the frame every screen renders inside, so one change here lands on
every screen at once. It is the highest leverage-per-row file in the list.

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 97 | `<p class="sub">` → `SubLine` | KEEP-AS-IS | structure |
| 109 | `{empty.body}` | FACT-STAYS | per-screen empty copy; audited at its call sites |
| 110 | "Checked {…}." | FACT-STAYS | freshness marker |
| 151 | SubLine: "Reading… / Nothing to show / **not refreshed** · showing {…} / Could not read." | FACT-STAYS | **the stale marker.** Never moves |
| 178 | "The numbers below were read {…} and have not been refreshed since. {…}" | FACT-STAYS | stale banner — keep |
| 178 | *(why a banner and not a toast — the docstring at 168)* | EXPLANATION-MOVES | `stale-data` |
| 203 | "You are not in an admin group, so this screen has nothing to show you." | FACT-STAYS | keep clause 1 |
| 203 | "Nothing is wrong with the platform, and nothing failed." | EXPLANATION-MOVES | `admin-gate-not-failure` |
| 207 | `{error.message}` | KEEP-AS-IS | value |
| 215 | `{error.message}` | KEEP-AS-IS | value |
| 216 | `{errorReassurance(error)}` | FACT-STAYS | see `fetch.ts:165` — **the sentence that must appear on every failure surface** |
| 218 | "HTTP {…} · {…}" | KEEP-AS-IS | value |
| 233 | "Retrying is disabled for {n} more seconds, because the API asked us to wait that long." | FACT-STAYS | keep clause 1 |
| 233 | "…because the API asked us to wait that long" | EXPLANATION-MOVES | `rate-limited` |

---

### 3.8 `apps/swarm-ui/src/PlatformCounts.tsx` — 13 rows over 10 locations

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 53 | "This is not loaded automatically. Each run performs one Firestore count() per task state…" | EXPLANATION-MOVES | `count-cost` ⚠︎contract — "twelve, or twenty-four" is `len(TaskState)` typed out |
| 65 | "{n} successful run(s) this session · last read {…}" | FACT-STAYS | provenance marker |
| 76 | "The read succeeded and returned no counts at all. That should be impossible…" | FACT-STAYS | keep |
| 76 | "…count_tasks_by_state iterates the whole enum and writes a key for every state" | EXPLANATION-MOVES | `counts-empty-is-a-failure` |
| 103 | "Admin only. The platform figures are absent from this response rather than zero…" | FACT-STAYS | keep |
| 103 | "…the API omits the field for a non-admin, and an omitted field is not a count of nothing" | EXPLANATION-MOVES | `admin-gate-not-failure` |
| 120 | "Admin only — you are not in an admin group. Nothing failed." | FACT-STAYS | keep |
| 129 | "{code} — {message}" | KEEP-AS-IS | value |
| 134 | "No counts are shown, because none arrived. This says nothing about how much work the platform…" | FACT-STAYS | keep |
| 169 | "{n} state(s) did not come back ({…}). No total is shown, because a sum over a partial response…" | FACT-STAYS | **the withheld total.** Count of what is missing stays |
| 169 | "…because a sum over a partial response would look like a complete one" | EXPLANATION-MOVES | `withheld-total` |
| 187 | "{n} task documents across the nine states that are written" | FACT-STAYS | ⚠︎contract — "nine" is typed out; `REAL_STATES.length` is in scope two lines below |
| 190 | "{n} of the {n} states in the contract are never written to a task document…" | EXPLANATION-MOVES | `never-written-states` — already derives its numbers; the model for every other row |

---

### 3.9 `apps/swarm-ui/src/SubmitWorkflow.tsx` — 14 rows over 12 locations

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 211 | add step | KEEP-AS-IS | control |
| 216 | "Every step needs an id: dependencies are declared by id, not by position." | EXPLANATION-MOVES | `step-ids` |
| 219 | "The step limit could not be read, so nothing caps this form and no number is guessed." | FACT-STAYS | keep |
| 219 | "The API enforces its own and names it." | EXPLANATION-MOVES | `step-limit-unknown` |
| 267 | "{…} · {n} units · {…}" | KEEP-AS-IS | value |
| 271 | "depends on" | KEEP-AS-IS | label |
| 272 | "— no other named step yet" | FACT-STAYS | em dash + caption |
| 297 | "The 201 carried no dispatch block, so this screen cannot say what strategy was stored — an API…" | FACT-STAYS | keep |
| 312 | "Accepted as {…} / {…}. Step {…} opens it." | KEEP-AS-IS | value |
| 335 | "{…} · {n} steps · created {…}. No progress is shown here: a workflow's own state is written once…" | FACT-STAYS | keep "no progress is shown here" |
| 335 | "…a workflow's own state is written once at submission and never updated" | EXPLANATION-MOVES | `workflow-state-is-stale` |
| 354 | `{error.message}` | KEEP-AS-IS | value |
| 359 | "The request left this browser without a usable answer, so the workflow may exist. Open the…" | FACT-STAYS | ACTION — ambiguous write |
| 362 | "HTTP {…} · {…}" | KEEP-AS-IS | value |

---

### 3.10 `apps/swarm-ui/src/Workflows.tsx` — 10 rows

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 67 | "The workflows themselves loaded, so the steps, their order and their dependencies below are…" | FACT-STAYS | keep |
| 72 | "Nothing below should be taken as evidence that a step is or is not running." | FACT-STAYS | keep |
| 163 | "State could not be checked: {n} step(s) unread ({…}). The stored value is {…} and nothing has…" | FACT-STAYS | count of what is missing |
| 171 | "Stored state was {…}; the steps say {…}." | FACT-STAYS | **struck stored state** — §B8 item 16 |
| 203 | "{roll.text} · cancel requested" | FACT-STAYS | `.rollup.untrusted` strike already exists |
| 203 | *(why `cancel_requested` is not folded into the state — comment at 203)* | EXPLANATION-MOVES | `cancel-is-a-request` |
| 271 | "None of this workflow's tasks reported a dispatch, so what it publishes is not shown." | FACT-STAYS | keep |
| 271 | "That is an API older than the field, not a workflow that publishes nothing." | EXPLANATION-MOVES | `dispatch-absent-is-old-api` |
| 280 | "{…} · {…} · integrator {…}" | KEEP-AS-IS | value |
| 283 | "· integrator {…}" | KEEP-AS-IS | value |

---

### 3.11 `apps/swarm-ui/src/Holders.tsx` — 9 rows over 6 locations

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 76 | "The leases loaded, so the table below is trustworthy. The comparison is not available: {…}" | FACT-STAYS | keep |
| 110 | "Every pool a loaded lease names holds exactly the units those leases account for." | FACT-STAYS | keep — it is the reconciliation result |
| 145 | "Admission writes the lease and increments every pool in its list in one transaction, so these…" | EXPLANATION-MOVES | `lease-vs-counter` |
| 145 | "…a truncated page produces a delta that looks the same" | FACT-STAYS | keep — it bounds the claim above |
| 197 | "No loaded lease names a resource class." | FACT-STAYS | keep |
| 213 | "{n} lease(s) name no resource pool and {n} counted in no class — not folded into standard…" | FACT-STAYS | count of what is missing |
| 213 | "…which would understate the rest" | EXPLANATION-MOVES | `unclassified-leases` |
| 256 | "{n} rows returned · units are weighted, not agent counts" | FACT-STAYS | page marker |
| 256 | "…units are weighted, not agent counts" | EXPLANATION-MOVES | `units-not-agents` |

---

### 3.12 `apps/swarm-ui/src/AttemptTimeline.tsx` — 7 rows

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 129 | "A failed read, not a task with no events. The attempts below are complete." | FACT-STAYS | keep |
| 137 | "Every figure on these cards is that attempt's own… A null exit code or peak RSS is a missing…" | EXPLANATION-MOVES | `result-summary-last-attempt-only` |
| 153 | "Zero events came back, yet a task is written with its submitted event in the same batch — a…" | FACT-STAYS | keep |
| 161 | "The events endpoint orders oldest-first, caps the page server-side and returns no page token…" | EXPLANATION-MOVES | `events-no-page-token` |
| 168 | "{n} on this page, the oldest {n} this task recorded · no page token, so newer events may exist…" | FACT-STAYS | page marker — keep |
| 189 | `<dl class="kv">` | KEEP-AS-IS | data |
| 209 | "No event on this page belongs here — the page ends before it, or none were written." | FACT-STAYS | keep |

---

### 3.13 `apps/swarm-ui/src/App.tsx` — 8 rows

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 385 | `{section.question}` — the section subtitle, rendered under every section head | EXPLANATION-MOVES | §B7.3: "each section's `question` field lives here too, so it is stated once instead of on all four panes" |
| 414 | `title={s.question}` on the tab | DELETE | duplicates 385; a native tooltip is neither |
| 527 | "No such pane" | FACT-STAYS | heading is the marker |
| 529 | "This address names a pane of {…} that does not exist. Nothing was read and nothing failed…" | FACT-STAYS | keep |
| 623 | "What this UI reads, and how those reads are going." | KEEP-AS-IS | one-line caption |
| 625 | "This is not a list of the endpoints SwarmCloud offers. It is every route this browser tab has…" | EXPLANATION-MOVES | `provenance-strip` |
| 636 | "This page issues no requests of its own, so it starts empty by design. Open any section…" | FACT-STAYS | keep clause 1 |
| 641 | "Nothing failed, and this says nothing about whether the API is reachable." | FACT-STAYS | keep |

---

### 3.14 `apps/swarm-ui/src/Profiles.tsx` — 9 rows

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 83 | "The capacity read succeeded and returned {n} pools, so this is not a failed query — runner_…" | FACT-STAYS | keep |
| 94 | "A task must clear every pool in its profile's list at the same moment. Capacity is the minimum…" | EXPLANATION-MOVES | `pools-all-at-once` |
| 98 | "These pool lists are for tenant {…}… They describe what a task you submit has to clear — not…" | EXPLANATION-MOVES | `tenant-scope` |
| 107 | "{n} profiles · the whole catalogue this response carried, not a page of it" | FACT-STAYS | completeness marker |
| 140 | "{n} could start for {…}" / "not measured — a pool could not be read" | FACT-STAYS | em dash variant |
| 159 | `<dl class="kv">` | KEEP-AS-IS | data |
| 235 | "No pool in this list is configured, so nothing here limits this profile and there is no number…" | FACT-STAYS | keep — never a 0 |
| 260 | legend: "Units, not agents" | EXPLANATION-MOVES | `units-not-agents` |
| 266 | legend: "Callers pick a profile by name" | EXPLANATION-MOVES | `runner-profile-by-name` |

---

### 3.15 `apps/swarm-ui/src/Activity.tsx` — 7 rows over 6 locations

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 138 | "Older tasks exist beyond this window — everything below describes these {n} rows and the span…" | FACT-STAYS | window marker — keep |
| 208 | "{n} terminal task(s) carry no completed_at… this is a data bug — they are counted as still…" | FACT-STAYS | keep |
| 231 | "succeeded / failed / cancelled / still open · {…} stacks bucket on completed_at; a task's…" | EXPLANATION-MOVES | `bucketed-on-completed-at` (the four series labels stay) |
| 322 | "{n}% of rows carry usage — the rest predate the capture, so any total would understate." | FACT-STAYS | keep |
| 407 | "Grouped client-side over the {n} rows in the window. There is no server-side filter or index on…" | FACT-STAYS | keep clause 1 |
| 407 | "…an engineer whose work fell outside the window is absent rather than shown as zero" | EXPLANATION-MOVES | `client-side-grouping` |
| 489 | "monthly_budget_usd is deliberately not shown… Rendering it would be rendering a permanent blank…" | EXPLANATION-MOVES | `no-cost-attribution` |

---

### 3.16 `apps/swarm-ui/src/Blockers.tsx` — 6 rows over 4 locations

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 50 | "**This list is incomplete.** {n} pool(s) could not be read ({…})… the count is withheld rather…" | FACT-STAYS | **withheld count.** Never moves |
| 50 | "This build of the API did not send the admission block, so nothing below was measured." | FACT-STAYS | keep |
| 124 | "No pool is refusing this profile." / "No refusal was measured — but see above: the list is…" | FACT-STAYS | keep both variants |
| 233 | "Not computed: a ceiling that was never read cannot be compared against one that was." | FACT-STAYS | keep |
| 259 | "Worked out from the pool counts read {…}. Leases are taken and released continuously…" | EXPLANATION-MOVES | `blockers-are-a-snapshot` |
| 259 | "One pool at a time, because lifting two at once is a different question." | EXPLANATION-MOVES | `blockers-one-at-a-time` |

---

### 3.17 `apps/swarm-ui/src/Capacity.tsx` — 7 rows over 6 locations

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 173 | "How many more agents of each profile could be admitted right now, for this tenant." | EXPLANATION-MOVES | `tenant-scope` |
| 173 | "An em dash is not a zero: it means the number was not measured, and the cell says why." | EXPLANATION-MOVES | `absent-vs-zero` (the em dash itself stays) |
| 199 | "{n} pools in its list · {n} unit(s) added to each on admission" | FACT-STAYS | keep the figures |
| 276 | "A task must clear every pool in its list at the same moment. Capacity is the minimum across…" | EXPLANATION-MOVES | `pools-all-at-once` |
| 449 | legend: "Units, not agents" | EXPLANATION-MOVES | `units-not-agents` ⚠︎contract — "standard 1, browser 2, large 4" restates `RESOURCE_CLASSES`; `units` is on the route |
| 455 | legend: "Freshness is this page's own read time" | EXPLANATION-MOVES | `freshness-not-change-time` |
| 465 | legend: "Headroom is for your tenant" | EXPLANATION-MOVES | `tenant-scope` |

---

### 3.18 `apps/swarm-ui/src/AdminSettings.tsx` — 5 rows over 4 locations

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 55 | "A task must clear every pool its profile lists, so its ceiling is the minimum across them…" | EXPLANATION-MOVES | `pools-all-at-once` |
| 103 | "'Ceiling' is how many agents of that profile could run at once, not units: a browser agent…" | EXPLANATION-MOVES | `units-not-agents` ⚠︎contract — "weighs 2 and a large one 4" |
| 140 | "A change writes hard_limit and nothing else — the active counter is owned by the admission…" | EXPLANATION-MOVES | `hard-limit-only` |
| 140 | "Lowering a ceiling below what is currently in use does not evict anything" | FACT-STAYS | ACTION — consequence of the control being used |
| 207 | "no route for this pool kind" | FACT-STAYS | caption on a disabled control |

---

### 3.19 `apps/swarm-ui/src/QuotaDetail.tsx` — 7 rows over 4 legend entries (legend only; 0 `<p>`)

The clearest split in the file set, and the one §B7 names.

| Line | `<dt>` / text | Class | Topic / marker |
|---|---|---|---|
| 120 | "Six states, not five" — THROTTLED is the common case and the one most easily missed | EXPLANATION-MOVES | `provider-quota-states` |
| 128 | "UNKNOWN is not healthy" — no worker has reported recently | EXPLANATION-MOVES | `quota-unknown-not-healthy` |
| 128 | *the distinction itself* | FACT-STAYS | UNKNOWN must render in its own non-green treatment, not fall through to a neutral chip |
| 133 | "An effective limit of 0 is a fact" — "the only zero here that means something rather than nothing" | EXPLANATION-MOVES | `effective-limit-zero` |
| 133 | *the distinction itself* | FACT-STAYS | **this is the hard one.** A measured `0` here and an unmeasured cell elsewhere on the same screen currently read alike once the sentence is gone. The `0` must carry a treatment that the em-dash cells do not — a solid figure against the em dash, and the state chip (EXHAUSTED / DISABLED / COOLDOWN) on the same row as its cause |
| 139 | "A provider with no document does not appear" — absence means "never used", not "no such provider" | EXPLANATION-MOVES | `quota-doc-absence` |
| 139 | *the distinction itself* | FACT-STAYS | the screen must state the row count and that it lists **documents**, not providers |

---

### 3.20 `apps/swarm-ui/src/Agents.tsx` — 4 rows over 3 locations

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 166 | "filters apply to the {n} loaded rows" | FACT-STAYS | page marker |
| 172 | "No agent is holding a pool slot right now. This is a real zero from a successful read." | FACT-STAYS | keep |
| 172 | "Nothing is waiting. READY and PARKED work costs nothing, so an empty tab here is normal." | EXPLANATION-MOVES | `capacity-states` |
| 191 | "More rows exist beyond this page. Counts and grouping above describe only what is loaded." | FACT-STAYS | page marker |

---

### 3.21 `apps/swarm-ui/src/ErrorBoundary.tsx` — 2 rows

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 39 | "Something in this page threw an error and it could not finish rendering." | FACT-STAYS | keep |
| 42 | "This is a bug in the UI, not a statement about the platform. Whatever is running is still…" | FACT-STAYS | keep — same rule as `errorReassurance` |

---

### 3.22 `apps/swarm-ui/src/types.ts` — 10 rendered strings

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 534 | `collect.headline` "No pull request. Nothing is pushed." | FACT-STAYS | the control's own number |
| 536 | `collect.detail` "Each of the {n} steps harvests its patch into the task's own GCS prefix…" | EXPLANATION-MOVES | `dispatch-strategies` |
| 546 | `direct-pr.headline` "Up to {n} pull requests — one per step." | FACT-STAYS | the control's own number |
| 548 | `direct-pr.detail` "Every step pushes its own branch and opens its own pull request…" | EXPLANATION-MOVES | `dispatch-strategies` |
| 557 | `integrate.headline` "Exactly one pull request for the whole workflow." | FACT-STAYS | the control's own number |
| 559 | `integrate.detail` (n<2) "One final step receives the others' work… there is nothing to integrate." | EXPLANATION-MOVES | `integrate-needs-final-step` |
| 562 | `integrate.detail` (n≥2) "The final step merges the other {n} steps' branches into its own…" | EXPLANATION-MOVES | `dispatch-strategies` |
| 595 | `CARRIER_NOTE` "Recorded on the task and returned by the API, but no worker code reads it yet." | FACT-STAYS | **a control that does nothing must say so on the surface.** Never moves |
| 600 | `CARRIER_DETAIL.checkpoints` "The default. Intended to carry a step's work to the next one as…" | EXPLANATION-MOVES | `dispatch-carrier` |
| 603 | `CARRIER_DETAIL.branches` "Intended to carry a step's work to the next one as a pushed branch…" | EXPLANATION-MOVES | `dispatch-carrier` |

---

### 3.23 `apps/swarm-ui/src/fetch.ts` — 4 rows covering 16 rendered strings

| Line | Text | Class | Topic / marker |
|---|---|---|---|
| 141–157 | `errorHeading` — 13 short headings ("Your session expired", "Refreshing paused", …) | KEEP-AS-IS | each is a 2–7 word heading, not prose |
| 172 | `errorReassurance` (not_found) "This task does not exist, or it belongs to another tenant. The API…" | FACT-STAYS | keep — the 404 is deliberately ambiguous and must not be resolved in copy |
| 175 | `errorReassurance` (unreachable) "The request never reached the platform, so this says nothing about…" | FACT-STAYS | keep |
| 177 | `errorReassurance` (default) "This is a failure to read the platform. It says nothing about what is…" | FACT-STAYS | **the single most load-bearing sentence in the product.** It appears on every failure surface; it is one line; it must not become a `?` |

---

## 4. The duplicate concepts — where the page actually gets cleaner

Collapsing these is the substance of the win. The table below is generated
from the `EXPLANATION-MOVES` rows in §3, not written by hand.

**108 distinct topic ids** are proposed, across 168 topic references on the 163
`EXPLANATION-MOVES` rows. **34 of those topics are cited by two or more rows,
covering 94 references** — those are the duplicates, and collapsing them is
where the page actually gets cleaner. The other 74 are one-offs.

| Topic id | Files | Rows | Sites |
|---|---:|---:|---|
| `dispatch-strategies` | 3 | 7 | `AgentDetail.tsx:1742`, `AgentDetail.tsx:2006`, `AgentDetail.tsx:2048`, `Dispatch.tsx:215`, `types.ts:536`, `types.ts:548`, `types.ts:562` |
| `units-not-agents` | 6 | 6 | `Submit.tsx:273`, `Runtimes.tsx:659`, `Holders.tsx:256`, `Profiles.tsx:260`, `Capacity.tsx:449`, `AdminSettings.tsx:103` |
| `pools-all-at-once` | 5 | 5 | `Overview.tsx:961`, `Submit.tsx:273`, `Profiles.tsx:94`, `Capacity.tsx:276`, `AdminSettings.tsx:55` |
| `absent-vs-zero` | 4 | 4 | `Accounts.tsx:3308`, `AgentDetail.tsx:1092`, `Runtimes.tsx:178`, `Capacity.tsx:173` |
| `signin-verify-by-reload` | 1 | 4 | `Accounts.tsx:1741`, `Accounts.tsx:1787`, `Accounts.tsx:1850`, `Accounts.tsx:1926` |
| `admin-gate-not-failure` | 3 | 3 | `Accounts.tsx:1368`, `Shell.tsx:203`, `PlatformCounts.tsx:103` |
| `credential-split` | 1 | 3 | `Accounts.tsx:2568`, `Accounts.tsx:2771`, `Accounts.tsx:3185` |
| `dispatch-carrier` | 2 | 3 | `Dispatch.tsx:161`, `types.ts:600`, `types.ts:603` |
| `events-no-page-token` | 2 | 3 | `AgentDetail.tsx:2431`, `AgentDetail.tsx:2445`, `AttemptTimeline.tsx:161` |
| `projected-not-measured` | 2 | 3 | `Accounts.tsx:433`, `Accounts.tsx:3299`, `Overview.tsx:1724` |
| `reauth-required` | 1 | 3 | `Accounts.tsx:2976`, `Accounts.tsx:3225`, `Accounts.tsx:3226` |
| `refresh-token-required` | 1 | 3 | `Accounts.tsx:3265`, `Accounts.tsx:3266`, `Accounts.tsx:3274` |
| `runner-profile-by-name` | 3 | 3 | `AgentDetail.tsx:2255`, `Runtimes.tsx:78`, `Profiles.tsx:266` |
| `sign-in-not-paste` | 1 | 3 | `Accounts.tsx:2568`, `Accounts.tsx:3132`, `Accounts.tsx:3133` |
| `tenant-scope` | 2 | 3 | `Profiles.tsx:98`, `Capacity.tsx:173`, `Capacity.tsx:465` |
| `account-label-rules` | 1 | 2 | `Accounts.tsx:2667`, `Accounts.tsx:2732` |
| `advisory-vs-lease` | 1 | 2 | `Accounts.tsx:811`, `Accounts.tsx:3346` |
| `binding-window` | 2 | 2 | `Accounts.tsx:3315`, `Overview.tsx:1724` |
| `capacity-states` | 2 | 2 | `Submit.tsx:331`, `Agents.tsx:172` |
| `cost-is-token-cost-only` | 2 | 2 | `AgentDetail.tsx:839`, `Overview.tsx:1451` |
| `declared-vs-resolved-backend` | 1 | 2 | `Runtimes.tsx:219`, `Runtimes.tsx:680` |
| `dispatch-absent-is-old-api` | 2 | 2 | `Dispatch.tsx:350`, `Workflows.tsx:271` |
| `integrate-needs-final-step` | 2 | 2 | `Dispatch.tsx:125`, `types.ts:559` |
| `lending` | 1 | 2 | `Accounts.tsx:1328`, `Accounts.tsx:1362` |
| `park-on-missing-credential` | 2 | 2 | `Accounts.tsx:378`, `Overview.tsx:1611` |
| `reauth-does-not-unpause` | 1 | 2 | `Accounts.tsx:2794`, `Accounts.tsx:2849` |
| `requests-equal-limits` | 2 | 2 | `AgentDetail.tsx:802`, `Runtimes.tsx:689` |
| `result-summary-last-attempt-only` | 2 | 2 | `AgentDetail.tsx:1473`, `AttemptTimeline.tsx:137` |
| `second-browser-application` | 1 | 2 | `Accounts.tsx:2091`, `Accounts.tsx:3329` |
| `signin-code-single-use` | 1 | 2 | `Accounts.tsx:2039`, `Accounts.tsx:2059` |
| `spend-null-not-zero` | 1 | 2 | `AgentDetail.tsx:832`, `AgentDetail.tsx:1229` |
| `subscription-only-no-api-key` | 1 | 2 | `Accounts.tsx:2643`, `Accounts.tsx:2917` |
| `what-sets-it-apart-is-arithmetic` | 1 | 2 | `Runtimes.tsx:449`, `Runtimes.tsx:704` |
| `workspace-is-memory` | 2 | 2 | `AgentDetail.tsx:808`, `Runtimes.tsx:689` |

A topic cited by one row is not automatically waste: `withheld-total`
(`PlatformCounts.tsx:169`) and `room-unknown-not-zero` (`Submit.tsx:279`) are
each cited once and each explain a rule the whole product depends on. The
74 one-offs are the long tail of the Help section, not candidates for deletion.

**The single biggest collapse is not in this table.** `App.tsx:385` renders
`section.question` under every section head, and `App.tsx:414` repeats the same
string as a `title` on every tab. Six sections × up to five tabs each is the
same sentence rendered up to eleven times per section per navigation. §B7.3
already calls for it: state it once, in `#help/<section>`.

---

## 5. Contract and platform values restated in prose ⚠︎

`scripts/lib/check-contract-parity.sh` asserts the **shell/jq** restatements of
the frozen contract. Its target list is `FS_JQ` and `scripts/`; it does not read
TypeScript at all. So every number below is a copy with nothing asserting it.

| Where | Value in prose | Source of truth | Servable? |
|---|---|---|---|
| `Capacity.tsx:449` | "standard 1, browser 2, large 4" | `swarm_common/profiles.py:78-80` `RESOURCE_CLASSES[*].units` | **yes** — `units` is on the wire (`types.ts:51,98,847,960`) |
| `AdminSettings.tsx:103` | "a browser agent weighs 2 and a large one 4" | same | **yes** |
| `Accounts.tsx:518,738,828,2834,3303` (×5) | "the 30 minutes a reading is trusted for" | `quota_broker/accounts.py:92` `DEFAULT_STALE_AFTER = timedelta(minutes=30)` | **no route publishes it** — this is a request to Track A, not a change |
| `PlatformCounts.tsx:53` | "twelve, or twenty-four as an admin" | `len(TaskState)` = 12 | derivable in the client from `REAL_STATES + NEVER_WRITTEN`, which the same file already does at `:193` |
| `PlatformCounts.tsx:187` | "the nine states that are written" | `REAL_STATES.length`, imported **in the same file** | trivially derivable; this one is a straight bug |
| `Submit.tsx:331` | "only LEASED, DISPATCHED, STARTING and RUNNING hold capacity" | `swarm_common/states.py:35` `CONCURRENCY_STATES` | client has `CONCURRENCY_STATES` at `types.ts:1399` — render from the set |
| `Overview.tsx:1446` | "re-run every 20 seconds" | `Overview.tsx:108` `setInterval(…, 20_000)` — **in the same file** | interpolate the constant |
| `AgentDetail.tsx:669` | "truncated at 1000 characters" | worker-side truncation | not served |
| `types.ts:1384-1392` | `NEVER_WRITTEN` / `REAL_STATES` are themselves a TS restatement of `TaskState` | `swarm_common/states.py:17` | — |

**Rule for the executing wave:** a topic whose short form contains a number
reads that number from a route or from an imported constant. §B7.2 already
requires this ("It restates no frozen value"); this table is the list of places
that currently do not.

**Request, not a change (CLAUDE.md rule 1):** two of the above have no server
seam. `DEFAULT_STALE_AFTER` and the events page cap are both platform facts the
UI states as literals today. A small addition to an existing response —
`GET /v1/accounts` carrying `stale_after_seconds`, `GET /v1/tasks/{id}/events`
carrying the limit it applied — removes five literals and the
"a page that looks complete is not evidence that it is" caveat respectively.
That is recorded here as a request to Track A.

---

## 6. Actionable explanation — 21 rows that must not go behind a hover

§B7.1 draws its line between *fact* and *explanation*. There is a third kind,
and `Accounts.tsx` is full of it: explanation that is **a required action**.
Burying a definition behind a hover costs a reader a second; burying "reload the
pool before you sign in again" behind a hover costs them a duplicate
subscription.

**Rule: an actionable explanation may be summarised in the `?` card, but its
imperative clause stays on the surface, next to the control it constrains.**

21 rows in §3 carry the `ACTION` marker; they group into the 15 requirements
below. Fourteen of the 21 are in `Accounts.tsx`, which is why that file needs
a component change and not only a prose change (§8).

| Row | Action that must stay visible |
|---|---|
| `Accounts:1225` | reload this screen before pressing refresh again |
| `Accounts:1734` | do not sign in again until you have looked |
| `Accounts:1741`, `1787`, `1850`, `1926` | reload the pool and look for the label **before** starting another sign-in |
| `Accounts:2091` | a second account needs a second browser **application**, not a private window |
| `Accounts:2349` | paste all of the code, including the part after the `#` |
| `Accounts:2682` | signing in under an existing label **replaces** that credential |
| `Accounts:2802` | still REAUTH_REQUIRED — use "move to AVAILABLE"; nothing needs signing in again |
| `Accounts:2849` | not AVAILABLE, so nothing will be started on it |
| `Accounts:2976` | only a person can replace this; it is a sign-in |
| `Accounts:3073` | {n} agents hold this — move to DRAINING first |
| `Submit:248` | the task may exist — check Agents before submitting again |
| `SubmitWorkflow:359` | the workflow may exist — open the board before resubmitting |
| `AgentDetail:2006` | do not open a pull request from this branch |
| `AgentDetail:2025` | open the pull request by hand; re-running duplicates work |
| `AdminSettings:140` | lowering a ceiling evicts nothing; the pool drains |

---

## 7. The 94 `title=` attributes — a pre-existing failure of the same test

Not part of the 236, but they belong in the same wave, because they are already
the failure mode §B7.2 warns about: "hover alone is not enough — it is
unavailable on a touch device and invisible in a screenshot."

`Runtimes` 12, `Accounts` 13, `Capacity` 11, `Overview` 10, `Profiles` 8,
`Agents` 6, `Blockers` 5, `AdminSettings` 4, `PlatformCounts` 4, `Workflows` 4,
`AgentDetail` 4, `Activity` 3, `Dispatch` 2, and one each in
`App`, `AttemptTimeline`, `DataSources`, `Holders`, `Liveness`, `QuotaDetail`,
`Submit`, `SubmitWorkflow`.

`Accounts.tsx:509 readingTitle()` is the clearest case: five sentences that
carry the whole `never` / `absent` / `reset` / `stale` / `live` distinction,
reachable only by hovering a table cell with a mouse. Those five are
`EXPLANATION-MOVES` into `absent-vs-zero` and `projected-not-measured`; the
distinction itself already survives without them, because the cell draws **no
bar at all** when unmeasured (`Accounts.tsx:526`).

Converting a `title` to a `<HelpCard>` is a net improvement on every axis
(touch, screenshot, `aria-describedby`) and costs no surface area. It is
recorded here so the wave does not "finish" with 94 hover-only explanations
still in place and call the screens clean.

---

## 8. After this migration: what is left, and is it clean?

### The arithmetic

A location survives if any of its rows is `FACT-STAYS` or `KEEP-AS-IS`. It
disappears only if every row on it is `EXPLANATION-MOVES` or `DELETE`.

| | Locations |
|---|---:|
| Classified below | 363 |
| Disappear from the surface entirely | −108 |
| **Still rendered after the migration** | **255** |
| — of which carry a `FACT-STAYS` row (a sentence or a marker) | 189 |
| — of which are `KEEP-AS-IS` only (a value, a label, a control) | 66 |
| — of the 189, shortened rather than removed (split blocks) | 56 |

**That is a 30% reduction in rendered blocks, not a 50% one.** The screens that
actually empty out are `Capacity.tsx` (6 → 1), `Runtimes.tsx` (18 → 7),
`Profiles.tsx` (9 → 5) and `types.ts` (10 → 4) — the four whose prose is almost
entirely *teaching*. The screens that barely move are `Shell.tsx` (11 → 11),
`Holders.tsx` (6 → 6), `Workflows.tsx` (8 → 8) and `Agents.tsx` (3 → 3), because
what is on them is fact, and fact does not move.

The two biggest files come down but stay big: **`Accounts.tsx` 103 → 65** and
**`AgentDetail.tsx` 70 → 49**.

### Is 255 clean? No.

**Honestly: no, and the headline figure flatters it twice over.** 255 blocks
across 21 screens averages 12 per screen, and the average hides the shape:
`Accounts.tsx` alone keeps 65, which is a quarter of everything left on one
screen. And 56 of the 189 surviving facts survive as *shortened* sentences,
which is an improvement in words but not in block count — the reader still sees
a line of text in that spot.

What this table does deliver is the two clean kills: Accounts' four-part inlined
help essay (`3125–3285`, ~1,150 rendered words, all of it `EXPLANATION-MOVES`)
and the six `section.question` subtitles repeated on every pane of their
section. Neither annotates anything on screen. Both are pure Help-section
material, and removing them is uncontroversial.

What it does not deliver:

1. **`Accounts.tsx` keeps 65 blocks and stays the busiest screen in the
   product.** Most of them are the eleven sign-in outcome panels at
   `1734–2059`, and as *facts* they are irreducible: each distinguishes what the
   platform did from what this page can prove it did, and 14 of the 21 actionable
   rows in §6 are among them. What is reducible is their *shape* — eleven
   separate panels of full sentences. The follow-on is one outcome component with
   three fixed slots (*what happened* / *what is certain* / *what to do*), so
   eleven paragraphs become eleven three-line cards. That is a component change,
   not a prose change, and it is deliberately not in this table.

2. **189 surviving facts are still 189 pieces of text.** §B7.1 permits
   shortening a fact to a marker, and roughly 40 of them should become one:
   "the read succeeded and returned nothing — this is a real zero, not a failed
   query" is a sentence where a labelled `0` set against a distinct em-dash
   treatment would do the same work. But that is gated: without §B4.2's retuned
   state colours and §B8 item 4's actual `.panel` box, a marker has nothing to be
   legible against, and shortening the sentence first really would delete the
   honesty rule rather than move it. **Do not run the shortening pass before
   those two land.**

3. **The `?` count goes from 0 to ~108 topics over ~163 sites.** A `?` on every
   one of those sites is a different kind of clutter, and it is the failure mode
   this directive can produce on its own. §B7.2's rule — "inline, after the label
   it explains; never after a value" — only holds the number down if the wave
   puts **one `?` per panel heading**, not one per fact. That is roughly 20 on
   the busiest screens and one or two on the rest.

4. **The 94 `title=` attributes are untouched** by the counts above, and they
   are already hover-only. Finishing this migration without folding them in
   leaves 94 explanations that are invisible in a screenshot and unreachable on
   a touch device, on screens that are then called clean.

**What would have to happen for the answer to be yes:**

- the outcome-panel component (Accounts, Submit, SubmitWorkflow — ~25 locations);
- the shortening pass over ~40 `FACT-STAYS` sentences, **after** §B4.2 and §B8/4;
- the 94 `title=` attributes folded into the same topic ids (§7);
- §B8 item 20's four `DEV` badges, already agreed.

That path lands at roughly **150 rendered blocks across 21 screens**, with
`Accounts.tsx` under 30. That is clean. 255 is *better than 363*, and it is
what this table delivers on its own — no more than that.

---

## 9. Verification

| What | How | Result |
|---|---|---|
| `<p>` count | `grep -o '<p[ >]' apps/swarm-ui/src/*.tsx \| wc -l` | **236** |
| `muted` count | `grep -o 'muted' apps/swarm-ui/src/*.tsx \| wc -l` | **108** |
| `muted` on a `<p>` | `grep -o '<p className="muted[^"]*"' …` | **84**, so **24** are on other elements |
| legend blocks | `grep -n 'section legend' apps/swarm-ui/src/*.tsx` | **6**, in 6 files |
| legend entries | `awk '/section legend/,/<\/dl>/' <file> \| grep -c '<dt'` | 8 + 7 + 3 + 2 + 4 + 5 = **29** |
| `title=` count | `grep -o 'title=' apps/swarm-ui/src/*.tsx \| wc -l` | **94** |
| classification counts | `grep -c '\| <CLASS> \|' docs/web-ui/prose-migration-table.md` | 191 / 163 / 66 / 3 = **423 rows** |
| distinct locations | script over this file's own rows | **363**, of which **255** survive |
| **line references** | **30 rows sampled and opened at the cited line** | **30/30 correct** — 28 matched by text automatically, 2 confirmed by hand (`Dispatch.tsx:161`, whose text is the constant at `types.ts:600`; `Submit.tsx:279`, which points at a JSX comment) |
| parity gap | `grep -n 'swarm-ui\|typescript' scripts/lib/check-contract-parity.sh` | no match — **TypeScript is not covered** |
| `20 seconds` | `grep -n '20_000' apps/swarm-ui/src/Overview.tsx` | `:108` — same file as the prose at `:1446` |
| `30 minutes` | `grep -rn 'DEFAULT_STALE_AFTER' apps/quota-broker` | `accounts.py:92`; 5 literal restatements in `Accounts.tsx` (`518`, `738`, `828`, `2834`, `3303`) |
| resource weights | `sed -n '77,81p' apps/common/swarm_common/profiles.py` | `standard` 1, `browser` 2, `large` 4 — matches the prose, and `units` is on the wire |
| task states | `grep -n 'class TaskState' -A 14 apps/common/swarm_common/states.py` | 12 members; `REAL_STATES` is 9 — `PlatformCounts.tsx:187` types "nine" out |

**A correction this check produced.** The first spot-check pass failed 2 of 20.
Both were legend rows cited at the `<dd>` line rather than the `<dt>` line, which
turned out to be systematic: **23 legend row references were off by one to three
lines and have been renumbered** against `grep -n '<dt'`. The rows themselves
were right; the line numbers were taken from the wrong element. The 30-row
sample above is the re-run.

### Not verified

- **Nothing was rendered in a browser.** Every claim that a marker is "visible
  without hovering" is read off the JSX, the class names and the comments beside
  them — not off a screenshot. `Accounts.tsx:529` drawing no bar for an
  unmeasured cell, and `AgentDetail.tsx:1092` hatching the cpu track, are the two
  load-bearing examples and both are code-read only.
- **The "after" counts in §1 and §8 are arithmetic over this table**, not a count
  of a rendered screen. They assume every row is executed as classified.
- **The topic ids are proposed, not implemented.** `help.ts` does not exist yet;
  the parallel lane owns it. If it lands with a different naming scheme, this
  table's Topic column is what needs updating, not the classifications.
- **The `<=60-word` short forms are not written here.** §B7.2 caps a card at 60
  words; this table gives each `EXPLANATION-MOVES` row a topic and the source
  sentence to compress, not the finished card text.
- **`make lint` and `make test` do not read this file.** `check-doc-links.sh`
  globs `docs/*.md` non-recursively, so nothing under `docs/web-ui/` is link
  checked. This document is therefore unlinked from any checked index, and that
  is a known gap rather than an oversight.
