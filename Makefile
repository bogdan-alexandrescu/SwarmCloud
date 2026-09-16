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

ENVIRONMENT ?= dev
PROJECT_ID  ?= saga-agents-staging
REGION      ?= us-central1

export ENVIRONMENT
export PROJECT_ID
export REGION

SCRIPTS  := scripts
ENV_DIR  := terraform/environments/$(ENVIRONMENT)
TERRAFORM := $(shell if [ -x "$$HOME/.local/bin/terraform" ]; then echo "$$HOME/.local/bin/terraform"; else command -v terraform; fi)
TFLINT    := $(shell if [ -x "$$HOME/.local/bin/tflint" ]; then echo "$$HOME/.local/bin/tflint"; else command -v tflint || echo ""; fi)
CHECKOV   := $(shell if [ -x "$$HOME/.local/bin/checkov" ]; then echo "$$HOME/.local/bin/checkov"; else command -v checkov || echo ""; fi)
TRIVY     := $(shell if [ -x "$$HOME/.local/bin/trivy" ]; then echo "$$HOME/.local/bin/trivy"; else command -v trivy || echo ""; fi)
KUBECTL   := $(shell if [ -x /opt/homebrew/bin/kubectl ]; then echo /opt/homebrew/bin/kubectl; else command -v kubectl || echo ""; fi)

TF_STATE_BUCKET ?= saga-agents-terraform-state-staging
TF_INIT_ARGS := -backend-config=bucket=$(TF_STATE_BUCKET) -backend-config=prefix=swarm/$(ENVIRONMENT)

.PHONY: help prerequisites bootstrap infra build push deploy up smoke \
        load-test quota-test concurrency-test failure-test race-test test lint \
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

tf-init: ## terraform init for this environment
	@$(TERRAFORM) -chdir=$(ENV_DIR) init -input=false $(TF_INIT_ARGS)

tf-plan: ## terraform plan (review every create in a shared project)
	@$(TERRAFORM) -chdir=$(ENV_DIR) plan -input=false -lock-timeout=120s -out=$(CURDIR)/build/$(ENVIRONMENT).tfplan

tf-apply: ## terraform apply the plan from tf-plan
	@test -f build/$(ENVIRONMENT).tfplan || { echo "run 'make tf-plan' first"; exit 1; }
	@$(TERRAFORM) -chdir=$(ENV_DIR) apply -input=false -lock-timeout=120s $(CURDIR)/build/$(ENVIRONMENT).tfplan
	@rm -f build/$(ENVIRONMENT).tfplan

infra: ## Plan and apply infrastructure, then point kubectl at the swarm cluster
	@$(MAKE) tf-plan
	@$(MAKE) tf-apply
	@$(SCRIPTS)/configure-kubectl.sh || echo "note: the GKE cluster is not reachable yet; the Cloud Run path does not need it"

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

test: ## Unit tests plus the destroy guard self-test (no cloud resources needed)
	@$(SCRIPTS)/destroy.sh --self-test
	@if [ -d tests/unit ] && [ -n "$$(find tests/unit -name 'test_*.py' -print -quit)" ]; then \
	  uv run --project . pytest tests/unit -q; \
	else \
	  echo "no unit tests in tests/unit yet"; \
	fi

## ---------------------------------------------------------------------------
## Quality
## ---------------------------------------------------------------------------

lint: ## shellcheck, terraform fmt/validate, tflint, kubernetes manifests
	@echo "==> shellcheck"
	@shellcheck -x $(SCRIPTS)/*.sh $(SCRIPTS)/lib/*.sh
	@echo "==> terraform fmt"
	@$(TERRAFORM) fmt -check -recursive terraform || { echo "run 'make fmt'"; exit 1; }
	@if [ -d $(ENV_DIR)/.terraform ]; then echo "==> terraform validate"; $(TERRAFORM) -chdir=$(ENV_DIR) validate; fi
	@if [ -n "$(TFLINT)" ] && [ -n "$$(find terraform -name '*.tf' -print -quit 2>/dev/null)" ]; then echo "==> tflint"; $(TFLINT) --chdir=terraform --recursive; fi
	@if [ -n "$(KUBECTL)" ] && [ -n "$$(find kubernetes -name '*.yaml' -print -quit 2>/dev/null)" ]; then echo "==> kubernetes manifests"; find kubernetes -name '*.yaml' -exec $(KUBECTL) apply --dry-run=client -f {} \; >/dev/null; fi
	@echo "lint ok"

fmt: ## Rewrite terraform files in canonical form
	@$(TERRAFORM) fmt -recursive terraform
	@echo "formatted"

security: ## checkov over terraform, trivy over the built images and the repo
	@if [ -n "$(CHECKOV)" ] && [ -n "$$(find terraform -name '*.tf' -print -quit 2>/dev/null)" ]; then $(CHECKOV) -d terraform --quiet --compact; else echo "no terraform to scan"; fi
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
	@rm -rf build/*.tfplan build/*.json build/*.jsonl build/kubeconfig-*.yaml build/cloudbuild-*.yaml
	@echo "cleaned build/"
