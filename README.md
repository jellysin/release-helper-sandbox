# JellySin Release Helper Sandbox

<!-- praetor:readme-governance:start -->
[![HISS Adopted][praetor-hiss-badge]][praetor-hiss-agents]

Praetor manages this repository's declared governance policy. This managed
block records adoption state; it is not a verification certificate.

**Verification**: `make verify-all` runs the repository's configured
verification cascade.

**HISS Audit**: `praetorctl audit` enforces policy, generated-surface
integrity, and the debt ratchet.

**Context Sync**: `praetorctl compile-context --verify` verifies every
generated agent context against `AGENTS.md`.

**Debt Baseline**: `.standards-baseline.json` anchors the debt ratchet at
0 recorded infractions; audit forbids growth.

[praetor-hiss-badge]: https://img.shields.io/badge/Standards-HISS%20Adopted-blue
[praetor-hiss-agents]: https://github.com/jellysin/release-helper-sandbox/blob/HEAD/AGENTS.md
<!-- praetor:readme-governance:end -->

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

A release cycle runs release-helper from one commit of its main branch. It pushes a releasable commit,
lets release-helper's reusable release-please workflow open the release PR and merges it, creates the
tag and the draft with the prepare action, packages, attests and publishes the release on that tag,
imports it into the catalog with the catalog action, checks the result, and runs the release-please
workflow again to see the merged PR marked tagged. The reusable workflow is called at the commit
release-helper's consumers pin. The workflows under `.github/workflows` hold the details.

## Write access

Every workflow writes with its own job token, scoped to this repository. No token of another
repository is stored here.

## Licence

EUPL-1.2, see [LICENSE](LICENSE).
