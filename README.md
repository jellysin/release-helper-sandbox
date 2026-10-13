# JellySin Release Helper Sandbox

This repository is a test target for [jellysin/release-helper](https://github.com/jellysin/release-helper).
It is not a plugin. Its tags, releases, attestations and plugin repository entries are test data, made by
automation to prove that release-helper's release path works against the real forge.

Nothing consumes this repository. Do not install its "plugin" in a Jellyfin server and do not list
`catalog/manifest.json` as a plugin repository.

## What runs here

The sandbox plays both roles that release-helper serves:

- a plugin repository: `plugin/` holds a fixture plugin descriptor and a fixture assembly, and
  `version.txt` the version of the next release;
- a plugin catalog: `catalog/manifest.json` and `catalog/plugins.json` are the manifest and the
  allowlist that release-helper's catalog action reads and extends.

A release cycle runs release-helper from one commit of its main branch: it records the next version,
creates the tag and the draft with the prepare action, packages, attests and publishes the release on
that tag, imports it into the catalog with the catalog action and checks the result. The workflows
under `.github/workflows` hold the details.

## Write access

Every workflow writes with its own job token, scoped to this repository. No token of another
repository is stored here.

## Licence

EUPL-1.2, see [LICENSE](LICENSE).
