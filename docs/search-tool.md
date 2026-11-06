# The search tool: live-browser agents search through an API

**Status: built, switched OFF.** Lane SEARCH-TOOL, owner decision 2026-10-13
(~21:30 PDT), part of #1030. Nothing changes for any agent until an operator
stores the provider key and swarm-api runs with `SEARCH_TOOL_ENABLED=true`.

## 1. Why this exists

The live-browser analysis (the operator's `scratchpad/lb-analysis/ANALYSIS.md`)
found that Google answered **429 and redirected to `/sorry/` on 3 of 3 real
searches** typed into the live browser ([browser-egress.md §0](browser-egress.md#0-why-this-exists)).
Asked "proxy, search API, or both" ([§7, Q5](browser-egress.md#7-owner-questions)),
the owner answered **both**: the residential/ISP proxy for the pages an agent
opens, and a search API for finding them. A search API answers the commonest
case without typing into a search engine's page at all, so it neither trips
the engine's rate limit nor raises the terms-of-service question in
[§3.4](browser-egress.md#34-abuse-and-terms-of-service): the provider is paid
to answer automated queries.

So an agent on `claude-code-browser` gets a `search` tool. It returns titles,
URLs and snippets; the agent opens the pages it needs **in the live browser**,
where the person watching sees them. Its note says exactly that:
"To search the web, use the search tool, then open the results in the live
browser: do not type queries into a search engine's page."

## 2. The shape

```
agent ──search{query,count}──▶ swarm-search shim ──spool──▶ worker relay
      (MCP, in --mcp-config)    agent_worker.searchmcp       agent_worker.searchtools
                                 holds nothing               signs with the attempt key
                                                                    │
                                    POST /v1/attempts/{attempt_id}/search
                                                                    ▼
                                swarm-api  swarm_api.search: proof → fence+cap → key → provider
                                                                    │
                                                    Brave Search API (first provider)
```

* **The provider's key is swarm-api's alone.** It is a platform secret,
  `swarm-search-api-key`, read from Secret Manager on every call and never
  cached, never in an environment variable, never in a pod. The agent's
  container holds no key of any kind for this, the same way it holds no forge
  token for the git tools ([runner-git-tools.md §0](runner-git-tools.md)).
* **Authentication is the git tools', reused.** The worker signs each request
  with the attempt key it registered before the agent existed, and sends it
  with its ID token. swarm-api checks both (`ChildService.authenticate_worker`,
  `verify_proof`), then the fence in one transaction: the agent RUNNING under
  this attempt's live lease, no cancel requested. A stale attempt is 409
  `search_fenced` and the worker exits fenced without touching the lease
  (invariant 5). Every unproven request is the same 403 and writes nothing.
* **Only a live-browser profile is answered** (`RunnerProfile.live_browser`,
  read from the agent's own profile, never from the request). The tool is for
  an agent that opens results in the live browser; a headless agent would
  fetch the pages directly from the same NAT address that was blocked.
* **The caller names a query and a count (1-10, default 5).** Never a
  provider, a URL or a key (invariant 10); an extra field is a 422.

## 3. Off by default, and how it is switched

`SEARCH_TOOL_ENABLED` (swarm-api, default false) is a **feature flag**, not a
refusal switch (`swarm_api.refusals`): its off position is "there is no such
tool". Off:

* the route answers 404 before reading or writing anything;
* the child-key registration answers `search_tool: false`, so the worker
  creates no `SWARM_SEARCH` spool, claude-code names no `swarm-search` server,
  and the live-browser note says nothing about a search tool. An agent told to
  use a tool it does not have spends turns looking for it.

The worker learns the flag from swarm-api's registration answer rather than a
variable of its own, so there is one switch and it is in one place. Turned off
mid-attempt, the next search answers `search_unavailable` and the agent is told
to search in the live browser instead.

**To turn it on** (operator):

1. Create the provider account and API key (owner's choice of provider: §5).
2. `scripts/create-secrets.sh --search-api-key --stdin` — reads the key from
   stdin only, creates `swarm-search-api-key` if it is missing (replicated only
   to the region), adds the version and grants swarm-api, and nobody else,
   `secretAccessor` on it.
3. Run swarm-api with `SEARCH_TOOL_ENABLED=true` (and `SEARCH_PROVIDER`, if it
   is not `brave`). **Not done in this lane:** terraform does not render these
   variables yet (`terraform/infra/locals.tf`, swarm-api's environment); until
   it does, the deployed service keeps the default, off.

**How `swarm addon enable brave-search` knows it is on.** It asks swarm-api, as
the platform admin signed in with `swarm login`, to run one search: `POST
/v1/admin/setup/addons/brave-search/probe` (`swarm_api.routes.addon_probe`).
swarm-api reads `swarm-search-api-key` with its own identity and sends one
query of one result to the provider, exactly the path an agent's search takes,
and answers `{ok, reason, status, results}`: `ok: false` with a reason when the
running revision has the flag off (nothing is read), when swarm-api cannot read
the key (its accessor grant is named), or when the provider refuses it (the
provider's error kind and HTTP status from §4). The add-on is not reported on
until the answer is `ok: true`. A swarm-api older than the route answers 404;
only then does the check fall back to the key's IAM policy for swarm-api's read
grant, and it says so. An IAM-policy read was the only check before; it was
replaced (owner pick, 2026-10-31) because a binding says nothing about whether
Brave accepts the key or whether the flag reached the revision. The probe
spends no tenant's daily cap and writes no audit row (it is no tenant's
search), and its answer never carries the key, a result or the provider's body.
It is a platform admin's route only: a pool admin, a workspace admin, a member,
the deployer and the doctor are refused.

## 4. Limits, errors and the audit

| what | value | why |
|---|---|---|
| queries per tenant per UTC day | 200 (`SEARCH_DAILY_QUERIES_PER_TENANT`) | At Brave's $5 per 1,000 a tenant costs at most $1 a day. Counted **before** the provider is called, so a failed call spends one too: the cap bounds what the platform can be billed, and failures can be billed. Over it: 429 `search_daily_cap`. |
| searches per attempt per hour | 60 (worker) | Spares a looping agent's round trips; the tenant cap is the one that counts. |
| results per call | 1-10 | The owner's number. |
| query | 1-400 characters, one line | A line a person would type. |
| provider timeout | 10 s | One slow provider must not hold a request thread. |

Provider failures are mapped to codes the agent can act on, never 409 (which
the worker reads as the fence): a refused key is 502 `search_provider_refused`,
the provider's rate limit 429 `search_provider_busy` (retryable), a rejected
query 422 `search_query_rejected`, no answer 503 `search_provider_unavailable`
(retryable), an unreadable answer 502 `search_provider_failed`; no key stored
is 503 `search_unconfigured`. Messages are built from the status and the code,
never from the provider's body (which can echo the request, and the request
carried the key), and are scrubbed of the key anyway.

Every proven call writes one row in `search_audit`: the attempt's tuple, the
query, the count, the provider, how many results, the outcome and its code. It
records the **query**, because an audit of searches that does not say what was
searched answers nothing; it never records the key, a result's text or a URL.
A tenant member reads an agent's rows at `GET /v1/tasks/{task_id}/search-calls`,
under the agent's own scope. The worker's own log names request ids and result
counts, never the query.

## 5. The provider (owner question)

The provider sits behind `SearchProvider` (`swarm_api.search`); Brave Search
API is the first implementation and the default. The choice is the owner's,
asked in this lane's `questions.json`. Prices read from the vendors' pages on
2026-10-14:

| provider | per 1,000 queries | notes |
|---|---|---|
| **Brave Search API** (built) | **$5**, $5 free credit a month | Own index. 50 queries/s. Storing results needs a plan that grants storage rights; this tool stores none, only the query in the audit. |
| Google Programmable Search (Custom Search JSON API) | $5, 100 free a day, 10k/day cap | **Closed to new customers; existing customers must move by 2027-01-01.** Not available to us. |
| SerpAPI | $25 (1k/month plan) → $10 (15k) → $3.75 (1M) | Scrapes Google's results page for you: Google results without our IP, at a higher price and with the scraping question moved to the vendor. |
| Tavily (a Bing-alternative) | $8 pay-as-you-go ($0.008 a credit, 1 credit a basic search), 1,000 free a month | Built for agents; returns cleaned content. Bing's own Search API was retired in August 2025. |

A second provider is one class implementing `search(query, count, key=)` and
one entry in `PROVIDERS`; `SEARCH_PROVIDER` picks it and an unknown name is
refused at start.

## 6. The invariants

Nothing here creates infrastructure demand (1) or touches capacity (2, 3).
Every call carries the fencing generation and a stale attempt searches nothing
(5). The audit rows and the daily count are the tenant's own (9). The caller
names a query, never a provider, URL, key or backend parameter (10).

## 7. Not verified

* **A real call to Brave.** The provider is exercised against a fake
  transport shaped from Brave's documentation (endpoint, `X-Subscription-Token`,
  `web.results[].title/url/description`); no key exists yet.
* **That agents prefer the tool to typing into Google.** The note tells them
  to; the measurement is browser-egress.md §6.2's block rate, before and after.
