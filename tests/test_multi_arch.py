from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.build_apt import build_apt_repository
from scripts.build_pages import build_pages
from scripts.promote_release import find_missing_central_assets, verify_central_assets
from scripts.registry_common import (
    RegistryError,
    release_download_url,
    validate_pull_request,
    validate_registry,
)
from tests.test_registry import make_registry, write_json
from tests.test_runtime import make_runtime


def add_variant(path: Path, architecture: str) -> Path:
    record = json.loads(path.read_text())
    record["artifact"]["architecture"] = architecture
    record["manifest"]["compatibility"]["architectures"] = [architecture]
    record["artifact"]["sha256"] = hashlib.sha256(architecture.encode()).hexdigest()
    for endpoint in (record["artifact"]["source"], record["artifact"]["central"]):
        endpoint["asset"] = f"sha256-{record['artifact']['sha256']}.deb"
        endpoint["url"] = release_download_url(
            endpoint["repository"], endpoint["tag"], endpoint["asset"]
        )
    destination = path.with_name(f"{record['manifest']['version']}_{architecture}.json")
    write_json(destination, record)
    return destination


class MultiArchitectureRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        _, self.legacy = make_registry(self.root)
        self.riscv = add_variant(self.legacy, "riscv64")

    def test_same_version_legacy_and_qualified_variants_are_valid(self):
        items = validate_registry(self.root)
        self.assertEqual(
            {item.release["artifact"]["architecture"] for item in items},
            {"arm64", "riscv64"},
        )
        runtime = make_runtime(self.root)
        add_variant(runtime, "riscv64")
        self.assertEqual(len(validate_registry(self.root)), 4)

    def test_promotion_checks_each_architecture_in_one_shared_pool(self):
        items = validate_registry(self.root)
        assets = [
            {
                "name": item.release["artifact"]["central"]["asset"],
                "size": item.release["artifact"]["size"],
                "digest": f"sha256:{item.release['artifact']['sha256']}",
            }
            for item in items
        ]
        response = subprocess.CompletedProcess(
            [], 0, json.dumps({"assets": assets[:1]}), ""
        )
        with patch("scripts.promote_release.run_gh", return_value=response) as gh:
            self.assertEqual(
                find_missing_central_assets(items, "LILYGO-UI/packages"), items[1:]
            )
            gh.assert_called_once()
        response.stdout = json.dumps({"assets": assets})
        with patch("scripts.promote_release.run_gh", return_value=response):
            verify_central_assets(items, "LILYGO-UI/packages")

    def test_duplicate_legacy_and_qualified_identity_rejected(self):
        add_variant(self.legacy, "arm64")
        with self.assertRaisesRegex(
            RegistryError, "duplicate package/version/architecture"
        ):
            validate_registry(self.root)

    def test_architecture_must_match_filename(self):
        self.riscv.rename(self.riscv.with_name("1.2.3_amd64.json"))
        with self.assertRaisesRegex(RegistryError, "filename must match"):
            validate_registry(self.root)

    def test_all_cannot_overlap_concrete_variant_of_same_version(self):
        add_variant(self.legacy, "all")
        with self.assertRaisesRegex(RegistryError, "combine all and concrete"):
            validate_registry(self.root)

    def test_pages_keep_both_variants_and_independent_yank_state(self):
        record = json.loads(self.riscv.read_text())
        record.update(
            status="yanked",
            yank={"reason": "Broken K230 startup", "requested_by": "alice"},
        )
        write_json(self.riscv, record)
        output = self.root / "public"
        build_pages(
            self.root,
            output,
            base_url="https://example.com/packages",
            snapshot_id="b" * 40,
            generated_at="2026-11-10T12:00:00+08:00",
        )
        detail = json.loads(
            (
                output / f"v1/snapshots/{'b' * 40}/apps/cc.lilygo.ui.Demo.json"
            ).read_text()
        )
        self.assertEqual(
            {item["architecture"]: item["status"] for item in detail["releases"]},
            {"arm64": "published", "riscv64": "yanked"},
        )
        index = json.loads((output / f"v1/snapshots/{'b' * 40}/index.json").read_text())
        self.assertEqual(index["apps"][0]["latest_version"], "1.2.3")

    def test_owner_can_add_and_yank_second_architecture(self):
        riscv_record = json.loads(self.riscv.read_text())
        self.riscv.unlink()

        def git(*args):
            return subprocess.check_output(
                ["git", *args], cwd=self.root, text=True
            ).strip()

        git("init", "-q")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "Test")
        git("add", ".")
        git("commit", "-qm", "legacy arm64")
        base = git("rev-parse", "HEAD")
        write_json(self.riscv, riscv_record)
        git("add", ".")
        git("commit", "-qm", "add riscv64")
        self.assertEqual(
            validate_pull_request(self.root, base, "alice"),
            {"apps/lilygo-ui-demo/releases/1.2.3_riscv64.json"},
        )
        with self.assertRaisesRegex(RegistryError, "does not own"):
            validate_pull_request(self.root, base, "bob")
        base = git("rev-parse", "HEAD")
        riscv_record.update(
            status="yanked", yank={"reason": "Broken startup", "requested_by": "alice"}
        )
        write_json(self.riscv, riscv_record)
        git("add", ".")
        git("commit", "-qm", "yank riscv64")
        validate_pull_request(self.root, base, "alice")
        self.assertEqual(json.loads(self.legacy.read_text())["status"], "published")
        # A same-version replacement can restore only the yanked variant.
        base = git("rev-parse", "HEAD")
        riscv_record["status"] = "published"
        del riscv_record["yank"]
        write_json(self.riscv, riscv_record)
        git("add", ".")
        git("commit", "-qm", "republish riscv64")
        validate_pull_request(self.root, base, "alice")

    @unittest.skipUnless(shutil.which("dpkg-deb"), "dpkg-deb required")
    def test_real_debs_build_separate_apt_indexes_with_shared_all_runtime(self):
        runtime_path = make_runtime(self.root, architecture="all")
        artifacts = {}
        for path in (self.legacy, self.riscv, runtime_path):
            record = json.loads(path.read_text())
            arch = record["artifact"]["architecture"]
            package = record["manifest"]["package"]
            version = record["manifest"]["version"]
            staging = self.root / f"fixture-{arch}"
            (staging / "DEBIAN").mkdir(parents=True)
            (staging / "DEBIAN/control").write_text(
                f"Package: {package}\nVersion: {version}\nArchitecture: {arch}\nMaintainer: Test <test@example.com>\nDescription: Packaging integration fixture\n"
            )
            (staging / "payload").write_text(f"{package}: {arch}\n")
            deb = self.root / f"{package}_{arch}.deb"
            subprocess.run(
                ["dpkg-deb", "--build", str(staging), str(deb)],
                check=True,
                capture_output=True,
            )
            digest = hashlib.sha256(deb.read_bytes()).hexdigest()
            record["artifact"].update(size=deb.stat().st_size, sha256=digest)
            for endpoint in (
                record["artifact"]["source"],
                record["artifact"]["central"],
            ):
                endpoint["asset"] = f"sha256-{digest}.deb"
                endpoint["url"] = release_download_url(
                    endpoint["repository"], endpoint["tag"], endpoint["asset"]
                )
            write_json(path, record)
            artifacts[digest] = deb
        output = self.root / "apt"

        def download(item, destination):
            shutil.copy2(artifacts[item.release["artifact"]["sha256"]], destination)

        with patch("scripts.build_apt._download_artifact", side_effect=download):
            build_apt_repository(
                self.root, output, generated_at="2026-11-10T12:00:00+08:00"
            )
        for arch, other in [("arm64", "riscv64"), ("riscv64", "arm64")]:
            text = (output / f"dists/stable/main/binary-{arch}/Packages").read_text()
            self.assertIn(f"Architecture: {arch}", text)
            self.assertNotIn(f"Architecture: {other}", text)
            self.assertIn("Architecture: all", text)
            self.assertIn("Package: meshtasticd", text)
            self.assertIn("Version: 1.2.3", text)
        release = (output / "dists/stable/Release").read_text()
        self.assertIn("Architectures: all arm64 riscv64", release)
        for arch in ("all", "arm64", "riscv64"):
            index = output / f"dists/stable/main/binary-{arch}/Packages"
            self.assertIn(hashlib.sha256(index.read_bytes()).hexdigest(), release)


if __name__ == "__main__":
    unittest.main()
