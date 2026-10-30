# Publish a SwarmCloud release, then promote it

Since BGD's "Move publishing into CI now" (2026-10-30, DAYONE-RELEASE-CI, part of
#2688), CI publishes a release, not a laptop. A person still decides every step
that matters: they dispatch the run, they approve it, and they promote by hand.

The release home is project `swarmcloud-prod`: images in
`us-docker.pkg.dev/swarmcloud-prod/swarmcloud`, the bundle in
`gs://swarmcloud/releases/<version>/`, signed with the KMS key
`gcpkms://projects/swarmcloud-prod/locations/us/keyRings/swarmcloud-release/cryptoKeys/cosign`.

## Why it is split this way

- **Publish is CI's.** `.github/workflows/publish-release.yml` assembles the
  release from one main commit and the build record that commit's `release`
  run scanned, copies every image by digest, signs it and uploads the bundle.
  No laptop holds the bundle or the tools, and every publish is a run with a
  log.
- **The approval is the confirmation.** At a terminal, `publish.sh` asks for
  the version typed. In CI there is no terminal: the version typed into the
  dispatch form reaches `publish.sh --ci` as `SWARM_PUBLISH_VERSION`, and the
  `release` environment's reviewer approves the run. `--ci` is refused outside
  GitHub Actions, and refused unless that version is exactly the bundle's.
- **Promote stays a person's typed act.** Moving `channels/stable` is what
  every company's next install reads, so it waits for the manual install and
  upgrade test, and `--promote` takes no `--ci`.
- **CI's identity holds three roles, each on one resource, none on the
  project** (`scripts/release-home.sh --ci-publisher`): enough to write a new
  release, not enough to change the release home's IAM or its key.

## Once: let CI publish

1. Grant the workflow's identity (`vars.GCP_DEPLOY_SA`, the one `release.yml`
   uses) the three release roles. Read them first, then type the project id:

   ```bash
   SA="$(gh variable get GCP_DEPLOY_SA)"
   scripts/release-home.sh --ci-publisher "${SA}" --dry-run
   scripts/release-home.sh --ci-publisher "${SA}"
   ```

   It grants, in `swarmcloud-prod`, `roles/artifactregistry.writer` on the
   repository `swarmcloud`, `roles/storage.objectAdmin` on `gs://swarmcloud`
   and `roles/cloudkms.signerVerifier` on `swarmcloud-release/cosign`. A second
   run grants nothing.

2. In the repository's settings, create the environment `release`: a required
   reviewer (BGD), and deployment branches limited to `main`.

3. Let the deployer's trust admit the workflow. `publish` authenticates as
   `vars.GCP_DEPLOY_SA` through `attribute.job_workflow_ref`, and the deployer
   trusts only the files in `terraform/bootstrap/wif.tf` `deployer_workflows`;
   until `publish-release.yml` is applied there, its auth step fails with
   `Permission 'iam.serviceAccounts.getAccessToken' denied`, having published
   nothing. The bootstrap root is applied by a person, on BGD's yes, with
   [deployer-trust-pin.md](deployer-trust-pin.md): the plan shows one member
   added, `deployer_wif["publish-release.yml@refs/heads/main"]`, and
   `scripts/verify-deployer-trust.sh` then ends `checked 7 members, all pinned
   to workflow files`.

## Each release

1. Pick the commit: a main commit whose `release` run succeeded. The run must
   still hold its `scanned-images-dev` artifact (kept 30 days). The run
   assembles and publishes with that commit's own scripts, so the commit must
   be one whose `publish.sh` takes `--ci` (DAYONE-RELEASE-CI or later).

   ```bash
   gh run list --workflow release.yml --branch main --status success --limit 5
   ```

2. Dispatch, with the version (`YYYY.MM.N`, or a candidate `YYYY.MM.N-rc.N`)
   and the full commit SHA:

   ```bash
   gh workflow run publish-release.yml -f version=2026.11.1 -f commit=<40-character sha>
   ```

   `check` refuses a malformed version, a commit not on main, or one with no
   successful release run holding the record, before anyone is asked to
   approve.

3. Approve the `release` environment on the run's page. That is the
   confirmation. The run publishes under `releases/<version>/`, SHA256SUMS last;
   a version already published is refused, never overwritten.
   A candidate (`-rc.N`) then moves `channels/rc` to itself, after SHA256SUMS, so
   `SWARM_CHANNEL=rc` installs it; every bundle carries
   `github-app-permissions.json` (from `swarm_api.setup_github`), which
   `swarm upgrade` compares with the live App's permissions.

4. Run the manual install-and-upgrade test (plan section 7.3) against the
   published release, then record it and promote, typed, at a terminal with
   rights in the release home:

   ```bash
   scripts/release/publish.sh --record-test 2026.11.1 --record tested.json
   scripts/release/publish.sh --promote 2026.11.1
   ```

   `--promote` refuses a release with no passing test record, and moves
   `marketplace.json`, `install.sh` and, last, `channels/stable`.

## If the run fails

- **Before the approval** (`check`): the error names the input. Dispatch again
  with a correct one.
- **During the publish**: a release cut short has no `SHA256SUMS`, so no
  installer reads it, but `releases/<version>/` exists and a re-run of the same
  version is refused. Publish the next version (`-rc.N+1`, or `N+1`).
