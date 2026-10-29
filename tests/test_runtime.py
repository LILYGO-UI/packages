from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.build_apt import build_apt_repository
from scripts.build_pages import build_pages
from scripts.registry_common import (
    release_download_url,
    validate_pull_request,
    validate_registry,
)
from tests.test_registry import make_registry


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def make_runtime(root: Path, *, owner: str = "alice", architecture: str = "arm64") -> Path:
    source = Path(__file__).resolve().parents[1]
    for schema in (
        "runtime.schema.json",
        "runtime-release.schema.json",
        "registry-root.schema.json",
        "registry-index.schema.json",
    ):
        shutil.copy2(source / schema, root / schema)

    package = "meshtasticd"
    version = "2.8.0.0~local4528018+lr2021cad1"
    digest = "e" * 64
    asset = f"sha256-{digest}.deb"
    source_repository = f"{owner}/packages"
    source_tag = f"lpm-staging-{package}-{version}-eeeeeeeeeeee"
    central_tag = f"apt-pool-{package}"
    runtime = {
        "$schema": "https://raw.githubusercontent.com/LILYGO-UI/packages/main/runtime.schema.json",
        "schema_version": 1,
        "package": package,
        "owners": [owner],
    }
    write_json(root / f"runtimes/{package}/runtime.json", runtime)
    release = {
        "$schema": "https://raw.githubusercontent.com/LILYGO-UI/packages/main/runtime-release.schema.json",
        "schema_version": 1,
        "status": "published",
        "submitted_by": owner,
        "manifest": {
            "$schema": "https://raw.githubusercontent.com/LILYGO-UI/packages/main/runtime-release.schema.json",
            "schema_version": 1,
            "package": package,
            "version": version,
            "title": "Meshtastic daemon",
            "summary": "Meshtastic daemon runtime",
            "description": "Meshtastic daemon for communicating with Meshtastic devices.",
            "license": "GPL-3.0",
            "source_repo": "https://github.com/meshtastic/firmware",
            "homepage": "https://github.com/meshtastic/firmware",
            "compatibility": {"architectures": [architecture]},
        },
        "artifact": {
            "architecture": architecture,
            "size": 1234,
            "sha256": digest,
            "media_type": "application/vnd.debian.binary-package",
            "source": {
                "repository": source_repository,
                "tag": source_tag,
                "asset": asset,
                "url": release_download_url(source_repository, source_tag, asset),
            },
            "central": {
                "repository": "LILYGO-UI/packages",
                "tag": central_tag,
                "asset": asset,
                "url": release_download_url("LILYGO-UI/packages", central_tag, asset),
            },
        },
    }
    release_path = root / f"runtimes/{package}/releases/{version}.json"
    write_json(release_path, release)
    return release_path


class RuntimeRegistryTests(unittest.TestCase):
    def test_runtime_release_is_accepted_by_pull_request_validation(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = Path(__file__).resolve().parents[1]
            for schema in (
                "runtime.schema.json",
                "runtime-release.schema.json",
            ):
                shutil.copy2(source / schema, root / schema)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Runtime Test"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "runtime@example.com"], cwd=root, check=True)
            subprocess.run(["git", "add", "runtime.schema.json", "runtime-release.schema.json"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
            base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
            make_runtime(root)
            subprocess.run(["git", "add", "runtimes"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "runtime"], cwd=root, check=True)
            changed = validate_pull_request(root, base, "alice", schema_root=root)
            self.assertIn("runtimes/meshtasticd/runtime.json", changed)
            self.assertIn(
                "runtimes/meshtasticd/releases/2.8.0.0~local4528018+lr2021cad1.json", changed
            )

    def test_runtime_release_validates_without_app_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            release_path = make_runtime(root)
            releases = validate_registry(root)
            self.assertEqual(len(releases), 1)
            self.assertEqual(releases[0].kind, "runtime")
            self.assertEqual(releases[0].path, release_path)

    def test_runtime_manifest_allows_minimal_lpm_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            release_path = make_runtime(root)
            release = json.loads(release_path.read_text(encoding="utf-8"))
            for field in ("title", "summary", "description", "license", "source_repo", "homepage"):
                release["manifest"].pop(field)
            write_json(release_path, release)
            self.assertEqual(len(validate_registry(root)), 1)

    def test_runtime_is_indexed_by_apt_and_omitted_from_appstore_pages(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            make_runtime(root)
            output = root / "public"

            def fake_download(item, destination: Path) -> None:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"fake deb payload")

            control = [
                "Package: meshtasticd",
                "Version: 2.8.0.0~local4528018+lr2021cad1",
                "Architecture: arm64",
                "Maintainer: LILYGO <support@lilygo.cc>",
                "Description: Meshtastic daemon for communicating with Meshtastic devices",
                " Meshtastic is an off-grid text communication platform.",
            ]
            with (
                patch("scripts.build_apt._download_artifact", side_effect=fake_download),
                patch("scripts.build_apt._control_record", return_value=control),
            ):
                build_apt_repository(
                    root,
                    output,
                    generated_at="2026-10-28T12:00:00+08:00",
                )

            packages = (output / "dists/stable/main/binary-arm64/Packages").read_text()
            self.assertIn("Package: meshtasticd", packages)
            self.assertIn("Architecture: arm64", packages)

            build_pages(
                root,
                output,
                base_url="https://lilygo-ui.github.io/packages",
                snapshot_id="a" * 40,
                generated_at="2026-10-28T12:00:00+08:00",
            )
            index = json.loads(
                (output / f"v1/snapshots/{'a' * 40}/index.json").read_text()
            )
            self.assertEqual(index["apps"], [])

    def test_mixed_application_and_runtime_records_share_apt_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            make_registry(root)
            make_runtime(root)
            output = root / "public"

            def fake_download(item, destination: Path) -> None:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"fake deb payload")

            def control(path: Path) -> list[str]:
                if "meshtasticd" in path.as_posix():
                    return [
                        "Package: meshtasticd",
                        "Version: 2.8.0.0~local4528018+lr2021cad1",
                        "Architecture: arm64",
                        "Maintainer: LILYGO <support@lilygo.cc>",
                        "Description: Meshtastic daemon for communicating with Meshtastic devices",
                    ]
                return [
                    "Package: lilygo-ui-demo",
                    "Version: 1.2.3",
                    "Architecture: arm64",
                    "Maintainer: Alice <alice@example.com>",
                    "Description: Demo application",
                ]

            with (
                patch("scripts.build_apt._download_artifact", side_effect=fake_download),
                patch("scripts.build_apt._control_record", side_effect=control),
            ):
                build_apt_repository(root, output, generated_at="2026-10-28T12:00:00+08:00")

            packages = (output / "dists/stable/main/binary-arm64/Packages").read_text()
            self.assertIn("Package: lilygo-ui-demo", packages)
            self.assertIn("Package: meshtasticd", packages)

            build_pages(
                root,
                output,
                base_url="https://lilygo-ui.github.io/packages",
                snapshot_id="b" * 40,
                generated_at="2026-10-28T12:00:00+08:00",
            )
            index = json.loads(
                (output / f"v1/snapshots/{'b' * 40}/index.json").read_text()
            )
            self.assertEqual([app["package"] for app in index["apps"]], ["lilygo-ui-demo"])


if __name__ == "__main__":
    unittest.main()
