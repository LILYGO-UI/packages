from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from .registry_common import (
        RegistryError,
        ReleaseFile,
        download_and_validate_artifact,
        git_changed_paths,
        sha256_file,
        validate_registry,
    )
except ImportError:
    from registry_common import (
        RegistryError,
        ReleaseFile,
        download_and_validate_artifact,
        git_changed_paths,
        sha256_file,
        validate_registry,
    )


def run_gh(
    arguments: list[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["gh", *arguments],
        check=False,
        text=True,
        capture_output=True,
    )
    if check and result.returncode != 0:
        raise RegistryError(
            f"gh {' '.join(arguments)} failed: {(result.stderr or result.stdout).strip()}"
        )
    return result


def ensure_central_asset(
    item: ReleaseFile,
    *,
    repository: str,
    target: str,
    temporary: Path,
) -> None:
    release = item.release
    artifact = release["artifact"]
    central = artifact["central"]
    tag = central["tag"]
    asset_name = central["asset"]
    package = release["manifest"]["package"]
    release_view = run_gh(
        ["release", "view", tag, "--repo", repository, "--json", "tagName"],
        check=False,
    )
    if release_view.returncode != 0:
        run_gh(
            [
                "release",
                "create",
                tag,
                "--repo",
                repository,
                "--target",
                target,
                "--title",
                f"Debian pool: {package}",
                "--notes",
                "Content-addressed Debian packages promoted from reviewed registry submissions.",
            ]
        )

    existing_dir = temporary / "existing"
    existing_dir.mkdir(exist_ok=True)
    existing = run_gh(
        [
            "release",
            "download",
            tag,
            "--repo",
            repository,
            "--pattern",
            asset_name,
            "--dir",
            str(existing_dir),
        ],
        check=False,
    )
    existing_path = existing_dir / asset_name
    if existing.returncode == 0 and existing_path.is_file():
        if (
            existing_path.stat().st_size != artifact["size"]
            or sha256_file(existing_path) != artifact["sha256"]
        ):
            raise RegistryError(
                f"central release contains a conflicting asset: {asset_name}"
            )
        return

    source_path = temporary / asset_name
    download_and_validate_artifact(item, source_path)
    run_gh(["release", "upload", tag, str(source_path), "--repo", repository])


def verify_central_assets(releases: list[ReleaseFile], repository: str) -> None:
    cached: dict[str, dict[str, object]] = {}
    for item in releases:
        if item.release["status"] not in {"published", "yanked"}:
            continue
        artifact = item.release["artifact"]
        central = artifact["central"]
        tag = str(central["tag"])
        if tag not in cached:
            result = run_gh(
                ["api", f"repos/{repository}/releases/tags/{tag}"], check=False
            )
            if result.returncode != 0:
                raise RegistryError(f"central release does not exist: {tag}")
            try:
                payload = json.loads(result.stdout)
            except json.JSONDecodeError as exc:
                raise RegistryError(
                    f"GitHub returned invalid release data for {tag}"
                ) from exc
            cached[tag] = payload
        assets = cached[tag].get("assets", [])
        match = next(
            (
                asset
                for asset in assets
                if isinstance(asset, dict) and asset.get("name") == central["asset"]
            ),
            None,
        )
        if not isinstance(match, dict) or match.get("size") != artifact["size"]:
            raise RegistryError(
                f"central release asset is missing or has the wrong size: {central['asset']}"
            )
        digest = match.get("digest")
        if digest is not None and digest != f"sha256:{artifact['sha256']}":
            raise RegistryError(
                f"central release asset digest does not match: {central['asset']}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Promote reviewed release assets")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--head-ref", default="HEAD")
    parser.add_argument("--repository", required=True)
    parser.add_argument("--all-published", action="store_true")
    args = parser.parse_args()
    if not os.environ.get("GH_TOKEN") and not os.environ.get("GITHUB_TOKEN"):
        print("promotion failed: GH_TOKEN or GITHUB_TOKEN is required", file=sys.stderr)
        return 1
    root = args.root.resolve()
    try:
        changes = git_changed_paths(root, args.base_ref, args.head_ref)
        changed = {path for _, path in changes}
        releases = validate_registry(root)
        selected = [
            item
            for item in releases
            if item.release["status"] == "published"
            and (
                args.all_published or item.path.relative_to(root).as_posix() in changed
            )
        ]
        with tempfile.TemporaryDirectory(prefix="lpm-promote-") as name:
            temporary = Path(name)
            for index, item in enumerate(selected):
                item_temp = temporary / str(index)
                item_temp.mkdir()
                ensure_central_asset(
                    item,
                    repository=args.repository,
                    target=args.head_ref,
                    temporary=item_temp,
                )
            verify_central_assets(releases, args.repository)
    except RegistryError as exc:
        print(f"promotion failed: {exc}", file=sys.stderr)
        return 1
    print(f"Promoted {len(selected)} release assets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
