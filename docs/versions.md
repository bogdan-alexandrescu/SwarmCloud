# Versions

Every version this platform is built and verified against. All of them were
checked on the reference workstation on **2026-09-15/16**; the commands to
re-verify are at the bottom.

A pin here is not a preference. In several cases below the *wrong* version is
the one that wins `$PATH` on the reference machine, and in at least two of them
the failure is silent.

---

## Toolchain

| Tool | Pinned | Verified | Where it is enforced |
|---|---|---|---|
| Terraform | **1.16.2** | `terraform version` | `~/.local/bin/terraform`; roots require `>= 1.9.0` |
| OpenTofu | **1.12.6** | `tofu version` | drop-in alternative; not used by default |
| tflint | **0.64.0** | `tflint --version` | `make lint` |
| checkov | **3.3.17** | `checkov --version` | `make security`, CI |
| trivy | **0.74.0** | `trivy --version` | `make security`, `push-images.sh` |
| kubectl | **1.36.3** | `kubectl version --client` | `/opt/homebrew/bin/kubectl` |
| Python | **3.11** | `.python-version` | `requires-python = ">=3.11"` |
| uv | **0.11.7** | `uv --version` | pinned image in the Dockerfiles |
| Node | **24 LTS** (see note) | `node --version` in the image | `NODE_IMAGE` digest pin |
| gcloud SDK | **483.0.0** | `gcloud version` | `prerequisites.sh` |
| shellcheck | **0.11.0** | `shellcheck --version` | `make lint`, CI |
| jq | **1.7.1** | `jq --version` | `prerequisites.sh` |
| bash | **3.2.57** (macOS) | `bash --version` | see §4 |

### Terraform providers

| Provider | Constraint | Locked build |
|---|---|---|
| `hashicorp/google` | `~> 6.0` | **6.50.0** |
| `hashicorp/google-beta` | `~> 6.0` | **6.50.0** |
| `hashicorp/random` | `~> 3.0` | **3.9.1** |

Constraints live in each root's `versions.tf`; the **exact** build is pinned by
`.terraform.lock.hcl`, which is committed. A lock file that is not committed
means two operators can apply the same configuration through two different
provider builds and get two different results.

### Container bases (digest-pinned in `images/*/Dockerfile`)

| Image | Tag | Digest (truncated) |
|---|---|---|
| Python | `python:3.11-slim-bookworm` | `sha256:528257d4...` |
| Node | `node:24-bookworm-slim` | `sha256:2fe369e9...` |
| uv | `ghcr.io/astral-sh/uv:0.11.7` | `sha256:240fb85a...` |
| Playwright | `playwright==1.63.0` (pip) | — |
| Claude Code CLI | `@anthropic-ai/claude-code@2.1.273` | — |
| Codex CLI | `@openai/codex@0.154.0` | — |

Every `FROM` carries a digest, so a rebuild in six months produces the same
toolchain rather than whatever the tag points at that day.

---

## 1. Node: 24, not 20 — a deliberate departure

The original build brief specified **Node LTS 20**. The images pin **Node 24
LTS**, because Node 20 reached end of life in April 2026 and 24 is the current
LTS line. Pinning an EOL runtime would mean shipping an agent runtime that
receives no security updates — in an image that runs other people's code.

If a runner genuinely requires Node 20, change `NODE_IMAGE` in
`images/agent-runtime-base/Dockerfile` to a Node 20 digest and record why here.
Do not silently downgrade: the CLIs the `claude-code` and `codex` profiles run
are pinned against the Node in this image and are tested there.

Note that the Node on an operator's workstation is **not** the Node that runs
agents — the reference machine has v20.10.0 and the image has 24. Nothing in the
platform executes the workstation's Node, so `prerequisites.sh` reports it for
information rather than treating drift as a problem.

---

## 2. kubectl: why it is resolved explicitly

Three `kubectl` binaries exist on the reference workstation, and the two that win
`$PATH` lookup are **1.22 (EKS)** and **1.25 (Docker Desktop)** — both far outside
the supported skew for a modern control plane.

A 1.22 client against a 1.3x server does not fail loudly. It **silently drops
fields it does not understand** from manifests it applies. A pod security context
or an extended-run-time annotation that vanishes on apply is a platform promise
that quietly stops being true.

So `scripts/lib/common.sh` resolves kubectl by checking, in order:
`$SWARM_KUBECTL`, `/opt/homebrew/bin/kubectl`,
`/usr/local/opt/kubernetes-cli/bin/kubectl`, and only then `$PATH` — accepting
the first that reports **>= 1.30**. `$PATH` is consulted last, on purpose.

---

## 3. checkov: why `~/.local/bin` wins

Homebrew's checkov 3.3.10 on this machine **raises on import** and shadows the
working 3.3.17 in `~/.local/bin`. `prefer_local_bin()` therefore prefers
`~/.local/bin/<tool>` over `$PATH` for terraform, tflint, checkov and trivy, with
`SWARM_<TOOL>` environment overrides for anyone whose layout differs.

---

## 4. bash 3.2, not 4+

macOS ships **bash 3.2.57** and Apple will not ship a newer one. Every script in
`scripts/` is written for it:

* no associative arrays;
* no `mapfile` / `readarray`;
* no `${var,,}` / `${var^^}`;
* empty arrays always expanded as `${arr[@]+"${arr[@]}"}` — plain `"${arr[@]}"`
  on an empty array is an unbound-variable error under `set -u` in 3.2.

Note also that the interactive shell on this machine is **zsh**, which does not
word-split unquoted variables the way bash does. Scripts declare
`#!/usr/bin/env bash`; anything typed at a zsh prompt needs explicit quoting or
arrays.

---

## 5. Python 3.11

`.python-version` and `requires-python = ">=3.11"` in every package. 3.11 is
chosen for `datetime.UTC`-era typing ergonomics and for matching the runtime base
image exactly — the container and the developer machine run the same minor
version, so a syntax or stdlib difference cannot appear only in production.

Dependencies are resolved by `uv` and locked in `uv.lock`, which is committed.

---

## 6. The Firestore emulator needs a JRE

`gcloud emulators firestore` is a **Java** program. On the reference workstation
`/usr/bin/java` is Apple's stub that only offers to install a JRE, so
`command -v java` succeeds while `java -version` fails. `dev.sh` checks the
latter and says exactly what to install:

```bash
brew install --cask temurin
# or, if Docker works on your machine:
docker compose up firestore
```

`gcloud components install cloud-firestore-emulator` is also required; the local
SDK has `cloud-firestore-emulator 1.19.7`.

---

## 7. Upgrading

**Terraform / providers.** Bump the constraint in `versions.tf`, run
`terraform init -upgrade`, commit the changed `.terraform.lock.hcl`, and run
`make tf-plan` for **every** environment. A provider minor bump can rewrite an
attribute default; in a shared project that is a plan to read carefully.

**kubectl.** Must stay within one minor of the GKE control plane. Autopilot
follows its release channel (`RAPID` in dev, `STABLE` in prod), so check the
cluster version before bumping the client.

**Container bases.** Change the digest, not the tag, and rebuild:

```bash
docker buildx imagetools inspect python:3.11-slim-bookworm   # get the new digest
# edit images/agent-runtime-base/Dockerfile, then:
make build push
```

**Agent CLIs.** `CLAUDE_CODE_VERSION` and `CODEX_VERSION` are build args pinned
in the Dockerfile. Bump deliberately and run `make smoke` plus one real task per
affected profile — a CLI's output format changes more often than its flags.

**Scanners.** checkov and trivy update their rule sets constantly; a green build
today can fail tomorrow on a newly published CVE. That is the point, and it is
why `security.yml` also runs on a weekly schedule rather than only on PRs.

---

## 8. Re-verifying every pin

```bash
make prerequisites          # checks everything below and reports drift
```

or individually:

```bash
~/.local/bin/terraform version
tofu version
~/.local/bin/tflint --version
~/.local/bin/checkov --version
trivy --version
/opt/homebrew/bin/kubectl version --client
python3 --version && uv --version
gcloud version
shellcheck --version && jq --version && bash --version
terraform providers -chdir=terraform/infra
```

`prerequisites.sh` treats version drift as a **warning**, not a failure — except
for kubectl, where an old client is worse than a missing one because it fails
silently.
