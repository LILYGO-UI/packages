from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
from functools import cmp_to_key
from pathlib import Path
from typing import Any

try:
    from .registry_common import (
        RegistryError,
        debian_version_compare,
        json_bytes,
        validate_registry,
        validate_schema,
    )
except ImportError:
    from registry_common import (
        RegistryError,
        debian_version_compare,
        json_bytes,
        validate_registry,
        validate_schema,
    )


DEFAULT_BASE_URL = "https://lilygo-ui.github.io/packages"


def git_value(root: Path, arguments: list[str]) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RegistryError(
            f"git {' '.join(arguments)} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def commit_at(root: Path, path: Path, fallback: str, *, added: bool = False) -> str:
    arguments = ["git", "log", "-1", "--format=%cI"]
    if added:
        arguments.append("--diff-filter=A")
    arguments.extend(["--", path.relative_to(root).as_posix()])
    result = subprocess.run(
        arguments,
        cwd=root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    return result.stdout.strip() or fallback


def asset_url(base_url: str, path: str) -> str:
    return f"{base_url}/{path}"


def build_pages(
    root: Path,
    output: Path,
    *,
    base_url: str,
    snapshot_id: str,
    generated_at: str,
) -> None:
    releases = validate_registry(root)
    grouped: dict[str, list[Any]] = {}
    for item in releases:
        grouped.setdefault(str(item.app["package"]), []).append(item)

    snapshot_root = output / "v1" / "snapshots" / snapshot_id
    details_root = snapshot_root / "apps"
    details_root.mkdir(parents=True, exist_ok=True)
    index_apps: list[dict[str, Any]] = []
    copied_assets: set[str] = set()

    for package, items in sorted(grouped.items()):
        sorted_items = sorted(
            items,
            key=cmp_to_key(
                lambda left, right: debian_version_compare(
                    str(left.release["manifest"]["version"]),
                    str(right.release["manifest"]["version"]),
                )
            ),
            reverse=True,
        )
        published = [
            item for item in sorted_items if item.release["status"] == "published"
        ]
        current = published[0] if published else sorted_items[0]
        manifest = current.release["manifest"]
        app_id = str(manifest["app_id"])
        icon_path = str(manifest["assets"]["icon"])
        screenshots = [
            {
                "url": asset_url(base_url, str(screenshot["path"])),
                "caption": screenshot["caption"],
            }
            for screenshot in manifest["assets"]["screenshots"]
        ]
        for relative in [
            icon_path,
            *(str(value["path"]) for value in manifest["assets"]["screenshots"]),
        ]:
            if relative in copied_assets:
                continue
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / relative, destination)
            copied_assets.add(relative)

        detail_releases: list[dict[str, Any]] = []
        for item in sorted_items:
            release = item.release
            artifact = release["artifact"]
            value: dict[str, Any] = {
                "version": release["manifest"]["version"],
                "architecture": artifact["architecture"],
                "status": release["status"],
                "size": artifact["size"],
                "sha256": artifact["sha256"],
                "url": artifact["central"]["url"],
                "min_appkit_version": release["manifest"]["compatibility"][
                    "min_appkit_version"
                ],
                "published_at": commit_at(root, item.path, generated_at, added=True),
            }
            if release["status"] == "yanked":
                value["reason"] = release["yank"]["reason"]
                value["yanked_at"] = commit_at(root, item.path, generated_at)
            elif release["status"] == "revoked":
                value["reason"] = release["revocation"]["reason"]
            detail_releases.append(value)

        detail = {
            "protocol_version": 1,
            "app_id": app_id,
            "package": package,
            "title": manifest["title"],
            "summary": manifest["summary"],
            "description": manifest["description"],
            "authors": manifest["authors"],
            "categories": manifest["categories"],
            "license": manifest["license"],
            "source_repo": manifest["source_repo"],
            "homepage": manifest["homepage"],
            "icon_url": asset_url(base_url, icon_path),
            "screenshots": screenshots,
            "permissions": manifest["permissions"],
            "releases": detail_releases,
        }
        detail_path = details_root / f"{app_id}.json"
        validate_schema(root, "registry-app.schema.json", detail, detail_path)
        detail_path.write_bytes(json_bytes(detail))
        detail_url = f"{base_url}/v1/snapshots/{snapshot_id}/apps/{app_id}.json"
        if published:
            index_apps.append(
                {
                    "app_id": app_id,
                    "package": package,
                    "title": manifest["title"],
                    "summary": manifest["summary"],
                    "categories": manifest["categories"],
                    "icon_url": asset_url(base_url, icon_path),
                    "latest_version": manifest["version"],
                    "detail_url": detail_url,
                }
            )

    index = {
        "protocol_version": 1,
        "snapshot_id": snapshot_id,
        "generated_at": generated_at,
        "apps": index_apps,
    }
    index_path = snapshot_root / "index.json"
    validate_schema(root, "registry-index.schema.json", index, index_path)
    index_content = json_bytes(index)
    index_path.write_bytes(index_content)
    root_document = {
        "protocol_version": 1,
        "snapshot_id": snapshot_id,
        "generated_at": generated_at,
        "index": {
            "url": f"{base_url}/v1/snapshots/{snapshot_id}/index.json",
            "sha256": hashlib.sha256(index_content).hexdigest(),
        },
    }
    root_path = output / "v1" / "root.json"
    validate_schema(root, "registry-root.schema.json", root_document, root_path)
    root_path.parent.mkdir(parents=True, exist_ok=True)
    root_path.write_bytes(json_bytes(root_document))
    (output / ".nojekyll").touch()


def main() -> int:
    parser = argparse.ArgumentParser(description="Build static LILYGO registry pages")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--snapshot-id")
    parser.add_argument("--generated-at")
    args = parser.parse_args()
    root = args.root.resolve()
    output = args.output.resolve()
    try:
        snapshot_id = args.snapshot_id or git_value(root, ["rev-parse", "HEAD"])
        generated_at = args.generated_at or git_value(
            root, ["show", "-s", "--format=%cI", snapshot_id]
        )
        if not len(snapshot_id) == 40 or any(
            value not in "0123456789abcdef" for value in snapshot_id
        ):
            raise RegistryError("snapshot id must be a full lowercase Git commit SHA")
        build_pages(
            root,
            output,
            base_url=args.base_url.rstrip("/"),
            snapshot_id=snapshot_id,
            generated_at=generated_at,
        )
    except RegistryError as exc:
        print(f"Pages build failed: {exc}", file=sys.stderr)
        return 1
    print(f"Built registry snapshot {snapshot_id} in {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
