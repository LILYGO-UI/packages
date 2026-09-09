from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.registry_common import (
    RegistryError,
    ReleaseFile,
    _validate_debian_archive,
    download_and_validate_artifact,
)


class DebianArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package_root = self.root / "package"
        self.control = self.package_root / "DEBIAN"
        self.control.mkdir(parents=True)
        (self.control / "control").write_text(
            "Package: lilygo-ui-demo\n"
            "Version: 0.1.0\n"
            "Architecture: arm64\n"
            "Maintainer: Alice <alice@example.com>\n"
            "Description: Demo\n",
            encoding="utf-8",
        )
        self.binary = self.package_root / "usr/bin/lilygo-ui-demo"
        self.binary.parent.mkdir(parents=True)
        self.binary.write_text("demo\n", encoding="utf-8")
        self.binary.chmod(0o755)
        for script in ("preinst", "postinst", "prerm", "postrm"):
            self.add_script(script)
        self.item = ReleaseFile(
            self.root / "release.json",
            {
                "package": "lilygo-ui-demo",
                "app_id": "cc.lilygo.ui.Demo",
                "owners": ["alice"],
            },
            {
                "submitted_by": "alice",
                "manifest": {
                    "package": "lilygo-ui-demo",
                    "app_id": "cc.lilygo.ui.Demo",
                    "version": "0.1.0",
                },
            },
        )

    def add_script(self, name: str) -> None:
        script = self.control / name
        script.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        script.chmod(0o755)

    def build_package(self) -> Path:
        package = self.root / "demo.deb"
        subprocess.run(
            [
                "dpkg-deb",
                "--build",
                "--root-owner-group",
                str(self.package_root),
                str(package),
            ],
            check=True,
            capture_output=True,
        )
        return package

    def test_third_party_scripts_are_allowed_without_execution(self) -> None:
        _validate_debian_archive(self.item, self.build_package())

    def test_payload_still_rejects_privileged_modes(self) -> None:
        package = self.build_package()
        data_tar = self.root / "privileged.tar"
        # Construct the header directly because a sandbox may strip setuid bits.
        member = tarfile.TarInfo("./usr/bin/lilygo-ui-demo")
        member.mode = 0o4755
        with tarfile.open(data_tar, "w") as archive:
            archive.addfile(member)
        open_tar = tarfile.open

        def open_archive(path: Path, mode: str) -> tarfile.TarFile:
            return open_tar(data_tar if path.name == "data.tar" else path, mode)

        with patch("scripts.registry_common.tarfile.open", side_effect=open_archive):
            with self.assertRaisesRegex(RegistryError, "unsafe entry"):
                _validate_debian_archive(self.item, package)

    def test_payload_still_rejects_unsafe_links(self) -> None:
        self.binary.with_name("unsafe-link").symlink_to("/etc/passwd")
        with self.assertRaisesRegex(RegistryError, "unsafe link"):
            _validate_debian_archive(self.item, self.build_package())

    def test_download_still_checks_control_identity(self) -> None:
        payload = self.build_package().read_bytes()
        self.item.release["artifact"] = {
            "architecture": "arm64",
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "source": {"url": "https://example.com/demo.deb"},
        }
        target = self.root / "download.deb"
        with patch("scripts.registry_common.urlopen", return_value=io.BytesIO(payload)):
            download_and_validate_artifact(self.item, target)

        control_path = self.control / "control"
        control_path.write_text(
            control_path.read_text(encoding="utf-8").replace(
                "Package: lilygo-ui-demo", "Package: lilygo-ui-other"
            ),
            encoding="utf-8",
        )
        payload = self.build_package().read_bytes()
        self.item.release["artifact"].update(
            size=len(payload), sha256=hashlib.sha256(payload).hexdigest()
        )
        with patch("scripts.registry_common.urlopen", return_value=io.BytesIO(payload)):
            with self.assertRaisesRegex(RegistryError, "control fields do not match"):
                download_and_validate_artifact(self.item, target)


if __name__ == "__main__":
    unittest.main()
