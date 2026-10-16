from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.build_pages import build_pages
from scripts.promote_release import main as promote_main
from scripts.registry_common import RegistryError, release_download_url, validate_pull_request
from tests.test_registry import make_registry, write_json


class RepublishTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.original_icon, self.release_path = make_registry(self.root)
        app_path = self.root / "apps/lilygo-ui-demo/app.json"
        app = json.loads(app_path.read_text())
        app["owners"].append("bob")
        write_json(app_path, app)
        self.original = json.loads(self.release_path.read_text())
        self.original["status"] = "yanked"
        self.original["yank"] = {"reason": "Broken startup", "requested_by": "alice"}
        self.git("init", "-q")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.base = self.commit(self.original)

    def git(self, *arguments: str) -> str:
        return subprocess.check_output(
            ["git", *arguments], cwd=self.root, text=True, stderr=subprocess.PIPE
        ).strip()

    def commit(self, release: dict) -> str:
        write_json(self.release_path, release)
        self.git("add", ".")
        self.git("commit", "-qm", "Update test registry")
        return self.git("rev-parse", "HEAD")

    def replacement(self, *, submitter: str = "alice") -> dict:
        release = copy.deepcopy(self.original)
        release["status"] = "published"
        release.pop("yank")
        release["submitted_by"] = submitter
        release["manifest"]["title"] = "Repaired Demo"
        artifact = release["artifact"]
        artifact["sha256"] = "e" * 64
        artifact["size"] = 2345
        artifact["source"]["repository"] = f"{submitter}/packages"
        artifact["source"]["tag"] = "lpm-staging-lilygo-ui-demo-1.2.3-eeeeeeeeeeee"
        for endpoint in (artifact["source"], artifact["central"]):
            endpoint["asset"] = f"sha256-{artifact['sha256']}.deb"
            endpoint["url"] = release_download_url(
                endpoint["repository"], endpoint["tag"], endpoint["asset"]
            )
        return release

    def add_image(self, content: bytes, suffix: str = "png") -> str:
        digest = hashlib.sha256(content).hexdigest()
        relative = f"assets/sha256/{digest[:2]}/{digest}.{suffix}"
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return relative

    def test_coowner_republishes_new_artifact_and_images_in_pages(self) -> None:
        release = self.replacement(submitter="bob")
        manifest = release["manifest"]
        manifest["assets"]["icon"] = self.add_image(b"new icon")
        screenshot = self.add_image(b"new screenshot", "webp")
        manifest["assets"]["screenshots"] = [
            {"path": screenshot, "caption": "Repaired startup"}
        ]
        self.commit(release)

        changed = validate_pull_request(self.root, self.base, "Bob")
        self.assertIn(self.release_path.relative_to(self.root).as_posix(), changed)
        self.assertIn(manifest["assets"]["icon"], changed)
        self.assertIn(screenshot, changed)
        self.assertTrue(self.original_icon.is_file())
        self.assertEqual(
            json.loads(self.git("show", f"{self.base}:apps/lilygo-ui-demo/releases/1.2.3.json")),
            self.original,
        )

        output = self.root / "public"
        snapshot_id = "f" * 40
        build_pages(
            self.root,
            output,
            base_url="https://lilygo-ui.github.io/packages",
            snapshot_id=snapshot_id,
            generated_at="2026-10-16T12:00:00+08:00",
        )
        snapshot = output / "v1" / "snapshots" / snapshot_id
        index = json.loads((snapshot / "index.json").read_text())
        detail = json.loads((snapshot / "apps/cc.lilygo.ui.Demo.json").read_text())
        self.assertEqual(index["apps"][0]["latest_version"], "1.2.3")
        self.assertEqual(detail["title"], "Repaired Demo")
        self.assertEqual(detail["releases"][0]["status"], "published")
        self.assertEqual(detail["releases"][0]["sha256"], "e" * 64)
        self.assertEqual(detail["releases"][0]["size"], 2345)
        self.assertEqual(detail["releases"][0]["url"], release["artifact"]["central"]["url"])
        self.assertNotIn("reason", detail["releases"][0])
        self.assertNotIn("yanked_at", detail["releases"][0])
        self.assertEqual((output / manifest["assets"]["icon"]).read_bytes(), b"new icon")
        self.assertEqual((output / screenshot).read_bytes(), b"new screenshot")

    def test_owner_can_restore_the_original_artifact(self) -> None:
        release = copy.deepcopy(self.original)
        release["status"] = "published"
        release.pop("yank")
        self.commit(release)
        validate_pull_request(self.root, self.base, "alice")

    def test_published_and_revoked_releases_cannot_be_replaced(self) -> None:
        for status in ("published", "revoked"):
            with self.subTest(status=status):
                prior = copy.deepcopy(self.original)
                prior["status"] = status
                prior.pop("yank")
                if status == "revoked":
                    prior["revocation"] = {"reason": "Unsafe package"}
                base = self.commit(prior)
                self.commit(self.replacement())
                with self.assertRaisesRegex(RegistryError, "only yanked releases"):
                    validate_pull_request(self.root, base, "alice")

    def test_republish_requires_owner_and_matching_submitter(self) -> None:
        self.commit(self.replacement())
        for author, message in (
            ("mallory", "does not own"),
            ("bob", "submitted_by must match"),
        ):
            with self.subTest(author=author):
                with self.assertRaisesRegex(RegistryError, message):
                    validate_pull_request(self.root, self.base, author)

    def test_republish_cannot_change_architecture(self) -> None:
        release = self.replacement()
        release["artifact"]["architecture"] = "amd64"
        release["manifest"]["compatibility"]["architectures"] = ["amd64"]
        self.commit(release)
        with self.assertRaisesRegex(RegistryError, "preserve release identity and architecture"):
            validate_pull_request(self.root, self.base, "alice")

    def test_republish_cannot_change_manifest_identity(self) -> None:
        for field, value in (
            ("package", "lilygo-ui-another"),
            ("version", "1.2.4"),
            ("app_id", "cc.lilygo.ui.Another"),
        ):
            with self.subTest(field=field):
                release = self.replacement()
                release["manifest"][field] = value
                self.commit(release)
                with self.assertRaisesRegex(RegistryError, "identity does not match|filename must match"):
                    validate_pull_request(self.root, self.base, "alice")

    def test_republish_rejects_unreferenced_new_images(self) -> None:
        self.add_image(b"unreferenced image")
        self.commit(self.replacement())
        with self.assertRaisesRegex(RegistryError, "unreferenced assets"):
            validate_pull_request(self.root, self.base, "alice")

    def test_republish_cannot_keep_yank_metadata(self) -> None:
        release = self.replacement()
        release["yank"] = self.original["yank"]
        self.commit(release)
        with self.assertRaisesRegex(RegistryError, "state fields do not match"):
            validate_pull_request(self.root, self.base, "alice")

    def test_yanked_metadata_change_cannot_replace_artifact(self) -> None:
        release = self.replacement()
        release["status"] = "yanked"
        release["yank"] = self.original["yank"]
        self.commit(release)
        with self.assertRaisesRegex(RegistryError, "existing releases may only be yanked"):
            validate_pull_request(self.root, self.base, "alice")

    def test_promotion_selects_modified_published_release_and_keeps_old_asset(self) -> None:
        release = self.replacement()
        self.commit(release)
        old_asset = self.original["artifact"]["central"]["asset"]
        new_asset = release["artifact"]["central"]["asset"]
        central_assets = {
            old_asset: {"name": old_asset, "size": 1234, "digest": f"sha256:{'d' * 64}"}
        }

        def run_gh(arguments: list[str], **kwargs: object) -> Mock:
            if arguments[:2] == ["release", "view"]:
                return Mock(returncode=0)
            if arguments[:2] == ["release", "download"]:
                self.assertEqual(arguments[arguments.index("--pattern") + 1], new_asset)
                return Mock(returncode=1)
            if arguments[:2] == ["release", "upload"]:
                self.assertEqual(Path(arguments[3]).name, new_asset)
                self.assertNotIn("--clobber", arguments)
                central_assets[new_asset] = {
                    "name": new_asset, "size": 2345, "digest": f"sha256:{'e' * 64}"
                }
                return Mock(returncode=0)
            if arguments[0] == "api":
                return Mock(returncode=0, stdout=json.dumps({"assets": list(central_assets.values())}))
            self.fail(f"Unexpected GitHub mutation: {arguments}")

        with (
            patch.dict("os.environ", {"GH_TOKEN": "test-token"}),
            patch("sys.argv", [
                "promote_release.py", "--root", str(self.root), "--base-ref", self.base,
                "--repository", "LILYGO-UI/packages",
            ]),
            patch("scripts.promote_release.run_gh", side_effect=run_gh),
            patch("scripts.promote_release.download_and_validate_artifact") as download,
        ):
            self.assertEqual(promote_main(), 0)

        download.assert_called_once()
        self.assertEqual(download.call_args.args[0].release["artifact"]["sha256"], "e" * 64)
        self.assertEqual(set(central_assets), {old_asset, new_asset})


if __name__ == "__main__":
    unittest.main()
