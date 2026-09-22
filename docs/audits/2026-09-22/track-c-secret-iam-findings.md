# Two secret-IAM findings for Track C, and one decision recorded

Found on 2026-09-22 while closing the secret-version leak (`7fe7660`). Neither
is a change to Track C's files — both are reported here, per CLAUDE.md, rather
than patched.

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

## What was not verified

* No IAM policy was changed, and no secret version was created, deleted or
  read. Version counts, create times, policies and logs only.
* Version-add activity against the other team's `agents-*` secrets was not
  audited, so Finding 1 asserts a grant shape and nothing about its use.
* `FirestorePublishLedger` has not been exercised against real Firestore.
