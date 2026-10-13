"""check-cycle.py TAG: the cycle's release is published, immutable and complete, and the catalog lists it.

Standard library and gh only. Exits non-zero naming the first thing that does not hold.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = os.environ["GITHUB_REPOSITORY"]
PLUGIN = json.loads(Path("plugin/plugin.json").read_text(encoding="utf-8"))


def gh(path):
    done = subprocess.run(["gh", "api", path], capture_output=True, check=False, timeout=60)
    if done.returncode:
        raise SystemExit(f"gh api {path} failed")
    return json.loads(done.stdout)


def main(tag):
    version = tag.removeprefix("v")
    name = REPO.split("/")[1]
    release = gh(f"repos/{REPO}/releases/tags/{tag}")
    if release.get("draft") is not False or release.get("immutable") is not True:
        raise SystemExit(f"{tag}: release is not published and immutable")
    assets = sorted(asset["name"] for asset in release.get("assets", []))
    expected = sorted([f"{name}-{version}.zip", "release.json", "sbom.spdx.json", "checksums.txt"])
    if assets != expected:
        raise SystemExit(f"{tag}: release assets {assets}, expected {expected}")
    manifest = json.loads(Path("catalog/manifest.json").read_text(encoding="utf-8"))
    entries = [entry for entry in manifest if entry["guid"] == PLUGIN["guid"]]
    versions = {item["version"]: item for item in entries[0]["versions"]} if entries else {}
    entry = versions.get(f"{version}.0")
    source = f"https://github.com/{REPO}/releases/download/{tag}/{name}-{version}.zip"
    if entry is None or entry["sourceUrl"] != source:
        raise SystemExit(f"{tag}: the catalog lists no entry {version}.0 from {source}")
    print(f"{tag}: published, immutable, {len(assets)} assets; catalog lists {version}.0 ({len(versions)} versions)")


if __name__ == "__main__":
    main(sys.argv[1])
