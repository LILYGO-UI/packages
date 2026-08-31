from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from jsonschema import Draft202012Validator

from scripts.build_pages import build_pages
from scripts.promote_release import verify_central_assets
from scripts.registry_common import (
    RegistryError,
    ReleaseFile,
    _read_debian_control_fields,
    _validate_debian_archive,
    debian_version_compare,
    release_download_url,
    validate_pull_request,
    validate_registry,
)

SCHEMAS = (
    "publish.schema.json",
    "app.schema.json",
    "registry-release.schema.json",
    "registry-root.schema.json",
    "registry-index.schema.json",
    "registry-app.schema.json",
)


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def make_registry(root: Path, *, owner: str = "alice") -> tuple[Path, Path]:
    source = Path(__file__).resolve().parents[1]
    for schema in SCHEMAS:
        shutil.copy2(source / schema, root / schema)
    icon_content = b"small png icon"
    screenshot_content = b"small webp screenshot"
    icon_hash = hashlib.sha256(icon_content).hexdigest()
    screenshot_hash = hashlib.sha256(screenshot_content).hexdigest()
    icon = root / f"assets/sha256/{icon_hash[:2]}/{icon_hash}.png"
    screenshot = root / f"assets/sha256/{screenshot_hash[:2]}/{screenshot_hash}.webp"
    icon.parent.mkdir(parents=True)
    screenshot.parent.mkdir(parents=True)
    icon.write_bytes(icon_content)
    screenshot.write_bytes(screenshot_content)

    app = {
        "$schema": "https://raw.githubusercontent.com/LILYGO-UI/packages/main/app.schema.json",
        "schema_version": 1,
        "package": "lilygo-ui-demo",
        "app_id": "cc.lilygo.ui.Demo",
        "owners": [owner],
    }
    write_json(root / "apps/lilygo-ui-demo/app.json", app)
    digest = "d" * 64
    asset_name = f"sha256-{digest}.deb"
    source_repository = f"{owner}/packages"
    release = {
        "$schema": "https://raw.githubusercontent.com/LILYGO-UI/packages/main/registry-release.schema.json",
        "schema_version": 1,
        "status": "published",
        "submitted_by": owner,
        "manifest": {
            "$schema": "https://raw.githubusercontent.com/LILYGO-UI/packages/main/publish.schema.json",
            "schema_version": 1,
            "package": "lilygo-ui-demo",
            "version": "1.2.3",
            "app_id": "cc.lilygo.ui.Demo",
            "title": "Demo",
            "summary": "Demo application",
            "description": "A demonstration application.",
            "authors": [{"name": "Alice", "github": "alice"}],
            "categories": ["Utilities"],
            "license": "MIT",
            "source_repo": "https://github.com/alice/demo",
            "homepage": "https://example.com/demo",
            "assets": {
                "icon": icon.relative_to(root).as_posix(),
                "screenshots": [
                    {
                        "path": screenshot.relative_to(root).as_posix(),
                        "caption": "Main screen",
                    }
                ],
            },
            "permissions": [],
            "compatibility": {
                "architectures": ["arm64"],
                "min_appkit_version": "0.1.0",
            },
            "launcher": {"order": 50},
        },
        "artifact": {
            "architecture": "arm64",
            "size": 1234,
            "sha256": digest,
            "media_type": "application/vnd.debian.binary-package",
            "source": {
                "repository": source_repository,
                "tag": "lpm-staging-lilygo-ui-demo-1.2.3-dddddddddddd",
                "asset": asset_name,
                "url": release_download_url(
                    source_repository,
                    "lpm-staging-lilygo-ui-demo-1.2.3-dddddddddddd",
                    asset_name,
                ),
            },
            "central": {
                "repository": "LILYGO-UI/packages",
                "tag": "apt-pool-lilygo-ui-demo",
                "asset": asset_name,
                "url": release_download_url(
                    "LILYGO-UI/packages", "apt-pool-lilygo-ui-demo", asset_name
                ),
            },
        },
    }
    release_path = root / "apps/lilygo-ui-demo/releases/1.2.3.json"
    write_json(release_path, release)
    return icon, release_path


class RegistryValidationTests(unittest.TestCase):
    def test_schemas_are_valid_draft_2020_12(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for schema in SCHEMAS:
            with self.subTest(schema=schema):
                Draft202012Validator.check_schema(
                    json.loads((root / schema).read_text(encoding="utf-8"))
                )

    def test_valid_registry_and_pages_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            make_registry(root)
            releases = validate_registry(root)
            output = root / "public"
            build_pages(
                root,
                output,
                base_url="https://lilygo-ui.github.io/packages",
                snapshot_id="a" * 40,
                generated_at="2026-08-31T12:00:00+08:00",
            )
            index = json.loads(
                (output / f"v1/snapshots/{'a' * 40}/index.json").read_text()
            )
            detail = json.loads(
                (
                    output / f"v1/snapshots/{'a' * 40}/apps/cc.lilygo.ui.Demo.json"
                ).read_text()
            )
        self.assertEqual(len(releases), 1)
        self.assertEqual(index["apps"][0]["latest_version"], "1.2.3")
        self.assertEqual(detail["releases"][0]["sha256"], "d" * 64)

    def test_registry_rejects_modified_content_addressed_asset(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            icon, _ = make_registry(root)
            icon.write_bytes(b"changed")
            with self.assertRaisesRegex(RegistryError, "hash does not match"):
                validate_registry(root)

    def test_candidate_cannot_replace_trusted_schema(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            workspace = Path(name)
            candidate = workspace / "candidate"
            trusted = workspace / "trusted"
            candidate.mkdir()
            trusted.mkdir()
            make_registry(candidate)
            for schema in SCHEMAS:
                shutil.copy2(candidate / schema, trusted / schema)
            (candidate / "publish.schema.json").write_text("{}\n", encoding="utf-8")
            release_path = candidate / "apps/lilygo-ui-demo/releases/1.2.3.json"
            release = json.loads(release_path.read_text(encoding="utf-8"))
            del release["manifest"]["title"]
            write_json(release_path, release)

            with self.assertRaisesRegex(
                RegistryError, "'title' is a required property"
            ):
                validate_registry(candidate, schema_root=trusted)

    def test_publish_pull_request_requires_owner(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            for schema in SCHEMAS:
                shutil.copy2(
                    Path(__file__).resolve().parents[1] / schema, root / schema
                )
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True)
            base = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip()
            make_registry(root)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "submission"], cwd=root, check=True)
            changed = validate_pull_request(root, base, "alice")
            self.assertIn("apps/lilygo-ui-demo/releases/1.2.3.json", changed)
            with self.assertRaisesRegex(RegistryError, "new application owner"):
                validate_pull_request(root, base, "bob")

    def test_owner_can_yank_without_changing_release_identity(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            make_registry(root)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=root,
                check=True,
            )
            subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "published"], cwd=root, check=True)
            base = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True
            ).strip()
            release_path = root / "apps/lilygo-ui-demo/releases/1.2.3.json"
            release = json.loads(release_path.read_text(encoding="utf-8"))
            release["status"] = "yanked"
            release["yank"] = {
                "reason": "Critical startup failure",
                "requested_by": "alice",
            }
            write_json(release_path, release)
            subprocess.run(["git", "add", "."], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "yank"], cwd=root, check=True)

            changed = validate_pull_request(root, base, "alice")
            output = root / "public"
            build_pages(
                root,
                output,
                base_url="https://lilygo-ui.github.io/packages",
                snapshot_id="b" * 40,
                generated_at="2026-08-31T12:00:00+08:00",
            )
            index = json.loads(
                (output / f"v1/snapshots/{'b' * 40}/index.json").read_text()
            )
            detail = json.loads(
                (
                    output / f"v1/snapshots/{'b' * 40}/apps/cc.lilygo.ui.Demo.json"
                ).read_text()
            )

        self.assertEqual(changed, {"apps/lilygo-ui-demo/releases/1.2.3.json"})
        self.assertEqual(index["apps"], [])
        self.assertEqual(detail["releases"][0]["status"], "yanked")
        self.assertEqual(detail["releases"][0]["reason"], "Critical startup failure")
        self.assertIn("yanked_at", detail["releases"][0])

    def test_debian_version_order(self) -> None:
        self.assertLess(debian_version_compare("1.0~rc1", "1.0"), 0)
        self.assertGreater(debian_version_compare("1:1.0", "9.0"), 0)
        self.assertGreater(debian_version_compare("1.0-2", "1.0-1"), 0)
        self.assertEqual(debian_version_compare("1.01", "1.1"), 0)

    def test_debian_archive_rejects_maintainer_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            package_root = root / "package"
            control = package_root / "DEBIAN"
            binary = package_root / "usr/bin/demo"
            control.mkdir(parents=True)
            binary.parent.mkdir(parents=True)
            (control / "control").write_text(
                "Package: lilygo-ui-demo\n"
                "Version: 1.2.3\n"
                "Architecture: arm64\n"
                "Maintainer: Alice <alice@example.com>\n"
                "Description: Demo\n",
                encoding="utf-8",
            )
            binary.write_text("demo\n", encoding="utf-8")
            binary.chmod(0o755)
            package = root / "demo.deb"
            subprocess.run(
                [
                    "dpkg-deb",
                    "--build",
                    "--root-owner-group",
                    str(package_root),
                    str(package),
                ],
                check=True,
                capture_output=True,
            )
            self.assertEqual(
                _read_debian_control_fields(package),
                ["lilygo-ui-demo", "1.2.3", "arm64"],
            )
            item = ReleaseFile(root / "release.json", {}, {})
            _validate_debian_archive(item, package)

            postinst = control / "postinst"
            postinst.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            postinst.chmod(0o755)
            subprocess.run(
                [
                    "dpkg-deb",
                    "--build",
                    "--root-owner-group",
                    str(package_root),
                    str(package),
                ],
                check=True,
                capture_output=True,
            )
            with self.assertRaisesRegex(RegistryError, "maintainer scripts"):
                _validate_debian_archive(item, package)

    @patch("scripts.promote_release.run_gh")
    def test_central_asset_verification_requires_matching_digest(
        self, run_gh: Mock
    ) -> None:
        run_gh.return_value = Mock(
            returncode=0,
            stdout=json.dumps(
                {
                    "assets": [
                        {
                            "name": f"sha256-{'d' * 64}.deb",
                            "size": 1234,
                            "digest": f"sha256:{'e' * 64}",
                        }
                    ]
                }
            ),
        )
        item = SimpleNamespace(
            release={
                "status": "published",
                "artifact": {
                    "size": 1234,
                    "sha256": "d" * 64,
                    "central": {
                        "tag": "apt-pool-lilygo-ui-demo",
                        "asset": f"sha256-{'d' * 64}.deb",
                    },
                },
            }
        )
        with self.assertRaisesRegex(RegistryError, "digest does not match"):
            verify_central_assets([item], "LILYGO-UI/packages")


if __name__ == "__main__":
    unittest.main()
