# Three secret-IAM findings for Track C, and one decision recorded

Findings 1 and 2 were found on 2026-09-22 while closing the secret-version leak
(`7fe7660`). Finding 3 was added the same day while adding retention to the
publish path, which is the half of that leak `7fe7660` did not close. None of
them is a change to Track C's files — all three are reported here, per
CLAUDE.md, rather than patched.

---

## Finding 1 — `swarmSecretLister` grants `versions.add` project-wide

`terraform/modules/iam/custom_roles.tf:139-175`.

The role's own description says:

> "List secret metadata project-wide, and create the two secrets an account
> pool entry needs. **Cannot read any payload.**"

The second sentence is true and carefully arranged: `secretmanager.versions.access`
is absent, deliberately, with a comment explaining that payload access stays
per-secret so "a bug here cannot widen into reading tenant keys". That reasoning
is sound and should not be undone.

**What the description does not say is that `secretmanager.versions.add` is
granted at the project level.** A custom role's permissions apply to every
secret in the project the role is bound on. So the grant is not "create the two
secrets an account pool entry needs" — it is *write a new version onto any
secret in `saga-agents-staging`*.

`saga-agents-staging` is shared with another team. Their `agents-*` secrets are
in scope of that grant today.

**Why this is worth writing down even though nothing has exercised it.** The
asymmetry is the point: the role is scrupulous about *reading* (per-secret,
argued in a comment) and casual about *writing* (project-wide, unmentioned).
A new version on a secret is not a read, but for a credential it is equivalent
to overwriting it — the consumer picks up `latest`. The blast radius of an
accidental write is therefore larger than the blast radius of the read the
comment spends a paragraph preventing.

It is also not on the deny-list. `scripts/lib/common.sh` stops us naming,
modifying or deleting the other team's resources; it does not and cannot stop
an IAM role we granted ourselves from permitting a write to them.

**Not claimed:** that this has ever happened. Version-add activity on `agents-*`
secrets was not audited. This is a grant-shape finding, not an incident.

**Suggested shape, for Track C to accept or reject:** bind `versions.add` per
secret in the `secret_manager` module the way `accessor` already is, and leave
`swarmSecretLister` with `secrets.list` plus the provisioning permissions only.
That keeps registration working — `_provision` creates the secret and binds it
in one operation, so it can bind `versionAdder` on the secret it just made — and
removes the project-wide write.

---

## Finding 2 — `usagepoll.py` has never recorded a reading in this deployment

`apps/quota-broker/quota_broker/usagepoll.py:94` calls `access()` on the same
base secrets the refresher cannot read, and logs `PermissionDenied` every five
minutes.

It fails *safe* — a failed read records nothing rather than recording a wrong
number — so this is not a correctness bug. But the consequence is that
`record_reading` has never been called for the two older `u-bogdan` accounts,
which means:

* headroom for those accounts has read **full**, always, for the life of the
  deployment; and
* `may_serve`'s assign floor has never had a reading to fire on.

So a panel that looks healthy is reporting an absence of measurement, not
measured health — the exact distinction the Accounts drawer copy is careful
about elsewhere. Worth a `—` rather than a full bar wherever that surfaces.

This resolves the same way Finding 1's accessor question does, and is listed
separately because it has a user-visible consequence that closing the leak does
not fix.

---

## Decision recorded — the accessor grant was considered and declined

The leak's root cause is that the broker cannot read the base secret, so it
cannot compare before publishing. Granting the broker
`roles/secretmanager.secretAccessor` on those secrets would remove the cause
outright and reduce the residual to zero.

**Decided 2026-09-22: do not grant it.** Keep the invariant.

The reasoning, so this is not silently reverted later:

* `terraform/modules/secret_manager/main.tf:109` — the `accessor` binding is
  `google_secret_manager_secret_iam_binding`, which is *authoritative*, and it
  lists exactly one member. Adding `var.refresher_member` breaks the stated
  invariant **"exactly one identity may read each secret"** and the passing test
  `a_provider_key_is_readable_by_exactly_one_identity`.
* `7fe7660` already bounds the damage to **at most one unverified write per
  (secret, token), for the life of the ledger**. That is not zero, and saying so
  plainly is the point — but it is not a cadence, which is what made the leak a
  leak.
* It self-corrects going forward. Since `6b7a3dd` (2026-09-20) `_provision`
  grants the broker accessor on the base secret at registration.
  `swarm-account-eng--team` has that binding and has **1 version**. Only the two
  `u-bogdan` secrets, which predate it, lack it.

**The cheaper cure, if zero is ever wanted:** `scripts/create-secrets.sh
--subscription` writes only the `-refresh` half, by design. That asymmetry is
the *sole* reason the still-valid publish path exists at all. If it wrote both
halves, `UNVERIFIED_NEEDED` could be deleted outright — no IAM change, invariant
preserved. Track D owns that script, so it is ours to do if the residual ever
stops being acceptable.

---

## Finding 3 — the broker cannot expire a secret version, and retention needs it

**This is a request for a binding, not a change to Track C's files.** Nothing in
`terraform/` was edited.

### The problem the binding is for

Closing the leak (`7fe7660`) stopped the platform writing *identical* versions.
It did not expire anything. Every LEGITIMATE refresh still appends a version and
nothing has ever destroyed or disabled one: `swarm-tenant-u-bogdan-anthropic`
held **1,816 versions, all ENABLED, zero destroyed** on 2026-09-22.

Only `latest` is ever read — `agent_worker.secrets.SecretManagerClient.access`
defaults to it, `quota_broker.secretstore.SecretManagerStore.access` pins
`/versions/latest`, and the Cloud Run mount `scheduler.dispatch` builds sets
`version="latest"`. So 1,815 of those versions had no consumer, and every one of
them stayed **retrievable** by anything holding `secretAccessor` on the secret.
That is the same principle `scripts/create-secrets.sh:226` already states: a
credential that rotates but leaves its predecessor enabled has not rotated.

The refresh cadence itself is correct and is not being changed — see
`sweep_accounts` for why idle accounts are refreshed on purpose. At one exchange
every ~5 hours per account, the steady state is ~1,752 new versions a year per
account, forever, and the only lever is expiring what they supersede.

### What was added, and how it behaves until the binding exists

`quota_broker.secretstore.SecretManagerStore.add_version` now destroys every
ENABLED version older than the newest three, guarded so that it can only ever
act on `swarm-tenant-*` / `swarm-account-*` names, never on the version the
publish just wrote, and never down to zero enabled versions.

**It fails safe and loudly.** Retention runs after the write and cannot fail it:
a `PermissionDenied` is logged once per secret per process, naming the two
missing permissions, and the publish succeeds. The retained set is recomputed
from a live listing on every publish, with no marker recording that a secret was
pruned, so the moment the binding lands the next publish catches up — no
backfill, no migration, no redeploy required for correctness.

### Exactly what is missing, and on what

Read from the live policies on 2026-09-22 (read-only; nothing was changed):

| Grant | Where | Permissions relevant here |
|---|---|---|
| `projects/saga-agents-staging/roles/swarmSecretLister` | project | `secrets.create/get/list/getIamPolicy/setIamPolicy`, `versions.add` |
| `roles/secretmanager.secretVersionAdder` | per secret, both halves | `versions.add`, `secrets.rotate` |
| `roles/secretmanager.secretAccessor` | per secret, `-refresh` half only | `versions.access` |

`swarm-quota-broker@saga-agents-staging.iam.gserviceaccount.com` therefore holds
**neither `secretmanager.versions.list` nor `secretmanager.versions.destroy`**,
anywhere. Both are needed: the retention pass lists the versions of the secret it
just wrote and destroys the ones past the newest three.

**Requested, for Track C to accept or reject:** grant the broker
`secretmanager.versions.list` and `secretmanager.versions.destroy`
**per secret**, in `terraform/modules/secret_manager/main.tf`, on the
`swarm-tenant-*` and `swarm-account-*` secrets this platform creates — alongside
the existing `version_adder` binding, and on the `-refresh` half too, which grows
at the same rate.

Three notes on the shape:

* **Per secret, not project-wide.** Finding 1 above objects to
  `swarmSecretLister` granting `versions.add` across a shared project; adding
  `versions.destroy` to that role would repeat the same mistake with a strictly
  worse blast radius. The code's name guard refuses to act outside this
  platform's names, but a guard in one component is not a substitute for a grant
  that cannot reach another team's secrets in the first place.
* **`roles/secretmanager.secretVersionManager` would cover it and is wider than
  needed.** It adds `versions.enable`, `versions.disable` and `versions.get`,
  none of which this uses. A two-permission custom role is the closer fit.
  (Incidentally: `terraform/bootstrap/variables.tf:178-190` says every role in
  that refusal list "confers `secretmanager.versions.access`, directly or by
  inheritance". That is true of `secretmanager.admin` and `secretAccessor`, but
  the live definition of `secretVersionManager` does **not** include
  `versions.access`. The role still does not belong on a CI identity — project-
  wide destroy is reason enough — but the stated reason is not the real one, and
  that is Track C's line to correct or keep.)
* **`version_destroy_ttl` is already `86400s`** (`modules/secret_manager/
  variables.tf:43`), which is the right pairing: a destroyed version becomes
  unreadable immediately, so the security property lands at once, and stays
  restorable for a day if a retention pass ever turns out to be wrong.

### Not claimed

No IAM policy was changed and no secret version was created, read or destroyed
while establishing any of the above. The retention code has been exercised
against a fake client only; it has never run against Secret Manager, because the
permission to do so does not exist yet.

---

## What was not verified

* No IAM policy was changed, and no secret version was created, deleted or
  read. Version counts, create times, policies and logs only.
* Version-add activity against the other team's `agents-*` secrets was not
  audited, so Finding 1 asserts a grant shape and nothing about its use.
* `FirestorePublishLedger` has not been exercised against real Firestore.
