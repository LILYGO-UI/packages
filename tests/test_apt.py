from __future__ import annotations

import gzip
import json
import hashlib
import lzma
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.build_apt import _sign_release, build_apt_repository
from scripts.registry_common import release_download_url
from tests.test_registry import make_registry, write_json


class AptRepositoryTests(unittest.TestCase):
    def test_builds_architecture_index_and_release_checksums(self) -> None:
        for architecture in ("arm64", "all"):
            with self.subTest(architecture=architecture), tempfile.TemporaryDirectory() as name:
                root = Path(name)
                make_registry(root, architecture=architecture)
                output = root / "public"

                def fake_download(item, destination: Path) -> None:
                    # Keep this test independent from GitHub while exercising the
                    # generated pool path and Packages checksums.
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(b"fake deb payload")

                control = [
                    "Package: lilygo-ui-demo",
                    "Version: 1.2.3",
                    f"Architecture: {architecture}",
                    "Maintainer: Alice <alice@example.com>",
                    "Description: Demo application",
                    " A test package.",
                ]
                with (
                    patch("scripts.build_apt._download_artifact", side_effect=fake_download),
                    patch("scripts.build_apt._control_record", return_value=control),
                ):
                    build_apt_repository(
                        root,
                        output,
                        generated_at="2026-08-31T12:00:00+08:00",
                    )

                index = output / f"dists/stable/main/binary-{architecture}/Packages"
                compressed_gzip = index.with_suffix(".gz")
                compressed = index.with_suffix(".xz")
                release = output / "dists/stable/Release"
                self.assertTrue(index.is_file())
                self.assertTrue(compressed_gzip.is_file())
                self.assertTrue(compressed.is_file())
                index_text = index.read_text()
                self.assertIn("Package: lilygo-ui-demo", index_text)
                self.assertIn("Version: 1.2.3", index_text)
                self.assertIn(f"Architecture: {architecture}", index_text)
                self.assertIn("Filename: pool/main/l/lilygo-ui-demo/", index_text)
                self.assertIn("Size: 1234", index_text)
                self.assertIn("SHA256: " + "d" * 64, index_text)
                self.assertEqual(gzip.decompress(compressed_gzip.read_bytes()), index.read_bytes())
                self.assertEqual(lzma.decompress(compressed.read_bytes()), index.read_bytes())

                digest = hashlib.sha256(index.read_bytes()).hexdigest()
                release_text = release.read_text()
                self.assertIn("SHA256:", release_text)
                self.assertNotIn(
                    "Description: LILYGO UI Debian packages\n\nMD5Sum:",
                    release_text,
                )
                prefix = f" main/binary-{architecture}"
                self.assertIn(f" {digest} {index.stat().st_size}{prefix}/Packages", release_text)
                gzip_digest = hashlib.sha256(compressed_gzip.read_bytes()).hexdigest()
                self.assertIn(
                    f" {gzip_digest} {compressed_gzip.stat().st_size}{prefix}/Packages.gz",
                    release_text,
                )
                compressed_digest = hashlib.sha256(compressed.read_bytes()).hexdigest()
                self.assertIn(
                    f" {compressed_digest} {compressed.stat().st_size}{prefix}/Packages.xz",
                    release_text,
                )
                self.assertIn(f"Architectures: {architecture}", release_text)

    def test_release_signatures_force_sha256_digest(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name) / "public"
            release = root / "dists" / "stable" / "Release"
            release.parent.mkdir(parents=True)
            release.write_text("Origin: LILYGO UI\n", encoding="utf-8")
            commands: list[list[str]] = []

            def fake_run(command, **kwargs):
                commands.append(list(command))
                if command[-1:] == ["demo-key"]:
                    return type(
                        "Result",
                        (),
                        {"returncode": 0, "stdout": b"PUBLIC KEY", "stderr": b""},
                    )()
                output = Path(command[command.index("--output") + 1])
                output.write_text("signed", encoding="utf-8")
                return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            with patch("scripts.build_apt.subprocess.run", side_effect=fake_run):
                _sign_release(release, "demo-key")

            self.assertEqual(len(commands), 3)
            for command in commands[:2]:
                self.assertIn("--digest-algo", command)
                self.assertEqual(command[command.index("--digest-algo") + 1], "SHA256")
            self.assertIn("--clearsign", commands[0])
            self.assertIn("--detach-sign", commands[1])

    def test_architecture_independent_packages_are_in_concrete_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            _, release_path = make_registry(root, architecture="arm64")
            release = json.loads(release_path.read_text())
            release["manifest"]["version"] = "1.2.4"
            release["manifest"]["compatibility"]["architectures"] = ["all"]
            digest = "e" * 64
            release["artifact"]["architecture"] = "all"
            release["artifact"]["sha256"] = digest
            release["artifact"]["size"] = 1234
            for endpoint in (release["artifact"]["source"], release["artifact"]["central"]):
                endpoint["asset"] = f"sha256-{digest}.deb"
                endpoint["url"] = release_download_url(
                    endpoint["repository"], endpoint["tag"], endpoint["asset"]
                )
            all_release = release_path.with_name("1.2.4.json")
            write_json(all_release, release)
            output = root / "public"

            def fake_download(item, destination: Path) -> None:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"fake deb payload")

            def control(item_path: Path) -> list[str]:
                is_all = item_path.name.startswith("sha256-e")
                return [
                    "Package: lilygo-ui-demo",
                    f"Version: {'1.2.4' if is_all else '1.2.3'}",
                    f"Architecture: {'all' if is_all else 'arm64'}",
                    "Maintainer: Alice <alice@example.com>",
                    "Description: Demo application",
                ]

            with (
                patch("scripts.build_apt._download_artifact", side_effect=fake_download),
                patch("scripts.build_apt._control_record", side_effect=control),
            ):
                build_apt_repository(root, output, generated_at="2026-08-31T12:00:00+08:00")

            concrete = (output / "dists/stable/main/binary-arm64/Packages").read_text()
            independent = (output / "dists/stable/main/binary-all/Packages").read_text()
            self.assertIn("Version: 1.2.4", concrete)
            self.assertIn("Architecture: all", concrete)
            self.assertIn("Version: 1.2.4", independent)


if __name__ == "__main__":
    unittest.main()
