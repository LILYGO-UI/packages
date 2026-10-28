from __future__ import annotations

import copy
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import promote_release
from scripts.registry_common import (
    RegistryError,
    ReleaseFile,
    release_download_url,
    validate_registry,
)
from tests.test_registry import make_registry, write_json


def gh_result(
    stdout: str = "", *, stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["gh"], returncode, stdout, stderr)


class PromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        _, self.release_path = make_registry(self.root)
        self.original = json.loads(self.release_path.read_text())
        self.repository = "LILYGO-UI/packages"
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def add_release(self, version: str, status: str, digest: str) -> None:
        release = copy.deepcopy(self.original)
        release["manifest"]["version"] = version
        release["status"] = status
        if status == "yanked":
            release["yank"] = {"reason": "Broken startup", "requested_by": "alice"}
        elif status == "revoked":
            release["revocation"] = {"reason": "Unsafe package"}
        artifact = release["artifact"]
        artifact["sha256"] = digest
        for endpoint in (artifact["source"], artifact["central"]):
            endpoint["asset"] = f"sha256-{digest}.deb"
            endpoint["url"] = release_download_url(
                endpoint["repository"], endpoint["tag"], endpoint["asset"]
            )
        write_json(self.release_path.with_name(f"{version}.json"), release)

    def items(self) -> list[ReleaseFile]:
        return validate_registry(self.root)

    @staticmethod
    def metadata(item: ReleaseFile) -> dict:
        artifact = item.release["artifact"]
        return {
            "name": artifact["central"]["asset"],
            "size": artifact["size"],
            "digest": f"sha256:{artifact['sha256']}",
        }

    def run_main(
        self, changes: list[tuple[str, str]] | None = None, *, all_published: bool = False
    ) -> int:
        arguments = [
            "promote_release.py", "--root", str(self.root), "--base-ref", "base",
            "--head-ref", "head", "--repository", self.repository,
        ]
        if all_published:
            arguments.append("--all-published")
        with (
            patch.dict("os.environ", {"GH_TOKEN": "test-token"}),
            patch("sys.argv", arguments),
            patch("scripts.promote_release.git_changed_paths", return_value=changes or []),
            redirect_stdout(self.stdout),
            redirect_stderr(self.stderr),
        ):
            return promote_release.main()

    def github_pool(self, *, release_exists: bool = True) -> tuple[Mock, dict]:
        state = {"release_exists": release_exists, "assets": {}}
        expected = {self.metadata(item)["name"]: self.metadata(item) for item in self.items()}

        def execute(arguments: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            if arguments[0] == "api":
                if not state["release_exists"]:
                    return gh_result(stderr="gh: Not Found (HTTP 404)", returncode=1)
                return gh_result(json.dumps({"assets": list(state["assets"].values())}))
            if arguments[:2] == ["release", "view"]:
                return gh_result(returncode=0 if state["release_exists"] else 1)
            if arguments[:2] == ["release", "create"]:
                self.assertFalse(state["release_exists"])
                state["release_exists"] = True
                return gh_result()
            if arguments[:2] == ["release", "download"]:
                name = arguments[arguments.index("--pattern") + 1]
                self.assertNotIn(name, state["assets"])
                return gh_result(returncode=1)
            if arguments[:2] == ["release", "upload"]:
                asset = Path(arguments[3])
                self.assertTrue(asset.is_file())
                self.assertNotIn(asset.name, state["assets"])
                self.assertNotIn("--clobber", arguments)
                state["assets"][asset.name] = expected[asset.name]
                return gh_result()
            self.fail(f"Unexpected GitHub operation: {arguments}")

        return Mock(side_effect=execute), state

    def test_missing_scan_caches_tags_and_includes_yanked(self) -> None:
        self.add_release("1.2.4", "yanked", "e" * 64)
        self.add_release("1.2.5", "revoked", "f" * 64)
        items = self.items()
        response = gh_result(json.dumps({"assets": [self.metadata(items[0])]}))
        with patch("scripts.promote_release.run_gh", return_value=response) as run_gh:
            missing = promote_release.find_missing_central_assets(items, self.repository)
        self.assertEqual(missing, [items[1]])
        run_gh.assert_called_once()

    def test_existing_asset_with_legacy_null_digest_is_accepted(self) -> None:
        items = self.items()
        metadata = self.metadata(items[0])
        metadata["digest"] = None
        with patch("scripts.promote_release.run_gh", return_value=gh_result(
            json.dumps({"assets": [metadata]})
        )):
            self.assertEqual(
                promote_release.find_missing_central_assets(items, self.repository), []
            )

    def test_revoked_release_needs_no_central_asset(self) -> None:
        self.add_release("1.2.3", "revoked", "d" * 64)
        with patch("scripts.promote_release.run_gh") as run_gh:
            self.assertEqual(
                promote_release.find_missing_central_assets(self.items(), self.repository), []
            )
            promote_release.verify_central_assets(self.items(), self.repository)
        run_gh.assert_not_called()

    def test_missing_tag_http_404_returns_all_required_assets(self) -> None:
        self.add_release("1.2.4", "yanked", "e" * 64)
        items = self.items()
        response = gh_result(stderr="gh: Not Found (HTTP 404)", returncode=1)
        with patch("scripts.promote_release.run_gh", return_value=response) as run_gh:
            self.assertEqual(
                promote_release.find_missing_central_assets(items, self.repository), items
            )
        run_gh.assert_called_once()

    def test_api_errors_are_not_treated_as_missing_releases(self) -> None:
        for error in (
            "gh: Resource not accessible by integration (HTTP 403)",
            "dial tcp: lookup api.github.com: no such host",
            "gh: API rate limit exceeded (HTTP 429)",
        ):
            with self.subTest(error=error), patch(
                "scripts.promote_release.run_gh",
                return_value=gh_result(stderr=error, returncode=1),
            ), patch("scripts.promote_release.ensure_central_asset") as ensure:
                self.assertEqual(self.run_main(), 1)
                ensure.assert_not_called()

    def test_invalid_api_payload_fails_before_upload(self) -> None:
        for payload in ("not-json", "null", "[]", "{}", '{"assets":null}', '{"assets":{}}'):
            with self.subTest(payload=payload), patch(
                "scripts.promote_release.run_gh", return_value=gh_result(payload)
            ), patch("scripts.promote_release.ensure_central_asset") as ensure:
                self.assertEqual(self.run_main(), 1)
                ensure.assert_not_called()

    def test_size_and_digest_conflicts_fail_before_backfilling_other_assets(self) -> None:
        self.add_release("1.2.4", "yanked", "e" * 64)
        items = self.items()
        for field, value in (("size", 1), ("digest", f"sha256:{'f' * 64}")):
            metadata = self.metadata(items[1])
            metadata[field] = value
            with self.subTest(field=field), patch(
                "scripts.promote_release.run_gh",
                return_value=gh_result(json.dumps({"assets": [metadata]})),
            ), patch("scripts.promote_release.ensure_central_asset") as ensure:
                self.assertEqual(self.run_main(), 1)
                ensure.assert_not_called()

    def test_code_only_and_empty_changes_backfill_published_and_yanked(self) -> None:
        self.add_release("1.2.4", "yanked", "e" * 64)
        self.add_release("1.2.5", "revoked", "f" * 64)
        for changes in ([], [("M", "scripts/promote_release.py")]):
            run_gh, state = self.github_pool()
            old_asset = "sha256-" + "a" * 64 + ".deb"
            state["assets"][old_asset] = {"name": old_asset, "size": 10}
            with self.subTest(changes=changes), patch(
                "scripts.promote_release.run_gh", run_gh
            ), patch(
                "scripts.promote_release.download_and_validate_artifact",
                side_effect=lambda item, target: target.write_bytes(b"validated deb"),
            ) as download:
                self.assertEqual(self.run_main(changes), 0, self.stderr.getvalue())
            self.assertEqual(download.call_count, 2)
            self.assertEqual(
                {call.args[0].release["status"] for call in download.call_args_list},
                {"published", "yanked"},
            )
            self.assertEqual(len(state["assets"]), 3)
            self.assertIn(old_asset, state["assets"])
            api_calls = [call for call in run_gh.call_args_list if call.args[0][0] == "api"]
            self.assertEqual(len(api_calls), 2)

    def test_unchanged_existing_assets_are_not_downloaded_again(self) -> None:
        self.add_release("1.2.4", "yanked", "e" * 64)
        run_gh, state = self.github_pool()
        state["assets"] = {self.metadata(item)["name"]: self.metadata(item) for item in self.items()}
        with patch("scripts.promote_release.run_gh", run_gh), patch(
            "scripts.promote_release.download_and_validate_artifact"
        ) as download:
            self.assertEqual(self.run_main(), 0)
        download.assert_not_called()
        self.assertTrue(all(call.args[0][0] == "api" for call in run_gh.call_args_list))

    def test_changed_existing_and_all_published_keep_strong_validation(self) -> None:
        items = self.items()
        response = gh_result(json.dumps({"assets": [self.metadata(items[0])]}))
        relative = self.release_path.relative_to(self.root).as_posix()
        for changes, all_published in (([("M", relative)], False), ([], True)):
            with self.subTest(all_published=all_published), patch(
                "scripts.promote_release.run_gh", return_value=response
            ), patch("scripts.promote_release.ensure_central_asset") as ensure:
                self.assertEqual(self.run_main(changes, all_published=all_published), 0)
                ensure.assert_called_once()
                self.assertEqual(ensure.call_args.args[0], items[0])

    def test_missing_entire_release_is_created_and_verified(self) -> None:
        run_gh, state = self.github_pool(release_exists=False)
        with patch("scripts.promote_release.run_gh", run_gh), patch(
            "scripts.promote_release.download_and_validate_artifact",
            side_effect=lambda item, target: target.write_bytes(b"validated deb"),
        ):
            self.assertEqual(self.run_main(), 0, self.stderr.getvalue())
        self.assertTrue(state["release_exists"])
        self.assertEqual(len(state["assets"]), 1)
        creates = [call for call in run_gh.call_args_list if call.args[0][:2] == ["release", "create"]]
        self.assertEqual(len(creates), 1)

    def test_missing_source_fails_without_upload_or_success(self) -> None:
        run_gh, state = self.github_pool()
        with patch("scripts.promote_release.run_gh", run_gh), patch(
            "scripts.promote_release.download_and_validate_artifact",
            side_effect=RegistryError("cannot download source artifact: missing staging asset"),
        ):
            self.assertEqual(self.run_main(), 1)
        self.assertEqual(state["assets"], {})
        self.assertIn("missing staging asset", self.stderr.getvalue())
        self.assertNotIn("Promoted", self.stdout.getvalue())
        self.assertFalse(any(
            call.args[0][:2] == ["release", "upload"] for call in run_gh.call_args_list
        ))

    def test_final_verification_refetches_and_rejects_still_missing_assets(self) -> None:
        response = gh_result(json.dumps({"assets": []}))
        with patch("scripts.promote_release.run_gh", return_value=response) as run_gh, patch(
            "scripts.promote_release.ensure_central_asset"
        ) as ensure:
            self.assertEqual(self.run_main(), 1)
        ensure.assert_called_once()
        self.assertEqual(run_gh.call_count, 2)
        self.assertIn("missing", self.stderr.getvalue())
        self.assertNotIn("Promoted", self.stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
