# Workspace-secret grant attestation: turn it on, rotate the key

SEC-GRANT-ATTEST, part of #1347. Design:
[docs/design/workspace-secrets.md](../design/workspace-secrets.md), "What
SEC-GRANT-ATTEST built".

**Why it exists.** A workspace-secret grant
(`workspace_secret_grants/{secret_id}~{consumer}`) is a Firestore document,
and a tenant's agent can write Firestore documents whose id it can guess.
Without an attestation, an agent that learned one of its own workspace's
secret ids could write an enabled grant and have that secret released to its
next attempt. swarm-api now MACs every grant it writes under
`swarm-workspace-grant-key`, and the release drops any grant whose MAC does
not verify.

**Why it rolls out in stages.** No grant written before this change carries
an attestation, and `grant_attestation_required` drops unattested grants. Turn
it on first and every existing grant stops being released at once, so every
required slot in every workspace parks CREDENTIAL_MISSING. So the key is
served first, the existing grants are signed, and only then is the setting
turned on. A grant whose attestation is present but WRONG is dropped from step
2 on, whatever the setting says.

## Where each piece lives

| piece | where | applied by |
|---|---|---|
| the secret `swarm-workspace-grant-key` (no version) and swarm-api's `secretAccessor` on it, nobody else | [`terraform/infra/workspace_grant_key.tf`](../../terraform/infra/workspace_grant_key.tf) | the release (`make apply`) |
| its value | `scripts/create-secrets.sh --workspace-grant-key`: generated, never shown | an operator |
| `SWARM_WORKSPACE_GRANT_KEY`, `SWARM_WORKSPACE_GRANT_KEY_PREVIOUS`, `GRANT_ATTESTATION_REQUIRED` on swarm-api | `var.workspace_grant_attestation` (`serve`, `previous_version`, `required`) in `terraform/environments/<env>/<env>.tfvars` | the release |
| the migration | `POST /v1/admin/workspace-secrets/attest-grants` | a platform admin |

## Turn it on

1. **Create the container.** Merge and release the change. The plan adds
   exactly two resources, `google_secret_manager_secret.workspace_grant_key`
   and `google_secret_manager_secret_iam_binding.workspace_grant_key_accessor`,
   and changes swarm-api's revision by one environment variable,
   `GRANT_ATTESTATION_REQUIRED = "false"`. Nothing reads the key yet.

2. **Add the key, then serve it.**

   ```bash
   scripts/create-secrets.sh --workspace-grant-key
   ```

   Then set in the environment's tfvars and release:

   ```hcl
   workspace_grant_attestation = {
     serve = true
   }
   ```

   From this revision on, every grant a manager ticks or unticks is signed.
   Check that it took: `gcloud run services describe swarm-api --region
   <region> --format=json | jq '.spec.template.spec.containers[0].env[] |
   select(.name == "SWARM_WORKSPACE_GRANT_KEY") | .valueFrom'` names the
   secret at `latest`. It never shows the value.

3. **Sign the grants written before step 2.** As a platform admin:

   ```bash
   scripts/api.sh POST /admin/workspace-secrets/attest-grants
   ```

   (`scripts/api.sh` keeps the ID token out of argv.)

   The answer is `{examined, already_attested, signed, unsigned, attested_at}`,
   with grant ids only. A grant is signed only when its fields match its newest
   `workspace_secret_grant_enabled`/`_disabled` audit row. Each id in
   `unsigned` was written by something other than the store, or was changed
   after its audited write. Read every one: if a manager meant it, have them
   untick and re-tick it in the console, which signs it. Otherwise leave it.
   Once the setting is on, it is released nothing. Also read `signed` against
   the console's matrix: the route trusts `admin_audit`, which a hostile agent
   can also write (the design's "What still is not stopped").

   The route is safe to run again. A grant already signed under the current
   key is counted in `already_attested` and left alone.

4. **Require it.** Set and release:

   ```hcl
   workspace_grant_attestation = {
     serve    = true
     required = true
   }
   ```

   An unattested grant is now released nothing. The release logs each dropped
   grant as `grant_attestation_invalid grant=... tenant=... secret=...`. Before
   step 4, `grant_attestation_missing` lines name the unattested grants that
   are still being released. A day with none of them means step 3 left nothing
   behind.

**Rolling back** is step 4 in reverse (`required = false`), which brings back
the unattested path. Even rolled back, a grant with a wrong attestation is
never released. Do not unset `serve` while any grant is signed and
`required` is on: with no key, nothing verifies, so nothing is released.

## Rotate the key

The ring holds two keys. The current one signs and both verify, so a rotation
invalidates no grant while it is in progress.

1. Note the current version number (`gcloud secrets versions list
   swarm-workspace-grant-key --filter=state=ENABLED`), then add a new one:
   `scripts/create-secrets.sh --workspace-grant-key --overwrite
   swarm-workspace-grant-key`.
2. Set `previous_version` to the number you noted and release:

   ```hcl
   workspace_grant_attestation = {
     serve            = true
     required         = true
     previous_version = "<n>"
   }
   ```

3. Run the attest route (step 3 above). Every grant signed under the
   previous key is re-signed under the new one (`reason: previous_key` on its
   `workspace_secret_grant_attested` audit row).
4. Clear `previous_version`, release, then disable version `<n>`.

Skipping step 3 and clearing `previous_version` drops every grant the old key
signed.

**After revoking a sensitive grant**, rotate twice. A MAC covers a grant's
fields, not its history, so an agent that copied the grant while it was
enabled can write it back with its old attestation. That copy stops
verifying only once its key has left the ring.
