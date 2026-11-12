# A generic SwarmCloud repository, and the cutover to a fresh one

**Status:** design, for the owner's approval. Part of the Day One install program. Nothing here has been applied.
**Written:** 2026-11-07, against `main` at `cbfb443`.
**Decision it serves:** the owner's, 7 November 2026: the SwarmCloud repository holds **nothing specific to a company, a person or a live deployment**, so it can be public and reused by anyone.

**The owner's picks, which this design follows and does not reopen:**

1. Lanes clean the files first. Then the current repository is **renamed to a private archive name**. It keeps every issue, pull request, branch and commit. A **fresh repository is created at the same name and address**, holding one clean commit. The open issues worth keeping are rewritten generically and carried over. Every integration works again on the new repository. Rewriting history in place was rejected: the old issues and pull requests keep their text, and every closed pull request keeps its commits reachable.
2. The live deployment's real values move to GitHub repository and environment variables and to Secret Manager. Tracked tfvars become examples. The real files are fetched from a private bucket at plan and release time.
3. All text is generic: roles instead of people (the owner, a member), `example.com` addresses, `example-project`, "a peer application" instead of product names.
4. The repository goes public again once it is clean.

**What this document is not allowed to hold.** It names no real identifier. Every value below is a **category** or a **placeholder** from section 3. A reader who needs the real value reads it from configuration (section 2), never from here.

**What was verified and what was not.** File paths and line numbers were read at `cbfb443`. The counts in section 1.3 were taken with `git grep -l` at that commit, using patterns built from configuration at run time; no value was written down. Nothing was run against GitHub's API, a Google Cloud project or the runner dispatcher. Facts that only the live objects can settle are marked **to be read at cutover**, and the runbook (section 6) reads each one before it relies on it.

---

## 0. Why a fresh repository, in one paragraph

A clean tip is not a clean repository. Every old commit, every pull request's diff, every issue body and its edit history carries the values. Rewriting history in place would break the 2,963 closed pull requests' links to their commits and leave the issues' text as it is. So the old repository becomes a private archive, unchanged, and a new repository at the same address starts from one commit that the guard (section 4) has proven clean. A fresh repository has a **new numeric id**. Anything GitHub stores per repository does not carry over: variables, secrets, environments, rulesets, labels, webhooks, App installation selections, Actions history. Section 6 enumerates each of these and rebuilds it.

---

## 1. Finding every deployment-specific value without committing the values

### 1.1 Two layers

A value can be found by its **shape** or by its **name**.

| Layer | Finds | Where its rules live | Needs a secret |
|---|---|---|---|
| **Shape** | Values that look deployment-specific whatever they are: a service-account address, an OAuth client id, a project number, an email address, a tracked `*.tfvars`, a `gs://` bucket | In the repository, as regular expressions plus the placeholder allowlist of section 3 | No |
| **Name** | Values with no shape: the company's name and domains, people's names and handles, the live project id, the shared project's other-team cluster and network, hostnames, the tailnet, peer products | In the **pattern list**: a repository secret and a Secret Manager entry (section 1.4) | Yes |

The shape layer holds rules, not values, so it can be public. The name layer holds only values, so it lives outside the tree.

### 1.2 Categories, and how each one is found

| # | Category | Shape rule (tracked) | Name entries (pattern list) |
|---|---|---|---|
| C1 | Google Cloud project ids | `projects/<id>`, `--project[= ]<id>`, `<id>.iam.gserviceaccount.com`, `<id>.svc.id.goog`, any id outside the placeholder set in those positions | The live project id; the shared project id; any other project the deployment touches |
| C2 | Project numbers | 10-13 digit numbers after `projects/`, before `-compute@`, in `serviceAccount:service-<n>@`, in OAuth client ids | The live numbers |
| C3 | Service-account addresses | `[a-z0-9-]+@[a-z0-9-]+\.iam\.gserviceaccount\.com` whose project part is not a placeholder | None needed: the shape covers every one |
| C4 | Company domains and hostnames | Any email domain or host outside `example.com`, `example.org`, `example.net`, the reserved `.test`/`.invalid`/`.localhost`, and the public product hosts listed in the allowlist (Google, GitHub, package registries) | The company's domains, the live hostname, the tailnet name |
| C5 | People | Email addresses at any non-placeholder domain | Names, handles and initials of people, including the owner's |
| C6 | OAuth and App identifiers | `\d+-[a-z0-9]{32}\.apps\.googleusercontent\.com`; a GitHub App id or installation id next to `app_id`/`installation_id` that is not a placeholder | None needed |
| C7 | Buckets, secrets, registries | `gs://` and bucket fields whose name embeds a non-placeholder project; secret names with a tenant part outside the placeholder set | The live tenant slugs |
| C8 | The shared project's other-team resources | None: names of a cluster or network have no shape | The other team's cluster, network, subnets, buckets, registries and accounts (the same entries as the deny list, section 2.4) |
| C9 | Company and peer product names | None | The company's name in every spelling; peer products' names |
| C10 | Tracked deployment files | Any tracked `*.tfvars` that is not `*.tfvars.example` and not under `tests/terraform/` fixtures; `.env` files; Terraform state; `*.sarif`, `.coverage` | None |
| C11 | Old issue and pull request numbers | A `#N` or `/issues/N` or `/pull/N` reference to the archive (lanes 9 and 10; question Q5) | None |

C3 and C6 need no pattern entries at all, because their shape is the finding. C8 and C9 are the categories that only the secret list can find. C11 is not a secret, but after the cutover an old number points at a different issue in the new repository.

### 1.3 How large the cleanup is (counts only, taken 2026-11-07)

Tracked files that hold at least one value of the category, out of 5,097:

| Category | Files | Where most of them are |
|---|---|---|
| Live project id (C1) | 389 | tests 253, docs 59, terraform 35, scripts 16, apps 12 |
| Project number (C2) | 62 | tests 45, docs 8 |
| Live-project service accounts (C3) | 137 | tests 105, docs 20 |
| Company domain, any use (C4) | 136 | docs 40, tests 35, kubernetes 24 (label keys, section 3.3) |
| Personal emails (C5) | 58 | docs 22, tests 14 |
| Real-shaped OAuth client ids (C6) | 16 | tests 11 |
| Tailnet (C4) | 22 | tests 12 |
| The owner's handle (C5) | 376 | apps/swarm-ui 120, tests 117, docs 78, `.github` 14 |
| Company name, any case (C9) | 544 | tests 303, docs 87, terraform 41 |

Three files under `apps/common/swarm_common/` hold values too. That directory is frozen, so its cleanup is a contract request (section 5.2).

### 1.4 The pattern list: format, home and reading

**Format.** One entry per line: `<category>\t<kind>\t<value>`. `<kind>` is `literal` (case-insensitive, word-bounded) or `regex`. Lines starting with `#` are comments. A literal is matched also with `-`, `_` and `.` swapped, so `example-project`, `example_project` and `example.project` are one entry.

**Home.** There are two stores, written from one source:

| Store | Name | Read by |
|---|---|---|
| Private config object (the **source**) | `gs://<state-bucket>/config/deployment-patterns.tsv` | The sync script and an operator who edits it |
| GitHub repository secret | `DEPLOYMENT_PATTERNS` | The CI guard job |
| Secret Manager entry | `swarm-deployment-patterns` | The worker's pre-publish gate, so a SwarmCloud agent's own branch is checked before it is pushed |

`scripts/guards/sync-patterns.sh` (lane 1) reads the source object, writes the secret with `gh secret set DEPLOYMENT_PATTERNS < file` and adds a Secret Manager version. `swarm addon enable ci` creates the Secret Manager entry itself (the container, with no version), because the gate is part of SwarmCloud's own CI machinery and the CI add-on is what turns that machinery on; the sync script still creates it on a project where the add-on has not run. Why the add-on stops at the container: the list's values come only from the owner's source, so the engine never writes, reads or prints a version, and an empty entry fails the gate closed exactly as an absent one does. Both copies carry a first line `# sha256 <digest of the rest>`. The guard prints that digest on every run, so two stores that disagree are visible without printing a value. Why not one store: pull-request CI jobs get no Google credentials (the CI identity's condition admits `refs/heads/main` only, `terraform/bootstrap/wif.tf:196`, which keeps out `pull_request` runs; a `pull_request_target` run is a different case, section 4.3 item 2), so CI cannot read Secret Manager on a pull request. The worker has no GitHub secret. One source with a digest keeps them equal.

**Rejected: hashed tokens in the tree.** Committing sha256 digests of the forbidden words would let a fork run the guard without a secret. It was rejected because the name layer's values are short words: a dictionary attack on a digest of a company name or a person's handle recovers it in seconds. A salted digest needs the salt kept secret, which brings back the secret.

### 1.5 Building the first list

The pattern list is built once, before lane 1 merges, by the owner (section 6, step P2). It needs no repository scan to start: every C1, C2, C4 and C8 value is already in configuration the owner controls.

1. **From the deployment's own configuration:** the live and shared project ids and numbers (`gcloud projects describe`), the hostname, the IAP members' domains, the tenant slugs, the deny list's names (section 2.4).
2. **From GitHub:** the owner's handle, collaborators' handles, the App slugs (`gh api repos/<owner>/<repo>/collaborators`, `gh api user/installations`).
3. **From the people:** the company's name in every spelling, peer products, people's names and initials.
4. **Sweep for what was missed:** `scripts/guards/candidates.sh` (lane 1) lists, from the tree, every token that the shape layer allowlists only because it is unknown: every email domain, every non-placeholder host, every capitalised word that appears in prose more than five times and is not an English word. It writes the candidates to `$TMPDIR`, never to the tree or a log, for the owner to sort into the list or into the allowlist.

---

## 2. The plumbing: where real values live and how each reader gets them

### 2.1 The three homes

| Home | Holds | Why there |
|---|---|---|
| **GitHub variables** (repository, and per environment for `dev` and `prod`) | Non-secret identifiers CI needs before it has cloud credentials: project id, region, workload identity provider, deployer and other CI account addresses, state bucket, API host, runner label, merge-train knobs | They exist before authentication, they are per environment, and workflows already read them (`vars.*`) |
| **GitHub secrets** | `DEPLOYMENT_PATTERNS`, the merge App key, the sandbox token | Read by CI only, never printed |
| **The private config prefix**, `gs://<state-bucket>/config/<env>/` | The real `<env>.tfvars`, the real `bootstrap.tfvars`, the deny list `deny-list.json`, the operator `.env`, the pattern source | Large files that Terraform reads whole; already private, versioned and readable by the deployer; the Day One installer already keeps its generated inputs in the same bucket (`swarm-install/inputs/`, `apps/swarm-install/swarm_install/tfvars.py:102`) |
| **Secret Manager** | Credentials (unchanged), plus `swarm-deployment-patterns` | Unchanged home for secret material; the worker reads it |

**Why the state bucket and not a new bucket:** the state bucket is the one bucket that must exist before anything else, is already private and versioned, and already holds the installer's inputs. A second bucket would be one more name to configure and one more grant to get wrong. The `config/` prefix is separate from `infra/` and `bootstrap/` state, so state locking never touches it.

### 2.2 Which settings move where

| Setting today | Where it is | Where it goes |
|---|---|---|
| Project id default | `scripts/lib/common.sh:158`, `.env.example:29` | No default. `.env` (local) or `vars.GCP_PROJECT_ID` (CI). Unset is an error that names the variable |
| Region and zone defaults | `common.sh:159-160` | Same as the project. The installer keeps its own `DEFAULT_REGION` as an offered choice (`steps/preflight.py`), which is a prompt default, not a deployment value |
| Bucket names | Derived from the project (`common.sh:174-191`) | Stay derived; they then hold no literal |
| `terraform/bootstrap/terraform.tfvars`, `terraform/environments/{dev,prod}/*.tfvars` | Tracked | Untracked and git-ignored. The real files go to `config/<env>/`. Tracked `*.tfvars.example` with placeholders replace them |
| `terraform/bootstrap/terraform.tfvars.example` | Tracked, holds live values | Rewritten with placeholders |
| `SHARED_DENY_LIST` | Literal array, `common.sh:260-291` | `config/<env>/deny-list.json` (section 2.4) |
| Destroy-guard floors | `scripts/lib/destroy-guard-proof-cases.json` (`inventory_floor`) | Into `deny-list.json` as `floors`; the tracked proof cases use placeholders |
| Python deny-list twin | `apps/swarm-api/swarm_api/issueruns.py:1331-1388` (a literal shared project) | A Terraform-rendered environment pair on swarm-api, from the same tfvars (section 2.4) |
| A hard-coded repository name | `.github/workflows/merge-train.yml:135` | `github.repository` (lane 12's patch) |
| Live values in variable descriptions, validations and `removed`/`moved` addresses | `terraform/bootstrap/variables.tf`, `terraform/infra/variables.tf`, `removed.tf`, `custom_roles_moved_to_bootstrap.tf` | Descriptions use placeholders. `removed`/`moved` blocks whose old address embeds a tenant slug are deleted once `terraform state list` shows no such address (lane 4 reads it first). Both `removed.tf` files were deleted on 2026-11-11, when neither state held any of their addresses |

### 2.3 How each reader gets them

**`scripts/lib/common.sh`.** `load_env` keeps sourcing `${SWARM_ENV_FILE:-.env}` with its ownership checks. A new function, `config_fetch <env>`, copies `gs://${TF_STATE_BUCKET}/config/<env>/` into `${XDG_CACHE_HOME:-$HOME/.cache}/swarm/<project>/<env>/`, keyed by each object's generation, and checks every file's owner and mode the way `load_env` does. `tf_var_file()` returns the cached `<env>.tfvars`. If the object is missing, it stops and names the object. It never falls back to a tracked file. The cache sits outside the clone, so nothing can stage it.

**The Makefile.** It already resolves `PROJECT_ID`, `REGION` and `ENVIRONMENT` through `scripts/lib/resolve.sh`, which sources `common.sh`. `VAR_FILE` becomes `$(shell scripts/lib/resolve.sh tfvars $(ENVIRONMENT))`, which runs `config_fetch` and prints the cached path. The comment holding a live value at `Makefile:53` becomes a placeholder.

**`terraform/infra` and `terraform/bootstrap`.** Unchanged in shape: one root each, `-var-file` from `tf_var_file`, backend bucket by `-backend-config`. Both roots already have no hard-coded backend bucket (`backend.tf`). Lane 4 adds the git-ignore lines and the examples, and changes no resource.

**The Day One installer** (`apps/swarm-install`). It already reads no committed tfvars and writes its generated inputs to the state bucket (`tfvars.py:1-12`, `:655`). Two additions, both in lane 5:
1. It writes `config/<env>/deny-list.json` in stage 1. For a project it created, the list is **explicitly empty** (`"entries": []`). For an existing project the person picked, it lists what preflight found that the deployment does not own, and asks the person to approve the list (an approval prompt, which the Day One rules allow).
2. It reads the deny list from that object into `state.deny_list`, replacing today's comment "never read from an environment variable" with "read from the config object, never from an environment variable".

**Every workflow.** Each job that plans or applies gets one step after `google-github-actions/auth`: `scripts/lib/resolve.sh tfvars "$ENVIRONMENT"`, so it uses the same fetch as a laptop. The guard job reads `secrets.DEPLOYMENT_PATTERNS`. No job writes a fetched file to an artifact or a log. This is lane 12's operator-pushed patch, because no lane edits `.github/workflows/`.

**Release time.** `publish-release.yml` does not need the tfvars. It needs only what it reads today (deployer account, workload identity provider). The public distribution addresses (the install script's bucket and the plugin's marketplace) are product addresses, not deployment values (question Q9).

### 2.4 The never-touch list: from configuration, failing closed

Today `SHARED_DENY_LIST` is a literal array of the shared project's other-team resources: its cluster, its network and subnets, its buckets and registries, its service accounts. About twenty call sites check it (`kube_context_allowed`, the plan guard, `destroy.sh`, `bin/kubectl` and others; `common.sh:293-350`). The plan R4 kept the literal so that "an environment variable an operator forgets would silently empty the live guard" (`docs/design/day-one-plan.md`, section 0). This design moves the values out **and keeps that property**: a missing list refuses everything; it never allows anything.

**The object.** `gs://<state-bucket>/config/<env>/deny-list.json`:

```json
{
  "project": "example-project",
  "entries": ["peer-cluster", "peer-vpc", "peer-subnet", "peer-bucket", "peer-runner@example-shared-project.iam.gserviceaccount.com"],
  "floors": {"cluster": 1, "network": 2, "bucket": 1, "service_account": 1},
  "reviewed_by": "owner@example.com",
  "reviewed_at": "2026-11-07"
}
```

**The rules, in `common.sh` (lane 2):**

1. `deny_list_load` fetches the object through `config_fetch` and parses it with `jq`.
2. **Missing, unreadable or malformed object: refuse.** Every guarded action (`is_shared_resource`, `kube_context_allowed`, `guard_deny_json`) stops with "the never-touch list for `<project>` is not readable; nothing that mutates runs without it". Nothing is treated as empty.
3. **`project` differs from `PROJECT_ID`: refuse.** A list copied from another deployment does not protect this one.
4. **Fewer entries of a kind than its floor: refuse.** A truncated list fails the way `scripts/verify-destroy-guard.sh` fails today.
5. **Empty is allowed only when it is written down:** `"entries": []` with `floors` all zero. That is a statement that this project shares nothing, reviewed by someone, not the absence of a statement.
6. `guard_deny_json` emits `[]` plus `"(default)"` for an empty list, never `[""]` (today's `printf '%s\n'` with no arguments prints one empty line, which would become an empty-string entry).
7. Read-only commands (`plan`, `status`, `doctor`) also load the list, so a broken list is found before an apply needs it.

**swarm-api's twin.** Terraform renders two environment variables on swarm-api from the same tfvars: `SWARM_SHARED_DENY_LIST` (the JSON array) and `SWARM_SHARED_DENY_LIST_SET=1`. `issueruns.shared_deny_list()` refuses any infrastructure-touching issue run when `SWARM_SHARED_DENY_LIST_SET` is absent. That is again a refusal, never an empty list.

**Why this holds the live deployment.** The live list moves into the live project's config object in the same change that removes the literal (lane 2), and lane 2's red-first test proves rule 2: with no object, `kube_context_allowed` refuses a context that names the shared cluster. CLAUDE.md's rule that the list lives once still holds: it lives once, in the object, and `common.sh` holds the one reader.

### 2.5 The repository's own address

Some files must name the repository itself: the plugin pin (`plugin/.claude-plugin/plugin.json`), the marketplace (`.claude-plugin/marketplace.json`), the README's install line, the issue forms' links. Under pick 1 the fresh repository keeps the same address, so that address stays a **product address**. The guard allows it only in those files, and reads it from `GITHUB_REPOSITORY` (CI) or `git remote get-url origin` (locally), so the allowlist holds no literal (question Q7).

---

## 3. One placeholder set for tests and docs

### 3.1 The set

Lane 1 commits these as `scripts/guards/placeholders.json`. The guard allowlists exactly these values, tests import them (`tests/placeholders.py` reads the JSON) and docs use them verbatim.

| Category | Placeholder |
|---|---|
| Project id (the deployment) | `example-project` |
| Project id (the shared project) | `example-shared-project` |
| Project number | `123456789012` (the shared project: `210987654321`) |
| Region and zone | `us-central1`, `us-central1-a` (public Google names; kept, they identify nothing) |
| Organisation domain | `example.com`; a second, outside domain: `example.org` |
| Hostnames | `swarm.example.com` (console), `api.swarm.example.com` |
| People | `owner@example.com` (the owner), `admin@example.com` (an admin), `member@example.com` (a member), `outsider@example.org` (not a member) |
| Groups | `swarm-admins@example.com`, `team-a@example.com` |
| Service accounts | `<role>@example-project.iam.gserviceaccount.com`, e.g. `swarm-api@example-project.iam.gserviceaccount.com` |
| Tenants | `example-tenant`, `team-a` |
| Buckets | `swarm-tfstate-example-project`, `swarm-artifacts-example-project` (the real naming scheme, with the placeholder project) |
| Secret names | `swarm-tenant-example-tenant-git`, and the platform's fixed slot names as they are |
| OAuth client id | `123456789012-example.apps.googleusercontent.com` |
| GitHub | owner `example-org`, repository `example-org/example-repo`, App id `123456`, installation id `12345678`, repository id `100000001` |
| Shared project's other-team resources | `peer-cluster`, `peer-vpc`, `peer-subnet`, `peer-bucket`, `peer-runner@example-shared-project.iam.gserviceaccount.com` |
| Products | "a peer application" |
| Tailnet | `example-tailnet.ts.net` |
| The installer's address | `203.0.113.7/32` (RFC 5737 documentation range) |

**Rules for using it.** A test builds every credential-shaped value at run time (`secrets.token_hex`), as CLAUDE.md requires. The placeholders above are identifiers, not credentials. A test that needs two different projects uses the two project placeholders, never a made-up third. A doc that quotes an owner's words replaces the name with the role, in brackets: "[the owner]".

### 3.2 Why fixed values and not random ones

Random values would make every snapshot and every doc example change between runs. Fixed values in one file let the guard allowlist them exactly and let a reviewer see at a glance that a value is a placeholder.

### 3.3 The Kubernetes label-key prefix

The label and annotation keys embed the company's domain as their prefix (`kubernetes/render.py`, the admission policies, `tenant-namespace.yaml`). The plan R5 kept them, because renaming a key breaks every live object and policy that matches it. This design makes the prefix **configuration**: `label_prefix`, which defaults to `swarm.<org_domain>`. The live deployment's `org_domain` gives exactly today's keys, so no live object changes. A fresh deployment gets keys under its own domain. The tracked code and tests use `swarm.example.com/`. This is lane 5's work, and question Q4 asks the owner to confirm it.

Where the prefix is read (owner, 12 November 2026, "Tfvars everywhere"): the deploy (`kubernetes/apply.sh`, every mode), a personal workspace's job (`scripts/register-tenant.sh`), the tenant network policy sweep (`scripts/apply-tenant-network-policies.sh`) and the cluster network parity check (`scripts/lib/check-cluster-network-parity.sh`) each resolve it once, through the shared helper `require_label_prefix` in `scripts/lib/common.sh`, and read it from the deployment's own `<env>.tfvars`, `label_prefix`, else `swarm.<first allowed_domains entry>`, the same rule group and service workspaces already follow. A configured `SWARM_LABEL_PREFIX` or `SWARM_ORG_DOMAIN` that differs is refused before anything is applied, with one fixed sentence naming neither value, because two answers for one key are how a namespace ends up under a key the live policies do not match. Only when the tfvars cannot be read does the configured prefix apply; with neither, the run refuses. No repository variable is needed: the deploy reads the prefix from the tfvars it already plans from (`scripts/lib/common.sh` `resolved_label_prefix`, which `require_label_prefix` turns into a refusal). The full list of callers, and of the scripts that need none with why, is held by the guard test `test_every_script_that_renders_the_token_resolves_the_prefix_through_the_shared_helper` in `tests/unit/scripts/test_label_prefix.py`.

---

## 4. The guard

### 4.1 What it is

`scripts/guards/deployment-values.sh`, a thin wrapper around `scripts/guards/deployment_values.py` (lane 1). Inputs:

- `--tree` (every tracked file at `HEAD`), `--diff <base>` (only added lines) or `--text <file>` (for issue and pull request text at cutover);
- the pattern list from `DEPLOYMENT_PATTERNS` (CI) or the Secret Manager entry (the worker);
- the shape rules and `placeholders.json` from the tree.

Output, one line per finding: `path:line C<n> <rule-id>`. The rule id for a name entry is its line number in the pattern list, never its value. The matched text is never printed. That is why a log, a CI annotation or a pull request comment from the guard leaks nothing.

### 4.2 Failing closed

| Situation | Result |
|---|---|
| `DEPLOYMENT_PATTERNS` unset or empty in CI | Exit 2: "pattern list unavailable". The shape layer still runs and reports, but the job is red |
| The list's first line digest does not match its content | Exit 2 |
| The worker's gate cannot read `swarm-deployment-patterns` | The gate fails and names the slot |
| Run locally with no list | The shape layer runs. The name layer prints "skipped: no pattern list", and the exit is 3, so no script can mistake it for a pass |

### 4.3 Where it runs

1. **On every pull request and push to `main`**, as a required check named `deployment-values`. Lane 12's patch adds the job to `security.yml`. It reads only the diff text and never runs pull-request code.
2. **On fork pull requests, once public.** Forks get no secrets under `pull_request`. The job runs under `pull_request_target`, checks out the head as data only (`persist-credentials: false`, no script from the head is executed: the guard comes from the base). The ref clause of the CI identity's condition does **not** protect this event: a `pull_request_target` run carries the base branch's ref (`refs/heads/main`) in its OIDC token, so `assertion.repository` and `assertion.ref` both match. The job is therefore built so it cannot ask for cloud credentials at all:
   - it declares `permissions: { contents: read }` and nothing else, so it gets no `id-token: write` and cannot mint an OIDC token;
   - it runs no `google-github-actions/auth` step and no other step that exchanges a token for cloud credentials;
   - it lives in its own workflow file, which is never added to `deployer_workflows` in `terraform/bootstrap/wif.tf`, so the per-workflow `job_workflow_ref` binding refuses it even if a later edit grants `id-token: write`;
   - `DEPLOYMENT_PATTERNS` is the only secret in its scope.

   On a fork's run the guard prints only a pass or fail and the number of findings, never `path:line` or a rule id, so a contributor's dictionary of guesses cannot be confirmed one string at a time. The maintainer reads the per-line report from a same-repository rerun or the worker's gate. Section 7.1 and step V6 check all of this before visibility is flipped.
3. **In the worker's pre-publish gate**, through `scripts/changed-guards.sh` (lane 1 adds it there), so an agent's branch fails before it is pushed.
4. **Over all tracked files and over the new repository's issue text**, in the cutover runbook (steps C3 and C9).

### 4.4 Report mode, then enforcing

Lane 1 ships the guard in **report mode**: it prints the count per directory and exits 0. That lets lanes 2 to 11 merge while the tree still holds old values. Lane 12 flips it to enforcing once `--tree` reports zero. Report mode prints counts only, never `path:line`, so a public log during report mode cannot be used as a map.

### 4.5 Tests (lane 1, red first)

- A planted value from each category in a temporary tree is found, by rule id, without the value appearing in the output.
- Every placeholder in `placeholders.json` passes.
- An unset list is exit 2 under `CI=true` and exit 3 locally.
- A digest mismatch is exit 2.
- The existing name guards (`tests/unit/scripts/test_no_live_literals.py`, `apps/swarm-ui/test/no-company-name.test.mjs`, `apps/swarm-ui/src/__tests__/noLiveRepository.test.ts`) hold their patterns as literals, built in pieces. Each becomes a reader of the pattern list. Lane 1 converts the Python one; lane 8 converts the two UI ones. Their literals go, because a guard that spells the value is itself a finding.

---

## 5. The cleanup: twelve lanes, disjoint by directory, in order

### 5.1 The lanes

Each lane replaces values with the placeholders of section 3, or moves them to configuration (section 2), and leaves the guard's count for its directories at zero. "Owns" is the whole territory; nothing outside it is edited. A test file belongs to the lane that owns the code it tests only when the table names it. Otherwise it belongs to the test lanes.

| Order | Lane | Owns | What it does | Red first |
|---|---|---|---|---|
| 1 | GUARD | `scripts/guards/` (new), `tests/unit/guards/` (new), `tests/placeholders.py` (new), `tests/unit/scripts/test_no_live_literals.py`, `install/manifest.d/generic-guard.yaml` (new) | The guard in report mode, `placeholders.json`, `sync-patterns.sh`, `candidates.sh`; adds the guard to `changed-guards.sh`'s list through a one-line patch in its PR body (that file is lane 2's), plus the workflow patch for the report-mode job | Section 4.5 |
| 2 | CONFIG-CORE | `scripts/lib/`, `Makefile`, `bin/`, `.env.example`, `.gitignore`, `tests/unit/scripts/test_shared_deny_list.py`, `tests/unit/scripts/test_common_*.py` | `config_fetch`, no defaults, the deny list from the object (section 2.4), floors into the object, the live list written to the live config object by the owner (step P3) | No object: refuse; wrong project: refuse; below floor: refuse; written empty: allow |
| 3 | SCRIPTS | `scripts/` except `scripts/lib/` and `scripts/guards/`; `tests/unit/scripts/` except lanes 1 and 2's named files | Placeholders in scripts, acceptance config and their tests | The guard's report for these directories goes from N to 0 |
| 4 | TERRAFORM | `terraform/`, `tests/terraform/` | Untrack the tfvars, add `*.tfvars.example`, placeholders in descriptions and validations, drop applied `removed`/`moved` blocks that embed a tenant slug, add the repository-id condition (section 6, step C5) | `terraform validate` on each example; a test that no tracked `*.tfvars` exists outside fixtures |
| 5 | KUBE-INSTALL | `kubernetes/`, `images/`, `install/` (except lane 1's fragment), `apps/swarm-install/`, `tests/unit/install/` | The configurable label prefix (section 3.3), read from the deployment's tfvars with a differing configured prefix refused, no project defaults in `render.py` and `apply.sh`, the installer writes and reads `deny-list.json` (section 2.3) | A render with `org_domain=example.com` gives `swarm.example.com/` keys; a fresh project's install writes an empty, reviewed list |
| 6 | APPS-BACKEND | `apps/` except `apps/common/`, `apps/swarm-ui/`, `apps/swarm-install/`; `tests/integration/` | swarm-api's deny-list twin from the rendered environment (section 2.4), placeholders in code and comments | No `SWARM_SHARED_DENY_LIST_SET`: an infrastructure issue run is refused |
| 7 | TESTS-UNIT | `tests/unit/` except `scripts/`, `install/`, `guards/` and lane 2's named files; `tests/acceptance/`, `tests/browser/`, `tests/fixtures/`, `tests/smoke/` and the other files directly under `tests/` except lane 1's `tests/placeholders.py` | Mechanical replacement with placeholders | The guard's count for these directories goes to 0 |
| 8 | UI | `apps/swarm-ui/` | Placeholders, the owner's handle out of fixtures, the two name guards read the pattern list | The UI name guards fail on a planted listed value |
| 9 | DOCS-DESIGN | `docs/design/`, `docs/audits/`, `docs/web-ui/` | Roles for people, placeholders, old `#N` references to plain names (question Q5) | Count to 0 |
| 10 | DOCS-REST | `docs/` except lane 9's directories | Runbooks look rulesets up by name, not by numeric id; placeholders | Count to 0 |
| 11 | ROOT-META | Every root file not named by another lane (`README.md`, `CLAUDE.md`, `LICENSE`, `docker-compose.yml`, `pyproject.toml`, `uv.lock`, `.python-version`, `.trivyignore.yaml`, `.dockerignore`, `.gcloudignore`, `.claude/`, `.mcp.json`, `.claude-plugin/`; `CONTRACT.md` excepted, below), `.github/` except `workflows/` and `actions/`, `plugin/`, `benchmarks/`, `scratchpad/` | Generic CLAUDE.md (the shared-project rule names categories, and the names come from the deny list), the `LICENSE` holder (question Q6), the issue forms' links from the repository address, `labels.yml` gains the undeclared labels (`ready`, `auto-merge`, `train-first`), `CODEOWNERS` names a team or role, `SECURITY.md` (new), deletes `scratchpad/` and the tracked `.coverage`, `checkov-k8s.sarif` and `verification.md` | Count to 0 |
| 12 | WORKFLOWS-ENFORCE | `.github/workflows/`, `.github/actions/` (through the operator-pushed patch), and the guard's mode switch in `scripts/guards/` (handed over from lane 1 when lane 1 merges) | `github.repository` for the hard-coded name, the tfvars fetch step, the guard job in enforcing mode under both events, fork pull requests kept off the self-hosted pool (section 7.2) | The guard over `--tree` is zero before the flip; a planted value turns the required check red |

**Order.** Lane 1, then lane 2 (both are foundations), then lanes 3 to 11 in parallel (their territories are disjoint), then lane 12, then the cutover (section 6). `CONTRACT.md` is not in any lane: its wording is frozen-contract text, and its five value-bearing lines go to the contract request below. Any other finding the guard reports in `CONTRACT.md` is written as a further contract request, never edited by a lane.

**Why at most twelve.** Thirteen directories hold values. Merging `tests/terraform` into the Terraform lane and `tests/integration` into the backend lane keeps each test file next to the code whose behaviour it pins. Splitting `docs/` in two keeps each docs lane under about 50 files.

### 5.2 What no lane can do: the frozen contract

`apps/common/swarm_common/config.py` gives `ALLOWED_DOMAINS` the company's domain as its **runtime default**. `identity.py` and `models.py` name the live project and domain in comments. Both are drafted already as contract requests 127 and 128 (`docs/design/day-one-plan.md`, section 4). The fresh repository cannot be clean, nor the guard enforce, until the owner accepts them, so **the cutover waits on them** (question Q8). If they are declined, the guard needs a written exemption for those three files, which means the public repository carries the values.

---

## 6. The cutover runbook

### 6.1 Roles and words

- **The owner** holds the GitHub account that owns the repository, administers the Google Cloud projects and is the only one who can click in GitHub's and Google's consoles. Steps marked **[owner click]** are one-click or one-form steps only the owner can do.
- **The operator** runs the commands. The owner may be the operator.
- `<owner>/<repo>` is the repository's address. `<owner>/<archive>` is the archive's new name, a private name the owner picks (question Q10). `<old-id>` and `<new-id>` are the numeric repository ids.
- **The inventory** is a file the operator writes in step P1, kept on the operator's machine and in the archive's private `config/` prefix, never in the new repository.

### 6.2 Phase P: prepare (days before; nothing visible changes)

| Step | Who | What | Verified by |
|---|---|---|---|
| P1 | Operator | **Take the inventory**, read live: `gh api repos/<owner>/<repo>` (settings, merge options, visibility, id), `/hooks`, `/keys`, `/environments` (and each one's `deployment-branch-policies` and protection rules), `/actions/variables`, `/actions/secrets` (names only), `/environments/<e>/variables` and `/secrets` for each environment, `/rulesets` (each body), `/branches/main/protection`, `/actions/permissions`, `/actions/permissions/workflow`, `/actions/runners`, `/collaborators`, `/invitations`, `/topics`, `/autolinks`, `/pages`, `/releases`, `/tags`, `/forks`, `/vulnerability-alerts`, `/automated-security-fixes`, `/code-scanning/default-setup`, `/installation`; `gh api user/installations` and each one's `repositories` and `repository_selection`; `gh label list --limit 500 --json name,color,description`; `gh issue list --state open --limit 500`; `gh pr list --state open` | Every call returned 200 or a documented 404; the file lists each object kind with a count, and the count is printed |
| P2 | Owner | Build the pattern list (section 1.5) into `config/deployment-patterns.tsv`; run `sync-patterns.sh` | The guard prints the same digest from the GitHub secret and from Secret Manager |
| P3 | Owner | Write the live `deny-list.json`, the real tfvars and the operator `.env` into `config/<env>/` | `make plan` from a clean clone with no tracked tfvars produces the same plan as before (no changes) |
| P4 | Owner and operator | Merge lanes 1 to 12; the guard is enforcing and green on `main` | The `deployment-values` check is required and green on `main`; `--tree` prints zero findings |
| P5 | Operator | Run a credential scanner over the **archive's whole history** (`git log -p --all` through `scripts/publish-scan.sh`'s scanner, and a second open-source scanner) | The list of anything secret ever committed, by `path` and commit, never the value; feeds section 8 |
| P6 | Owner | Answer the open questions (`questions.json`), especially Q1, Q8 and Q10 | Answers recorded |

### 6.3 Phase F: the freeze (about one working day)

| Step | Who | What | Verified by |
|---|---|---|---|
| F1 | Operator | Announce the freeze. Set `vars.MERGE_TRAIN=off` and stop dispatching new agents against the repository | No new train branches appear |
| F2 | Owner and operator | **Open pull requests** (18 at the brief's count; read live): each is merged, if green and wanted, or closed with a comment giving its reason and, where the work continues, the new issue it moves to | `gh pr list --state open` is empty |
| F3 | Operator | **Lane and train branches:** delete `train/*` branches and every branch whose pull request is closed; the rest stay in the archive | `git ls-remote --heads` shows only `main` and branches deliberately kept |
| F4 | Owner | **Open issues triage** (89 at the brief's count): mark each `keep` or `drop` in the inventory. `drop` stays in the archive, with a closing comment saying so | Every open issue is marked |
| F5 | Operator | For each `keep`, write the generic text into the inventory: title as a fact, body from the issue form, no values, roles not names | The guard's `--text` mode over every rewritten issue prints zero findings |
| F6 | Operator | Take the clean tree: `git archive main` into a new directory, `git init`, one commit with a generic message, author and committer both set deliberately on that command (`GIT_AUTHOR_*` and `GIT_COMMITTER_*`) to a role address (`owner@example.com` or the account's no-reply address), never the machine's or the worker's configured identity | The guard's `--tree` over that commit prints zero; `git log` has one commit; `git log --format='%ae %ce'` shows only the role address |

### 6.4 Phase C: the cutover (about two hours; deploys paused, the live service keeps serving)

The live service does not stop: Cloud Run and GKE keep serving, and nothing deploys. What is unavailable is CI, deploys, merging and agents' publishing.

| Step | Who | What | Verified by |
|---|---|---|---|
| C1 | Operator | Disable Actions on the old repository: `gh api -X PUT repos/<owner>/<repo>/actions/permissions -F enabled=false` | No run starts |
| C2 | **[owner click]** | Rename the repository to `<owner>/<archive>` (Settings › General). Confirm it is private | `gh api repos/<owner>/<archive>` shows `private: true` and `<old-id>`; `gh api repos/<owner>/<repo>` redirects to it (until C3) |
| C3 | **[owner click]** or operator | Create the fresh, **private**, empty repository at `<owner>/<repo>` with no README, then immediately disable Actions on it (`actions/permissions enabled=false`). Read `<new-id>` | `gh api repos/<owner>/<repo>` returns `<new-id>` ≠ `<old-id>`; the redirect to the archive is gone. If GitHub refuses the name (a retired namespace), go to rollback R1 |
| C4 | Operator | **Settings:** description and topics (generic), merge options as in the inventory (squash only or as recorded, delete branch on merge, `allow_auto_merge=true`), default workflow permissions read-only, fork pull request workflows require approval for all outside collaborators | `gh api repos/<owner>/<repo>` matches the inventory field by field |
| C5 | Owner | **The CI cloud identity.** Today the condition binds the repository by name (`assertion.repository == "<owner>/<repo>"`, `terraform/bootstrap/wif.tf:196`) and each account binding by `job_workflow_ref` with the name. The new repository therefore matches unchanged, and the renamed archive stops matching. That is wanted: the archive must never deploy. Lane 4 hardens the condition with `assertion.repository_id == "<new-id>" && assertion.repository_owner_id == "<owner-id>"`, from tfvars in `config/`. Now that `<new-id>` is known, apply bootstrap from the owner's machine with the new id in the bootstrap tfvars, **before any workflow runs** | `gcloud iam workload-identity-pools providers describe` shows the condition with the id; a CI run in C12 gets credentials |
| C6 | Operator | **Environments, before the first push:** create `dev`, `prod`, `dev-iam`, `release`, `repo-secrets`, `build-pr` and each `<env>-plan`, with the required reviewers, wait timers and deployment-branch policies from the inventory (for example `prod` and `release` need a reviewer and allow `main` only; `repo-secrets` allows `main` only). This must happen before any workflow runs, because GitHub creates a missing environment, with **no protection**, the first time a job names it | `gh api repos/<owner>/<repo>/environments` matches the inventory: every name, reviewer and branch policy |
| C7 | Operator | **Variables:** copy every repository and environment variable from the inventory (`gh variable set`); values are readable by API, so this is a copy | Names and values match the inventory, compared by script, printing names only |
| C8 | Owner | **Secrets** cannot be read back, so each is re-set from its source: `MERGE_APP_PRIVATE_KEY`: generate a **new** key on the merge App's page **[owner click]**, set it in `repo-secrets`, delete the old key after C12 (a rotation for free); `SWARM_SANDBOX_GITHUB_TOKEN`: a new fine-grained token selecting the sandbox repository only; `DEPLOYMENT_PATTERNS`: `sync-patterns.sh` | `gh secret list` per environment matches the inventory's names |
| C9 | Operator | **Labels:** `gh label create` for every label in the inventory, including the undeclared `ready`, `auto-merge`, `train-first`. Issue templates arrive with the commit | `gh label list` matches |
| C10 | Operator | Push the clean commit to `main`. Push the plugin tag `sc-v<next>` on it (the old `sc-v*` tags pointed at commits with values and are not carried; the plugin's version is bumped in that commit) | `git ls-remote` shows `main` and the tag; the guard's `--tree` on the pushed commit is zero |
| C11 | **[owner click]** | **GitHub App installations that select repositories:** for each installation in the inventory with `repository_selection: selected` that listed `<old-id>` (the merge App; the platform's forge App if it selects; the runner dispatcher's App if it has one), add the new repository. An installation set to "all repositories" needs nothing. **Fine-grained tokens** that selected the old repository (the runner dispatcher's registration token, if it is one; any operator token): edit each to select the new one, or issue a new one and revoke the old | `gh api user/installations/<id>/repositories` lists `<new-id>` for each; the token's repository list shows the new repository |
| C12 | Operator | Enable Actions. Dispatch `security.yml` and `application.yml` on `main`, then `release.yml` for `dev` | The runs get Google credentials (C5), pick up self-hosted runners (C13) and deploy `dev` with no change in the plan. `prod` waits for its reviewer (C6 proven) |
| C13 | Owner | **The self-hosted runner dispatcher** lives outside this repository, in its own project. Its target entry is keyed by the repository name (`runner-fallback.yml:8-11`), so it may need nothing. **To be read at cutover:** whether it receives `workflow_job` through a repository webhook (then recreate the hook with the same URL and a **new** secret, set in both places) or an App webhook (then C11 covers it), and whether it registers runners with a token that selects repositories by id | A queued job is picked up within the dispatcher's usual delay; `gh api repos/<owner>/<repo>/actions/runners` shows a runner during the run |
| C14 | Operator | **Webhooks:** recreate every repository webhook in the inventory (P1, `/hooks`) with a new secret. The platform's push receiver (`apps/swarm-github-hooks`) is an **App-level** webhook (`terraform/infra/github_hooks.tf`), so it needs no repository hook. It identifies repositories by full name, so pushes to the new repository arrive as before | `gh api repos/<owner>/<repo>/hooks/<id>/deliveries` shows a 2xx after a push; the receiver's log shows the delivery |
| C15 | Operator | **Deploy keys:** none are referenced in the tree. **To be read at cutover** from `/keys`; recreate any found with a new key pair | `/keys` matches the inventory |
| C16 | Operator | **The ruleset:** recreate `main-protection` from the body kept in `docs/runbooks/merge-train.md`, with its required checks (now including `deployment-values`) and its bypass list. It gets a new numeric id; lane 10's runbooks look it up by name. Classic branch protection is not used (`docs/ci.md`); if the inventory shows any, recreate it | `gh api repos/<owner>/<repo>/rulesets` shows it, active; a direct push to `main` by a non-bypass actor is refused |
| C17 | Operator | **The merge train:** its state is entirely on GitHub (train branches, the state block in the draft pull request body, labels, `merge-train` statuses), all drained in F2 and F3. Set `vars.MERGE_TRAIN` back to its inventory value | The next scheduled `merge-train.yml` run finds an empty queue and exits green |
| C18 | Operator | **SwarmCloud's own registration:** a registered repository's id is a hash of the tenant and the lower-cased `github.com/<owner>/<repo>` (`apps/swarm-api/swarm_api/repositories.py:363-367`). The name is unchanged, so registrations, grants, stored clone URLs and git-token slots keep working. Check that the forge App installation (C11) covers the new repository | An agent on the repository clones, pushes a branch and opens a pull request |
| C19 | Operator | **The sc plugin:** `/plugin marketplace add <owner>/<repo>` resolves by name; the pin in `plugin.json` names `sc-v<next>` (C10) | A clean install of the plugin from the marketplace succeeds |
| C20 | Operator | **Kept issues:** create each from the inventory text, in the original order, with its labels. Close the archive's issue with a comment giving the new number. Recreate the agent quality epic and point the lane briefs at its new number | The guard's `--text` over every new issue is zero; every `keep` has a new number |
| C21 | Operator | **Dependabot and security settings:** set them to the inventory's values (none is configured in `.github/` today). Secret scanning and push protection are switched on in section 7 | `gh api repos/<owner>/<repo>` security fields match |
| C22 | Operator | **Cloud-side connections:** none in Terraform (no Cloud Build trigger, no Developer Connect). **To be read at cutover:** `gcloud builds triggers list` and `gcloud builds connections list` in each project return nothing | Both lists are empty, or each item found is reconnected |
| C23 | Operator | **Local clones:** every operator clone and every agent workspace has the same remote URL with an unrelated history. Re-clone; agents clone per run, so they need nothing | `git fetch` in a fresh clone shows one root commit |

**Archive and new repository, side by side after C23:**

| Object | Archive (`<owner>/<archive>`, private) | New (`<owner>/<repo>`) |
|---|---|---|
| Commits, branches, tags | All, unchanged | One clean commit, `main`, `sc-v<next>` |
| Issues and pull requests | All 599 issues and 2,981 pull requests, with their text | Only rewritten kept issues |
| Actions | Disabled | Enabled |
| Variables, secrets, environments | Still set; the merge App's old key is deleted after C12 | Recreated (C6 to C8) |
| CI cloud credentials | None: the name no longer matches, and the id condition excludes it | Granted by id |
| App installations | Removed from selections once C12 is green | Selected |

### 6.5 Rollback

| Point reached | Rollback | Who |
|---|---|---|
| **R1**: before C10 (no commit pushed) | Delete the new repository (typed confirmation), rename the archive back to `<owner>/<repo>` **[owner click]**, re-enable its Actions. Repository settings, GitHub objects and integrations are as they were. Re-apply bootstrap with the old id if C5 ran | Owner |
| **R2**: after C10, before C20 | Same as R1. What was set on the new repository is lost; nothing on the archive was changed | Owner |
| **R3**: after C20 | Same as R1, and the new issues are closed with a pointer to the old ones. After this point, only rollback if the new repository cannot be made to deploy | Owner |

The rename redirect is restored by renaming back, as long as the new repository has been deleted first.

### 6.6 The fork of 24 October

A fork made while the repository was public is a separate repository in its own network, owned by whoever made it, and holds the history as it was on that day, values included.

- **What can be done:** if the owner or a collaborator made it, delete it. If someone else did, ask them to delete it. GitHub's sensitive-data removal request covers credentials, not identifiers such as project ids, so it is unlikely to apply.
- **What cannot be done:** the archive's rename does not touch it. A fork of a public repository does not follow the parent into privacy; it stays public on its own.
- **What follows:** treat every value in it as disclosed (section 8). The fork's owner is named nowhere in the tree; the inventory records it.

---

## 7. Going public and restoring protection

### 7.1 Preconditions, each checked by the operator before the owner flips visibility

1. The guard is enforcing, required and green on `main`; `--tree` is zero; the pattern list's digest is the same in both stores.
2. The contract requests 127 and 128 are merged (section 5.2).
3. Every issue, label description, environment name, topic and the repository description passes the guard's `--text` mode.
4. No Actions variable value is read by a step that prints it in a log. Variables are not secret, but logs on a public repository are public. Lane 12's patch masks the project and account values with `::add-mask::` in each job that reads them.
5. Section 7.2 is in place.
6. Every workflow that runs on `pull_request_target` (today only the guard's, section 4.3 item 2) declares `permissions: { contents: read }` with no `id-token: write`, runs no `google-github-actions/auth` step, and is absent from `deployer_workflows` in `terraform/bootstrap/wif.tf`. Checked by reading each such workflow file and the `deployer_workflows` list, because the identity's ref clause admits that event's `refs/heads/main` token.

### 7.2 The self-hosted runner pool and fork pull requests

A public repository lets anyone open a pull request. A workflow that runs a fork's code on a self-hosted runner gives that code a machine inside the deployment's network. Lane 12's patch adds `github.event.pull_request.head.repo.full_name == github.repository` to the pool label expression, so a fork's pull request runs on GitHub-hosted runners only. The repository setting "require approval for all outside collaborators" (C4) is a second gate. The identity's condition admits `refs/heads/main` only, which keeps cloud credentials away from `pull_request` runs. It does not keep them from `pull_request_target` runs, whose token carries the base branch's `refs/heads/main`: there the protection is the job's own minimal permissions and the per-workflow binding (section 4.3 item 2, checked in section 7.1 item 6 and step V6).

### 7.3 The flip, and after

| Step | Who | Verified by |
|---|---|---|
| V0a. Before the flip, restore the rest of the protection from the inventory, in this order: the ruleset's required status checks (each name must match a job that runs on the new repository, or every pull request waits forever), its bypass actors (the merge App and the repository role it had), required reviews and conversation resolution if the inventory had them, then any classic branch protection rule the inventory recorded | Operator | The ruleset body read back equals the inventory's body except its id; `gh api repos/<owner>/<repo>/rules/branches/main` lists every rule |
| V0b. Before the flip, prove protection and auto-merge together: open a throwaway pull request, label it `ready`, and watch it merge only after every required check is green; then try to merge one with a red check | Operator | The green one merges through auto-merge; the red one is refused |
| V1. Set visibility to public (Settings › Danger zone, typed confirmation) | **[owner click]** | `gh api repos/<owner>/<repo>` shows `visibility: public` |
| V2. Re-check the ruleset: rulesets on a personal account's private repository may have been unenforced by plan; on a public repository they enforce. Read it back and test a refused direct push | Operator | The ruleset's `enforcement: active`; a test push from a non-bypass account is refused |
| V3. Switch on secret scanning, push protection and private vulnerability reporting (free on public repositories) | Operator | `gh api repos/<owner>/<repo>` shows each `enabled` |
| V4. Switch on code scanning's SARIF uploads, which `security.yml` already sends | Operator | The next `security.yml` run's upload succeeds |
| V5. Publish `SECURITY.md` (lane 11) and the contribution route | Operator | The Security tab shows the policy |
| V6. A fork pull request from a test account runs on hosted runners only and gets no secrets except the guard's pattern use under `pull_request_target`. Run it while the repository is still private, through a collaborator's fork, before V1, and again after V1 | Operator | The run's runner name is GitHub-hosted; the log shows no value and only a pass/fail count; the guard job's resolved permissions in the run log list `Contents: read` and no `IdToken: write`; the log has no `google-github-actions/auth` step; `deployer_workflows` does not name the guard's workflow file |

---

## 8. What cannot be undone, and what needs rotating

### 8.1 What cannot be undone

- **Everything already published while the repository was public:** the fork (section 6.6), any clone, search-engine caches, public event archives that record pushes, issue text and comments, and software-heritage archives. The archive's privacy hides it from GitHub visitors and from nothing else.
- **Old links.** Once C3 runs, `github.com/<owner>/<repo>/issues/<n>` and `/pull/<n>` resolve in the **new** repository. Links in external notes, chats and other repositories either 404 or, once the new repository's numbering reaches them, point at an unrelated issue. Renaming back (R1 to R3) is the only cure, and it ends the cutover.
- **Actions history** (runs, artifacts, caches, code-scanning results) stays with the archive. `publish-release.yml` needs a green `release` run for the commit it publishes, so the first release on the new repository runs `release` first.
- **Stars, watchers and traffic** stay with the archive.
- **Installed plugins pinned to an old `sc-v*` tag** fail to reinstall from the new repository. They must update to `sc-v<next>`.

### 8.2 What needs rotating

Identifiers are not credentials. A project id, a project number, a service-account address, an OAuth client id or a hostname grants nothing alone; access stays behind IAP, IAM and the groups. They do not need rotating. What does:

| Item | Why | When |
|---|---|---|
| Anything secret that P5 finds in the archive's history | It was public | Before V1; each by its owner, through `scripts/create-secrets.sh --stdin` for platform slots |
| The merge App's private key | Re-set anyway in C8; rotating costs nothing | C8, old key deleted after C12 |
| Repository webhook secrets | Recreated in C13 and C14 | At recreation |
| Fine-grained tokens that selected the old repository | They still reach the archive | C11: revoke the old token |
| The sandbox token | Re-issued in C8 | C8 |

**Reviewed and not rotated:** the platform's GitHub App keys and webhook secret (never in the tree; they live in Secret Manager slots), the IAP Desktop client secret (Google documents a Desktop client secret as not confidential, and the discovery document serves it by design).

### 8.3 How the contract's invariants hold

This design changes no runtime behaviour except where values are read.

- **Invariants 1 to 8** (demand, all-or-nothing capacity, concurrency from `LEASED`, park-and-exit, fencing, no Spot, request equals limit, checkpointing): no admission, dispatch, reservation, fencing or resource code changes. The cleanup lanes swap literals for placeholders in tests and comments, and configuration for defaults in scripts.
- **Invariant 9** (per-tenant isolation): tenant GSAs, secrets, prefixes and namespaces keep their names. The label prefix keeps its live keys (section 3.3). Placeholders change only tracked examples.
- **Invariant 10** (callers pick a `runner_profile` by name): untouched.
- **CLAUDE.md rule 2** (the shared project's resources): the never-touch list keeps every entry and gains a fail-closed reader (section 2.4). A missing list refuses everything.

---

## 9. Open questions

The owner's questions are in `questions.json` beside this change, each with the option this design builds. In short:

- **Q1.** Kept issues: recreated from the rewritten text (built) or transferred.
- **Q2.** The never-touch list's home: the config object, failing closed (built), or the literal stays project-keyed in `common.sh`.
- **Q3.** The pattern list: one config source synced to a repository secret and a Secret Manager entry (built), or a repository secret only.
- **Q4.** The label-key prefix: configurable, defaulting to `swarm.<org_domain>` (built), or kept as a fixed exemption. Answered "configurable" (8 November 2026); where it is read, "Tfvars everywhere" (12 November 2026, section 3.3).
- **Q5.** Old `#N` references in tracked text: rewritten to plain names (built) or kept.
- **Q6.** The `LICENSE` copyright holder.
- **Q7.** The repository's own address, which names the owner's account, is accepted as a product address (built), or the repository moves.
- **Q8.** Contract requests 127 and 128: accepted before cutover (the cutover waits on them).
- **Q9.** The public distribution addresses (the install script's bucket, the marketplace) as product addresses.
- **Q10.** The archive's name, and who made the 24 October fork.
- **Q11.** The Day One installer's two additions (section 2.3) belong to lane 5 of this program.
- **Q12.** Fork pull requests run on hosted runners only (built), or pull requests never use the pool.
