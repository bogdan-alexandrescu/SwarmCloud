# Agent swarm -- operations entry point.
#
# Every target here is a thin wrapper over a script in scripts/. The scripts are
# the real interface: they run identically from a laptop and from CI, and they
# carry the safety checks. If a target here grows logic, that logic belongs in a
# script instead.
#
# GNU Make 3.81 (what macOS ships) has no .ONESHELL, so each recipe line is its
# own shell. Anything needing multiple steps lives in scripts/.

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

SCRIPTS  := scripts

# Configuration and tool paths are ASKED OF THE SCRIPTS, not re-derived here.
#
# This Makefile used to do both itself, and got both wrong in the same silent
# way. It read no .env, so with ENVIRONMENT=staging in .env `make tf-plan`
# planned dev while `make status` reported staging -- make and the scripts
# disagreed about which environment they were in. And it hard-coded its own
# `if [ -x $$HOME/.local/bin/... ]` probes, so the SWARM_TERRAFORM /
# SWARM_TFLINT / SWARM_CHECKOV / SWARM_TRIVY / SWARM_KUBECTL overrides that
# .env.example documents and scripts/lib/common.sh honours had no effect on
# `make lint`, `make fmt`, `make tf-plan`, `make tf-apply` or `make security`.
# On this workstation that matters: kubectl 1.22 and checkov 3.3.10 win $PATH
# and both are broken in ways that report success.
#
# `?=` still lets an explicit `make ENVIRONMENT=prod ...` win over .env, which is
# the precedence an operator expects.
RESOLVE := $(SCRIPTS)/lib/resolve.sh

ENVIRONMENT ?= $(shell $(RESOLVE) env ENVIRONMENT)
PROJECT_ID  ?= $(shell $(RESOLVE) env PROJECT_ID)
REGION      ?= $(shell $(RESOLVE) env REGION)

export ENVIRONMENT
export PROJECT_ID
export REGION

# ONE terraform root, parameterised per environment by a tfvars file. dev and
# prod therefore run identical configuration and differ only in inputs and in
# the state prefix -- a root per environment is how prod quietly drifts.
TF_ROOT  := terraform/infra
VAR_FILE := terraform/environments/$(ENVIRONMENT)/$(ENVIRONMENT).tfvars
TERRAFORM := $(shell $(RESOLVE) tool terraform)
TFLINT    := $(shell $(RESOLVE) tool tflint)
CHECKOV   := $(shell $(RESOLVE) tool checkov)
TRIVY     := $(shell $(RESOLVE) tool trivy)
KUBECTL   := $(shell $(RESOLVE) tool kubectl)

# The swarm's OWN state bucket, created by terraform/bootstrap. Deliberately not
# saga-agents-terraform-state-staging: that one belongs to another team.
TF_STATE_BUCKET ?= swarm-tfstate-$(PROJECT_ID)
TF_INIT_ARGS := -backend-config=bucket=$(TF_STATE_BUCKET) -backend-config=prefix=infra/$(ENVIRONMENT)
TF_VAR_ARGS  := -var-file=$(CURDIR)/$(VAR_FILE)

.PHONY: help prerequisites bootstrap infra build push deploy up smoke \
        load-test quota-test concurrency-test failure-test race-test e2e-test \
        test tf-test ui-test lint \
        fmt security tf-init tf-plan tf-apply status logs pause-swarm resume-swarm \
        destroy purge-data dev kubectl register-tenant secrets clean

## ---------------------------------------------------------------------------
## Getting started
## ---------------------------------------------------------------------------

help: ## Show this help
	@printf '\nAgent swarm -- make targets (ENVIRONMENT=$(ENVIRONMENT), PROJECT_ID=$(PROJECT_ID))\n\n'
	@awk 'BEGIN {FS = ":.*?## "} /^## -/ {next} /^[a-zA-Z0-9_-]+:.*?## / {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2} /^##[^-]/ {printf "\n\033[1m%s\033[0m\n", substr($$0, 4)}' $(MAKEFILE_LIST)
	@printf '\nFirst run:    make prerequisites bootstrap infra build push deploy smoke\n'
	@printf 'Every day:    make status | make logs | make smoke\n'
	@printf 'Danger zone:  make pause-swarm | make purge-data | make destroy\n\n'

prerequisites: ## Check tools, credentials, APIs and shared-project guard rails
	@$(SCRIPTS)/prerequisites.sh

bootstrap: ## Prepare the project: state bucket, .env, terraform init
	@$(SCRIPTS)/bootstrap.sh --environment $(ENVIRONMENT)

up: ## Everything from zero: bootstrap, infra, build, push, deploy, smoke
	@$(MAKE) bootstrap
	@$(MAKE) infra
	@$(MAKE) build
	@$(MAKE) push
	@$(MAKE) deploy
	@$(MAKE) smoke

## ---------------------------------------------------------------------------
## Infrastructure
## ---------------------------------------------------------------------------

tf-init: ## terraform init for this environment (state prefix infra/$(ENVIRONMENT))
	@$(TERRAFORM) -chdir=$(TF_ROOT) init -input=false -reconfigure $(TF_INIT_ARGS)

tf-plan: ## terraform plan (review every create in a shared project)
	@test -f $(VAR_FILE) || { echo "no $(VAR_FILE); ENVIRONMENT=$(ENVIRONMENT) has no inputs"; exit 1; }
	@$(SCRIPTS)/plan.sh

tf-apply: ## terraform apply the plan from tf-plan
	@test -f build/$(ENVIRONMENT).tfplan || { echo "run 'make tf-plan' first"; exit 1; }
	@$(TERRAFORM) -chdir=$(TF_ROOT) apply -input=false -lock-timeout=120s $(CURDIR)/build/$(ENVIRONMENT).tfplan
	@rm -f build/$(ENVIRONMENT).tfplan

infra: ## Plan and apply infrastructure, then point kubectl at the swarm cluster
	@$(MAKE) tf-plan
	@$(MAKE) tf-apply
	@$(SCRIPTS)/configure-kubectl.sh || echo "note: the GKE cluster is not reachable yet; the Cloud Run path does not need it"

pool-check: ## Compare live pool ceilings against the terraform output (needs credentials)
	@# NOT part of `make test`, which is offline by contract. Pool documents carry
	@# ignore_changes in terraform, so tfvars describes a NEW environment and says
	@# nothing about a running one; this is the only thing that compares them.
	@$(SCRIPTS)/pool-limit.sh --check

kubectl-guard: ## Print the export that puts the guarded kubectl ahead of the real one
	@echo 'export PATH="$(CURDIR)/bin:$$PATH"'
	@echo '# eval "$$(make kubectl-guard)" -- a bare kubectl then refuses any' >&2
	@echo '# context that is not the swarm cluster, read-only commands included.' >&2

kubectl: ## Write an isolated kubeconfig for the swarm cluster
	@$(SCRIPTS)/configure-kubectl.sh

## ---------------------------------------------------------------------------
## Images and deployment
## ---------------------------------------------------------------------------

build: ## Build every image with Cloud Build (never the local Docker daemon)
	@$(SCRIPTS)/build-images.sh

push: ## Scan and promote built images to the environment channel tag, by digest
	@$(SCRIPTS)/push-images.sh --channel $(ENVIRONMENT)

deploy: ## Roll the promoted digests out to Cloud Run
	@$(SCRIPTS)/lib/deploy.sh

## ---------------------------------------------------------------------------
## Verification
## ---------------------------------------------------------------------------

smoke: ## End-to-end: submit a mock task and prove it runs and releases capacity
	@$(SCRIPTS)/smoke-test.sh

load-test: ## Sustained load; reports admission and end-to-end latency percentiles
	@$(SCRIPTS)/load-test.sh

quota-test: ## Provider exhaustion parks work instead of paying to wait
	@$(SCRIPTS)/quota-test.sh

concurrency-test: ## No pool is ever over its effective limit, at any sample
	@$(SCRIPTS)/concurrency-test.sh

failure-test: ## Failures, cancellation and malformed input leak no capacity
	@$(SCRIPTS)/failure-test.sh

race-test: ## The last free slot goes to exactly one task
	@$(SCRIPTS)/race-test.sh

e2e-test: ## The seams: a workflow handoff, spend through the API, sign-in, state agreement
	@# The other targets above check PROPERTIES of the platform. This one checks
	@# that its halves are JOINED, which is the class of defect that produced
	@# every outage of the last three days -- both ends built, the middle never
	@# executed. tests/integration/test_e2e_suite_can_fail.py drives this same
	@# script offline against a fake platform and proves each of its checks
	@# fails when the fact it covers is false; that runs in `make test`.
	@$(SCRIPTS)/e2e-test.sh

test: ## Unit tests, terraform tests and the guard self-tests (no cloud resources needed)
	@$(SCRIPTS)/destroy.sh --self-test
	@$(SCRIPTS)/lib/plan-guard.sh --self-test
	@$(SCRIPTS)/lib/auth-guard.sh --self-test
	@$(SCRIPTS)/lib/kubectl-guard.sh --self-test
	@$(SCRIPTS)/lib/check-contract-parity.sh
	@$(SCRIPTS)/lib/check-env-parity.sh
	@# NO "if the directory exists" GUARD on either suite below. `make test` is
	@# the gate CLAUDE.md names before anyone may say a change is done, and a
	@# guard that turns "I could not find the tests" into a green run is a gate
	@# that passes hardest when it is most broken -- the class this repository
	@# keeps producing. pytest already fails on each case that matters: exit 4
	@# for a path that is not there, exit 5 for a path that collects nothing,
	@# exit 1 for a failing test. Nothing here needs to second-guess it.
	@uv run --project . pytest tests/unit -q
	@# tests/integration is OFFLINE -- it drives the real scripts end to end with
	@# a fake gcloud and a fake curl on PATH under --dry-run, creating nothing and
	@# needing no credentials. It was in no target, and it rotted exactly as the
	@# comment under tf-test predicted for the terraform suite: 9c639af ("An HTTP
	@# error is not an empty result") correctly taught fs_request to check the
	@# status code, the fake curl had never honoured `-o`/`-w`, and all three
	@# files errored in their fixture for 95 commits with nothing to report it.
	@uv run --project . pytest tests/integration -q
	@$(MAKE) tf-test

ui-test: ## Drive the real UI in a headed browser and assert what a person would eyeball
	@# DELIBERATELY NOT A PREREQUISITE OF `test`, AND `test` IS NOT A
	@# PREREQUISITE OF THIS.
	@#
	@# `make test` is offline, needs no credentials, creates nothing and
	@# finishes in seconds. That is load-bearing: it is the gate CLAUDE.md names
	@# before anyone may say a change is done, so it has to stay cheap enough
	@# that nobody is tempted to skip it. This suite builds a bundle, starts an
	@# HTTP server and drives a headed Chromium, and takes minutes.
	@#
	@# Folding it in would also, the first time Chromium was missing on a
	@# machine, teach somebody to add an "if the browser exists" guard -- and a
	@# guard that turns "I could not find the browser" into a green run is the
	@# exact shape the comment under `test` above refuses. Separate target, no
	@# guard, and it exits 3 rather than 0 when it could not run at all.
	@$(SCRIPTS)/ui-test.sh $(UI_TEST_ARGS)

tf-test: ## Native `terraform test` over terraform/ (mock provider, offline, no credentials)
	@# 86 assertions covering the platform promises the docs lean on. This suite
	@# existed and was wired into nothing -- not make test, not make lint, not any
	@# workflow -- so it sat outside the gate CLAUDE.md names as the completeness
	@# check and would have rotted the first time a module changed.
	@# The two reasons this can not run are kept apart. One message for both was
	@# the audit-04 shape: "terraform not found, or no tests/terraform" sent a
	@# reader to install a binary they already had when the suite had actually
	@# been renamed out from under the target. A missing BINARY is a workstation
	@# without terraform, and skipping is right -- the terraform.yml job still
	@# runs it. A missing SUITE is the 86 assertions silently not running, and is
	@# a failure.
	@if [ ! -d tests/terraform ]; then \
	  echo "tests/terraform is missing: the 86 mock-provider assertions cannot run." >&2; \
	  echo "If the suite moved, point tf-test at it -- do not let it skip." >&2; \
	  exit 1; \
	elif [ -z "$(TERRAFORM)" ]; then \
	  echo "terraform is not installed, so tests/terraform did NOT run here."; \
	  echo "The terraform.yml workflow runs it on every push."; \
	else \
	  $(TERRAFORM) -chdir=tests/terraform init -input=false >/dev/null && \
	  $(TERRAFORM) -chdir=tests/terraform test; \
	fi

## ---------------------------------------------------------------------------
## Quality
## ---------------------------------------------------------------------------

lint: ## shellcheck, doc links, terraform fmt/validate, tflint, kubernetes manifests
	@echo "==> shellcheck"
	@shellcheck -x $(SCRIPTS)/*.sh $(SCRIPTS)/lib/*.sh
	@echo "==> documentation links"
	@$(SCRIPTS)/lib/check-doc-links.sh
	@echo "==> terraform fmt"
	@# BOTH roots. `terraform fmt` takes one directory, and for a long time the
	@# only one named here was terraform/ -- so tests/terraform/, the 87
	@# assertions `make test` now depends on, was the one Terraform in the tree
	@# whose formatting nothing checked.
	@$(TERRAFORM) fmt -check -recursive terraform || { echo "run 'make fmt'"; exit 1; }
	@$(TERRAFORM) fmt -check -recursive tests/terraform || { echo "run 'make fmt'"; exit 1; }
	@if [ -d $(TF_ROOT)/.terraform ]; then echo "==> terraform validate"; $(TERRAFORM) -chdir=$(TF_ROOT) validate; fi
	@if [ -n "$(TFLINT)" ] && [ -n "$$(find terraform -name '*.tf' -print -quit 2>/dev/null)" ]; then echo "==> tflint"; $(TFLINT) --chdir=terraform --recursive; fi
	@if [ -n "$(KUBECTL)" ] && [ -f kubernetes/render.py ]; then echo "==> kubernetes manifests"; $(SCRIPTS)/lib/validate-manifests.sh; fi
	@echo "lint ok"

fmt: ## Rewrite terraform files in canonical form
	@$(TERRAFORM) fmt -recursive terraform
	@$(TERRAFORM) fmt -recursive tests/terraform
	@echo "formatted"

security: ## checkov over terraform and the RENDERED manifests, trivy over the repo
	@if [ -n "$(CHECKOV)" ] && [ -n "$$(find terraform -name '*.tf' -print -quit 2>/dev/null)" ]; then $(CHECKOV) -d terraform --framework terraform --quiet --compact; else echo "no terraform to scan"; fi
	@# RENDERED, not raw. The files in kubernetes/ are templates: checkov reads a
	@# placeholder image reference as an unpinned tag, and silently SKIPS a file a
	@# placeholder makes unparseable while still exiting 0 -- so the scan reports
	@# clean over exactly the two manifests that carry the container hardening.
	@# The three skipped checks are the image-reference family; scripts/push-images.sh
	@# pins the channel tag to a trivy-scanned digest, so provenance is enforced in
	@# the image pipeline rather than in a lint artifact. Same set as security.yml.
	@if [ -n "$(CHECKOV)" ] && [ -f kubernetes/render.py ]; then \
	  rm -rf build/rendered && mkdir -p build/rendered && \
	  python3 kubernetes/render.py policies > build/rendered/policies.yaml && \
	  python3 kubernetes/render.py tenant --tenant lint > build/rendered/tenant.yaml && \
	  for p in $$(python3 -c "import sys; sys.path.insert(0,'apps/common'); from swarm_common.profiles import RUNNER_PROFILES; print(' '.join(sorted(RUNNER_PROFILES)))"); do \
	    python3 kubernetes/render.py job --tenant lint --profile $$p --task tsk_lint --attempt att_lint --lease lease_lint --generation 1 > build/rendered/job-$$p.yaml; \
	  done && \
	  $(CHECKOV) -d build/rendered --framework kubernetes --skip-check CKV_K8S_14,CKV_K8S_15,CKV_K8S_43 --quiet --compact; \
	else echo "no kubernetes manifests to scan"; fi
	@if [ -n "$(TRIVY)" ]; then $(TRIVY) fs --scanners vuln,secret,misconfig --exit-code 1 --severity HIGH,CRITICAL --ignore-unfixed .; fi

## ---------------------------------------------------------------------------
## Operations
## ---------------------------------------------------------------------------

status: ## Cluster, jobs, agents, queues, leases, pools, quota -- one screen
	@$(SCRIPTS)/status.sh

logs: ## Recent control-plane logs (SERVICE=swarm-api LINES=100 FOLLOW=1)
	@if [ "$${FOLLOW:-0}" = "1" ]; then \
	  gcloud beta logging tail 'resource.type="cloud_run_revision" AND resource.labels.service_name=~"^swarm-"' --project $(PROJECT_ID) --format='value(timestamp,resource.labels.service_name,textPayload,jsonPayload.message)' | $(SCRIPTS)/lib/redact-stream.sh; \
	else \
	  gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.service_name=~"^swarm-"'$${SERVICE:+" AND resource.labels.service_name=\"$$SERVICE\""} --project $(PROJECT_ID) --limit $${LINES:-100} --freshness $${FRESHNESS:-1h} --order desc --format='table(timestamp.date("%H:%M:%S"),resource.labels.service_name:label=SERVICE,severity,jsonPayload.message,textPayload)' | $(SCRIPTS)/lib/redact-stream.sh; \
	fi

pause-swarm: ## Stop admitting new work; running tasks continue
	@$(SCRIPTS)/pause-swarm.sh

resume-swarm: ## Resume exactly what pause-swarm paused
	@$(SCRIPTS)/resume-swarm.sh

register-tenant: ## Register a tenant (GROUP=eng@saga.xyz or USER=alice@saga.xyz)
	@test -n "$${GROUP:-}$${USER_EMAIL:-}" || { echo "usage: make register-tenant GROUP=eng@saga.xyz [PROVIDERS=anthropic]"; exit 1; }
	@$(SCRIPTS)/register-tenant.sh $${GROUP:+--group $$GROUP} $${USER_EMAIL:+--user $$USER_EMAIL} $${PROVIDERS:+--providers $$PROVIDERS}

secrets: ## Store a tenant provider key (TENANT=eng PROVIDER=anthropic)
	@test -n "$${TENANT:-}" -a -n "$${PROVIDER:-}" || { echo "usage: make secrets TENANT=eng PROVIDER=anthropic"; exit 1; }
	@$(SCRIPTS)/create-secrets.sh --tenant $$TENANT --provider $$PROVIDER --stdin

dev: ## Local loop: Firestore emulator plus a service (TARGET=api|scheduler|quota-broker|emulator)
	@$(SCRIPTS)/lib/dev.sh $${TARGET:-api}

## ---------------------------------------------------------------------------
## Danger zone
## ---------------------------------------------------------------------------

destroy: ## Destroy swarm infrastructure, aborting on anything not ours
	@$(SCRIPTS)/destroy.sh --environment $(ENVIRONMENT)

purge-data: ## Delete swarm runtime data (Firestore, artifacts), never infrastructure
	@$(SCRIPTS)/purge-data.sh --environment $(ENVIRONMENT)

clean: ## Remove local build artifacts (never touches the cloud)
	@rm -rf build/*.tfplan build/*.json build/*.jsonl build/kubeconfig-*.yaml build/cloudbuild-*.yaml build/rendered
	@echo "cleaned build/"

verify-remote: ## Run the verification gate INSIDE the VPC (smoke, concurrency, race)
	@$(SCRIPTS)/verify-remote.sh
