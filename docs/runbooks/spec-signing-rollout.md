# Step-spec signing: rollout, rotation and revocation

Contract request 34 ([`docs/contract-change-requests.md`](../contract-change-requests.md),
issue #342). swarm-api signs every step's canonical spec with a Cloud KMS key
at submission; every worker verifies the signature before it reads anything
the spec decides, and refuses a task that does not verify as
`spec_signature_invalid`.

**Why it exists.** A tenant's agent writes its own task documents -- it has to,
to report progress -- and nothing stopped it from rewriting a *later* step of
its own workflow while that step sat parked: the prompt, the repository, the
inputs. The later step then ran whatever the earlier agent chose. The
signature binds what the caller submitted, so a rewritten step is refused
instead of run.

**Why it rolls out in stages.** Every task already queued or parked when the
verifying worker ships is unsigned, and signing cannot be retrofitted: signing
a document as it stands now would sign whatever an agent already wrote into
it. So the worker starts in `legacy`, which admits an unsigned task created
before a cutover, and moves to `enforce`, which admits none. The code enforces
after **2026-10-20T00:00:00Z** whatever the configuration says
(`SPEC_LEGACY_UNTIL`, in the worker), so a forgotten flag stops working anyway.

## Where each piece lives

| piece | where | applied by |
|---|---|---|
| `cloudkms.googleapis.com`, the key ring `swarm-<env>-specs`, the key `step-spec` (EC P-256, software), swarm-api's `signer` grant, the deployer's `publicKeyViewer` + `viewer` | [`terraform/bootstrap/spec_signing.tf`](../../terraform/bootstrap/spec_signing.tf) | **the owner**, never CI: CI must not hold `setIamPolicy` on the key, or the release could grant itself a signature |
| the names of both, spelled once | [`terraform/modules/spec_signing_key`](../../terraform/modules/spec_signing_key/main.tf) | both roots |
| `SPEC_VERIFY_KEYS`, `SPEC_SIGNING_KEY`, `SPEC_SIGNATURE_MODE`, `SPEC_LEGACY_CUTOVER` on every Cloud Run worker Job and on the scheduler (which copies them onto every Job it creates, Cloud Run and GKE); the signing version (`output.spec_signing_key_version`), which #353 puts on swarm-api as `SPEC_SIGNING_KEY_VERSION` | [`terraform/infra/spec_signing.tf`](../../terraform/infra/spec_signing.tf), values in [`dev.tfvars`](../../terraform/environments/dev/dev.tfvars) | the release |
| the `swarm-spec-verify-keys` ConfigMap in each tenant namespace, the GKE pods' **fallback** | [`kubernetes/render.py`](../../kubernetes/render.py) from `terraform output -json spec_verify_keys_configmap`, applied by [`kubernetes/apply.sh`](../../kubernetes/apply.sh) `--spec-verify-keys` | the release's `deploy` job, when `vars.APPLY_TENANT_NAMESPACES` is `true` |
| the alert on a refusal | [`terraform/modules/monitoring`](../../terraform/modules/monitoring/alerts.tf), `spec-signature-invalid` | the release |

Nobody holds `roles/cloudkms.signerVerifier`, and no tenant or worker account
holds any role on the key. Workers get the public keys in their Job's
environment, on both backends, with a read-only mount as the fallback on GKE:
no KMS call, no quota and no IAM at attempt start.

**What this does not protect against**, accepted by the owner on 2026-09-29:
the key lives in the shared `saga-agents-staging` project, so a project Owner
or Editor can redeploy swarm-api to run their own code as its account, and sign
through it. It does hold against tenant agents, which is who #342 is about.

## The rollout

Each step is its own change, in this order. Never skip to `enforce` before
signed tasks are confirmed: a worker that enforces before swarm-api signs
refuses every task.

### 1. The key (owner, bootstrap), then the Terraform half (release)

The owner applies bootstrap's step-spec resources **before** the Terraform-half
pull request merges. terraform/infra reads the key's versions at plan; a
release planned before the key exists, or before the deployer can read it,
fails at plan.

```bash
cd terraform/bootstrap
terraform plan \
  -target='module.project_services[0].google_project_service.this["cloudkms.googleapis.com"]' \
  -target='google_kms_key_ring.step_spec' \
  -target='google_kms_crypto_key.step_spec' \
  -target='google_kms_crypto_key_iam_member.swarm_api_signer' \
  -target='google_kms_crypto_key_iam_member.deployer_key_readers' \
  -out=spec-signing.tfplan
terraform apply spec-signing.tfplan
terraform output spec_signing_keys
# version 1's public key, per environment (dev shown). Bootstrap no longer
# renders the PEMs itself: it creates the key, and reading each version's key
# in the same plan that creates it is a for_each over values unknown until
# apply, which fails the plan (#360). terraform/infra renders them, as
# `terraform output spec_verify_keys`, from its first release on.
gcloud kms keys versions get-public-key 1 --key step-spec \
  --keyring swarm-dev-specs --location us-central1
```

Read the plan for: two key rings and two keys (`dev`, `prod`), each key
`EC_SIGN_P256_SHA256` / `SOFTWARE`; `roles/cloudkms.signer` for
`swarm-api@saga-agents-staging.iam.gserviceaccount.com` on each key and nothing
else; `roles/cloudkms.publicKeyViewer` and `roles/cloudkms.viewer` for
`swarm-tf-deployer` on each key. Nothing on the project.

Then the Terraform-half pull request merges and releases. It is inert for
today's code: the worker reads none of these variables yet. It exists first so
the verifying worker ships onto Jobs that already carry the keys.
`SPEC_SIGNING_KEY_VERSION` is **not** in it: #353 adds it to swarm-api's
environment in the same change as the code that reads it, because
`scripts/lib/check-env-parity.sh` refuses a variable no code reads, and the
hardened swarm-api **refuses to start without it**.

dev ships `spec_signature_mode = "legacy"` with the cutover at
`2026-10-20T00:00:00Z` -- the end of the window -- so that when the verifying
worker ships in step 2 every unsigned task is admitted, and none of the tasks
parked at that moment fails.

**Check** (read-only): the worker Jobs carry the keys.

```bash
gcloud run jobs describe swarm-job-eng-claude-code --region us-central1 \
  --format=json | jq -r '.spec.template.spec.template.spec.containers[0].env[]
  | select(.name | startswith("SPEC_")) | "\(.name)=\(.value | .[0:80])"'
```

### 2. Signing and verifying (#353's release)

swarm-api signs every new task; every worker verifies. In `legacy`, an unsigned
task created before the cutover runs with a WARNING
(`unsigned task admitted by the legacy window`) and a RUNNING event whose
detail has `phase: verify_spec` and `spec_check.reason: legacy_unsigned`.

**Check** that signing works, before anything else moves:

* a new task's document carries `spec_signature`, `spec_key_version` (the full
  name of version 1) and `spec_format`;
* its worker logs `spec signature verified`;
* the `spec-signature-invalid` metric stays at zero.

If swarm-api answers 503 on submissions, KMS refused the signature: check the
`signer` grant and `SPEC_SIGNING_KEY_VERSION`.

### 3. Tighten the cutover (a pull request)

Set `spec_legacy_cutover` in `dev.tfvars` to the moment the signing swarm-api
revision took all traffic -- that revision's creation time, from

```bash
gcloud run services describe swarm-api --region us-central1 \
  --format='value(status.traffic)'
gcloud run revisions describe <that revision> --region us-central1 \
  --format='value(metadata.creationTimestamp)'
```

From that release on, a task created after the cutover with its signature
stripped is refused instead of admitted. `create_time` is Firestore's and no
client can write it.

`status.traffic` names only the revision serving now, not the first one that
signed: by 2026-10-01 seven later revisions had replaced it. Find the first
revision whose environment carries `SPEC_SIGNING_KEY_VERSION` instead:

```bash
gcloud run revisions list --service swarm-api --region us-central1 \
  --format=json | jq -r '.[] | [.metadata.name, .metadata.creationTimestamp,
    ([.spec.containers[0].env[]? | select(.name | startswith("SPEC_")) | .name] | join(","))]
    | join(" ")'
```

In dev that was `swarm-api-00119-lcs`, created 2026-09-30T21:24:36.572669Z by
#353's release; the service routes 100% to the latest ready revision
(`terraform/modules/cloud_run`), so it took all traffic within that release's
`terraform apply`.

### 4. Enforce (a pull request, before 2026-10-20)

Count the non-terminal tasks without a signature (read-only), and let them
finish or cancel and resubmit them. Resubmitting signs them. When none is
left, a pull request sets `spec_signature_mode = "enforce"` and removes
`spec_legacy_cutover`. After 2026-10-20 the worker enforces regardless, and any
unsigned task left fails `spec_signature_invalid` with reason `unsigned`.

### Prod

Prod enforces from its first release and never had a legacy window (owner decision 2026-10-06 on #355): no unsigned task was ever parked there, which is the only thing a window protects. `prod.tfvars` states `spec_signature_mode = "enforce"` explicitly rather than inheriting the default, and sets no `spec_legacy_cutover`. The worker's code cap (#739) refuses an unsigned task created after 2026-09-30T21:24:36Z on every platform, so even a prod `legacy` setting could not re-open the window.

## GKE

**A GKE pod gets the keys in its environment, from the scheduler, at Job
creation** (owner decision 2026-10-08). `GkeJobDispatcher._manifest` puts the
scheduler's own `SPEC_VERIFY_KEYS`, `SPEC_SIGNING_KEY`, `SPEC_SIGNATURE_MODE`
and `SPEC_LEGACY_CUTOVER` (`spec_job_env`, the values Terraform sets on the
scheduler) on the worker container's `env:`, exactly as on a Cloud Run Job the
scheduler creates. Never through `worker_env`, which a task shapes:
`tests/unit/control_plane/test_spec_signing_dispatch_env.py` holds both. They
are public keys, a KMS key name, a mode and a timestamp; no private material
exists to put there.

This reverses the original GKE design, which delivered them only through the
ConfigMap. That was enough while GKE ran browser alone in namespaces the
release applied. Once claude-code moved to GKE (PR 866) and onboarding (#847)
began creating a namespace per person, a namespace with no ConfigMap ended
every task `CANNOT_START`.

**The `swarm-spec-verify-keys` ConfigMap is the fallback**, mounted read-only
at `/etc/swarm/spec-verify-keys`. The worker reads it only when its
environment carries no `SPEC_VERIFY_KEYS` (a scheduler deployed without them);
otherwise the environment wins, all four settings. The release refreshes it
with every tenant namespace only when the repository variable
`APPLY_TENANT_NAMESPACES` is `true`; until then it says so and applies nothing.
By hand:

```bash
terraform -chdir=terraform/infra output -json spec_verify_keys_configmap > keys.json
kubernetes/apply.sh --tenant eng --spec-verify-keys keys.json          # preview
kubernetes/apply.sh --tenant eng --spec-verify-keys keys.json --confirm
```

`apply.sh` passes its `${ENVIRONMENT}` to `render.py`, which accepts only that
environment's key, `projects/<project>/locations/<region>/keyRings/swarm-<env>-specs/cryptoKeys/step-spec`
(#346). A `keys.json` from another environment, or naming any other key in
the shared project, is refused before anything is applied. Set `ENVIRONMENT`
(or `.env`) to match the Terraform state you read the output from.

The mount is `optional`: a namespace without the ConfigMap still starts its
pods and verifies with the keys in `env:`. Only a pod with keys in neither
place exits `CANNOT_START` for every task, signed or not, rather than run one
unverified. With keys, verification is unchanged: an unsigned spec outside the
legacy window, a bad signature or an unknown key version is still refused.
Nothing in the tenant namespace may write a ConfigMap. The worker Role is
empty and the pods carry no token, and
`tests/unit/worker/test_spec_verify_keys_configmap.py` holds that.

## Rotation

Cloud KMS does not rotate asymmetric keys; rotate by hand, in this order:

1. create version N+1: `gcloud kms keys versions create --key step-spec
   --keyring swarm-dev-specs --location us-central1`;
2. release, so every worker's `SPEC_VERIFY_KEYS` holds N and N+1: the
   Cloud Run Jobs and the scheduler, which puts its copy on every GKE Job it
   creates from then on. A GKE pod already running keeps the keys it was
   created with. The GKE ConfigMap (the fallback) follows only if that
   release's `deploy` job ran with `vars.APPLY_TENANT_NAMESPACES=true`;
   otherwise re-apply it by hand (see [GKE](#gke)) before step 4;
3. set `spec_signing_key_version = N+1` in `dev.tfvars` and release;
4. once no non-terminal task names version N, disable it
   (`gcloud kms keys versions disable N ...`) and release.

The plan warns (`check "spec_signing_version_is_trusted"`) when
`spec_signing_key_version` names a version that is not enabled.

## Revocation

Disable the version. From the next release on it is absent from every
worker's `SPEC_VERIFY_KEYS` (the Cloud Run Jobs, and the scheduler, which puts
its copy on every GKE Job it creates from then on), and every task signed by
it is refused as `foreign_key_version`. The GKE ConfigMap is the fallback and
drops the version only when that release's `deploy` job runs with
`vars.APPLY_TENANT_NAMESPACES=true`. Otherwise re-apply it by hand (see
[GKE](#gke)): a pod that falls back to a stale ConfigMap still trusts the
revoked version. A compromised signing identity could sign anything by
submitting it through the API anyway, so revocation closes the leak; a release
is fast enough for that.

## The alert

`swarm-<env>-spec-signature-invalid` fires on one refusal. It counts the
worker's ERROR line `spec signature invalid: refusing to run this task` with
`end_cause = spec_signature_invalid`, from Cloud Run Jobs and GKE pods alike,
labelled by tenant and `spec_check.reason`.

Only a **worker container's** line counts: a Cloud Run Job named
`swarm-job-*`, or the container named `worker` in the platform's own GKE
cluster. The tenant comes from that resource, which the platform sets --
`tenant_id` from a GKE pod's `swarm-tenant-<tenant>` namespace, `job_name`
from a Cloud Run Job's name -- never from the line's own `labels.tenant_id`.
That is because the agent, beside the worker as the same uid, can write a
JSON line into the container's log through tini's `/proc/1/fd/1`, and nothing
in a JSON payload is the worker's alone (#346, #354 security review). So an
agent cannot page in another tenant's name, and no other job or pod in the
shared project can page at all. It **can** still page from its own worker
container, under its own job or namespace: a page whose tenant ran an agent at
that moment, with no refused task in its history, is that agent, not a forged
spec. The half that covers contract
request 33's worker actions refusing an **upstream** spec (`MERGE_REFUSED` /
`VERDICT_REFUSED` with a reason starting `upstream:`) is added when CR 33 is
built.

## Not verified here

* Which KMS quota bucket an `AsymmetricSign` on a SOFTWARE EC P-256 key
  draws from, and its headroom against the other team's use of KMS. Every
  task and workflow step is one sign call; a `RESOURCE_EXHAUSTED` is a 503 on
  every submission. `gcloud services quota list` does not exist in GA gcloud
  (586.0.0 answers `Invalid choice: 'quota'`); read the limits with

  ```bash
  gcloud quotas info list --service=cloudkms.googleapis.com \
    --project=saga-agents-staging --format=json
  ```

  Read 2026-10-01: `crypto_requests` 60,000/min per project,
  `software_high_latency_requests` 600/min per region, `software_usage`
  6,000,000/min per region. The project's 7-day peak of
  `serviceruntime.googleapis.com/quota/rate/net_usage` for `software_usage`
  in us-central1 was 1,500 per minute (2026-10-01T01:03Z); no
  `software_high_latency_requests` usage was reported in that window. If
  the 600/min bucket is the one that applies, it is the binding limit.
* That the `state=ENABLED` filter of `google_kms_crypto_key_versions` is
  accepted by the live API as written. The module filters on `state` again, so
  a filter that matched too much would still trust only enabled versions; one
  the API rejected would fail the plan, loudly.
