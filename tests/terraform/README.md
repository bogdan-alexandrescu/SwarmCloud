# terraform tests

Native `terraform test` suites for `terraform/`. Run them from this directory:

    ~/.local/bin/terraform -chdir=tests/terraform init
    ~/.local/bin/terraform -chdir=tests/terraform test

Every run block is `command = plan` against a `mock_provider "google"`, so the
whole suite runs offline, creates nothing and needs no credentials.

Mocking the provider is not only about speed. A mock provider returns every
computed attribute as unknown, which is exactly the state of a FIRST apply
against an empty project -- so these tests fail on the class of bug that only
ever shows up on day one: a `for_each` or a `count` derived from a value that
does not exist yet.
