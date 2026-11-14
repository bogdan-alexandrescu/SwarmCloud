# Placeholder deployment tfvars

A deployment's real `dev.tfvars`, `prod.tfvars` and bootstrap `terraform.tfvars`
are private: they live under the state bucket's `config/` prefix and are never
tracked here (docs/ci.md says where CI fetches them from). Tests that hold a
property of a deployment's tfvars read the tracked `*.tfvars.example` beside
each root. The two files here are for the properties an example cannot hold
generically:

* `dev.tfvars.fixture`: a deployment's capacity (`pool_limits`, the tenants'
  limits) and its identities (the smoke, swarm-verify and oracle-api tenants,
  `allowed_users`, `admin_pool_users`), and the ceilings docs/scaling.md states.
* `bootstrap.tfvars.fixture`: the IAP accessors those identities need, and the
  deployer-scoping record a deployment keeps beside `deployer_scoped_roles`.

Placeholders only (`example-project`, `example.com`). The suffix is
`.tfvars.fixture`, not `.tfvars`, so neither the deployment-values guard (C10)
nor tests/unit/terraform/test_no_tracked_tfvars.py reads them as a tracked
deployment file.
