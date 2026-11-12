# plugin.home in the discovery document (discovery.tf, variables.tf): the
# repository the plugin is served from, as owner/name, so the user guide's
# Start here page can fill its install command. It is set from its own
# variable, discovery_plugin_home, not a field of var.discovery: the install
# engine writes the whole discovery object and would overwrite it. Empty by
# default, and a value that is not owner/name is refused at plan time rather
# than served to every plugin.

mock_provider "google" {
  source = "./mocks/google"
}

variables {
  project_id  = "example-project"
  environment = "dev"

  image_refs = {
    "swarm-api"                = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-api@sha256:1111111111111111111111111111111111111111111111111111111111111111"
    "swarm-scheduler"          = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-scheduler@sha256:2222222222222222222222222222222222222222222222222222222222222222"
    "swarm-quota-broker"       = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-quota-broker@sha256:3333333333333333333333333333333333333333333333333333333333333333"
    "swarm-reconciler"         = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-reconciler@sha256:4444444444444444444444444444444444444444444444444444444444444444"
    "swarm-ui"                 = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-ui@sha256:5555555555555555555555555555555555555555555555555555555555555555"
    "swarm-verify"             = "us-central1-docker.pkg.dev/example-project/swarm-images/swarm-verify@sha256:6666666666666666666666666666666666666666666666666666666666666666"
    "agent-runtime-base"       = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-base@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    "agent-runtime-browser"    = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-browser@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    "agent-runtime-indexer"    = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-indexer@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    "agent-runtime-lightpanda" = "us-central1-docker.pkg.dev/example-project/swarm-images/agent-runtime-lightpanda@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
  }
}


run "the_plugin_home_reaches_swarm_discovery" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_frontend   = true
    frontend_hostname = "swarm.example.test"
    discovery = {
      oauth_client_id    = "123456789012-example.apps.googleusercontent.com"
      version            = "2026.11.0"
      plugin_min_version = "2026.11.0"
    }
    discovery_plugin_home = "example-org/example-repo"
  }

  assert {
    condition = one([
      for e in google_cloud_run_v2_service.discovery[0].template[0].containers[0].env : e.value
      if e.name == "DISCOVERY_PLUGIN_HOME"
    ]) == "example-org/example-repo"
    error_message = "swarm-discovery must read DISCOVERY_PLUGIN_HOME from var.discovery_plugin_home"
  }
}

run "the_plugin_home_defaults_to_empty" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    enable_frontend   = true
    frontend_hostname = "swarm.example.test"
    discovery = {
      oauth_client_id    = "123456789012-example.apps.googleusercontent.com"
      version            = "2026.11.0"
      plugin_min_version = "2026.11.0"
    }
  }

  assert {
    condition     = var.discovery_plugin_home == ""
    error_message = "discovery_plugin_home defaults to empty, so the document omits plugin.home and the guide keeps its placeholder"
  }

  assert {
    condition = length([
      for e in google_cloud_run_v2_service.discovery[0].template[0].containers[0].env : e
      if e.name == "DISCOVERY_PLUGIN_HOME"
    ]) == 0
    error_message = "an unset discovery_plugin_home renders no DISCOVERY_PLUGIN_HOME, so the document omits plugin.home"
  }
}

run "a_plugin_home_with_no_owner_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    discovery_plugin_home = "example-repo"
  }

  expect_failures = [var.discovery_plugin_home]
}

run "a_plugin_home_with_no_name_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    discovery_plugin_home = "example-org/"
  }

  expect_failures = [var.discovery_plugin_home]
}

run "a_plugin_home_with_no_owner_before_the_slash_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    discovery_plugin_home = "/example-repo"
  }

  expect_failures = [var.discovery_plugin_home]
}

run "a_plugin_home_with_a_path_past_the_name_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    discovery_plugin_home = "example-org/example-repo/extra"
  }

  expect_failures = [var.discovery_plugin_home]
}

run "a_plugin_home_with_a_url_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    discovery_plugin_home = "https://github.com/example-org/example-repo"
  }

  expect_failures = [var.discovery_plugin_home]
}

run "a_plugin_home_with_a_space_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    discovery_plugin_home = "example org/example-repo"
  }

  expect_failures = [var.discovery_plugin_home]
}

run "a_plugin_home_with_an_owner_starting_with_a_hyphen_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    discovery_plugin_home = "-example-org/example-repo"
  }

  expect_failures = [var.discovery_plugin_home]
}

run "a_plugin_home_with_a_dot_dot_name_is_refused" {
  command = plan

  module {
    source = "../../terraform/infra"
  }

  variables {
    discovery_plugin_home = "example-org/.."
  }

  expect_failures = [var.discovery_plugin_home]
}
