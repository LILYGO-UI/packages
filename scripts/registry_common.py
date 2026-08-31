from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tarfile
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from jsonschema import Draft202012Validator, FormatChecker

UPSTREAM = "LILYGO-UI/packages"
MAX_ICON_BYTES = 1024 * 1024
MAX_SCREENSHOT_BYTES = 2 * 1024 * 1024
MAX_DEB_BYTES = 2 * 1024 * 1024 * 1024 - 1


class RegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseFile:
    path: Path
    app: dict[str, Any]
    release: dict[str, Any]


def load_json(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise RegistryError(f"registry JSON files may not be symbolic links: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RegistryError(f"JSON root must be an object: {path}")
    return value


def validate_schema(root: Path, filename: str, value: object, source: Path) -> None:
    schema = load_json(root / filename)
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value),
        key=lambda error: list(error.absolute_path),
    )
    if errors:
        error = errors[0]
        location = ".".join(str(part) for part in error.absolute_path)
        suffix = f" at {location}" if location else ""
        raise RegistryError(
            f"schema validation failed for {source}{suffix}: {error.message}"
        )


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def release_download_url(repository: str, tag: str, asset: str) -> str:
    owner, name = repository.split("/", 1)
    return (
        f"https://github.com/{quote(owner, safe='')}/{quote(name, safe='')}/releases/download/"
        f"{quote(tag, safe='')}/{quote(asset, safe='')}"
    )


def iter_releases(
    root: Path, *, schema_root: Path | None = None
) -> Iterable[ReleaseFile]:
    schema_root = schema_root or root
    apps_root = root / "apps"
    if not apps_root.exists():
        return
    for app_path in sorted(apps_root.glob("*/app.json")):
        app = load_json(app_path)
        validate_schema(schema_root, "app.schema.json", app, app_path)
        package = str(app["package"])
        if app_path.parent.name != package:
            raise RegistryError(f"application directory must match package: {app_path}")
        for release_path in sorted((app_path.parent / "releases").glob("*.json")):
            release = load_json(release_path)
            yield ReleaseFile(release_path, app, release)


def validate_registry(
    root: Path, *, schema_root: Path | None = None
) -> list[ReleaseFile]:
    schema_root = schema_root or root
    releases = list(iter_releases(root, schema_root=schema_root))
    app_ids: dict[str, str] = {}
    for item in releases:
        validate_release(root, item, schema_root=schema_root)
        app_id = str(item.app["app_id"])
        package = str(item.app["package"])
        previous = app_ids.setdefault(app_id.lower(), package)
        if previous != package:
            raise RegistryError(
                f"app_id {app_id} is owned by both {previous} and {package}"
            )
    return releases


def validate_release(
    root: Path, item: ReleaseFile, *, schema_root: Path | None = None
) -> None:
    schema_root = schema_root or root
    release = item.release
    manifest = release.get("manifest")
    validate_schema(schema_root, "registry-release.schema.json", release, item.path)
    validate_schema(schema_root, "publish.schema.json", manifest, item.path)
    assert isinstance(manifest, dict)
    status = release["status"]
    state_fields = {key for key in ("yank", "revocation") if key in release}
    expected_state_fields = {
        "published": set(),
        "yanked": {"yank"},
        "revoked": {"revocation"},
    }[status]
    if state_fields != expected_state_fields:
        raise RegistryError(
            f"release state fields do not match status {status}: {item.path}"
        )
    package = str(item.app["package"])
    owners = [str(owner).lower() for owner in item.app["owners"]]
    if len(owners) != len(set(owners)):
        raise RegistryError(
            f"application owners must be unique ignoring case: {item.path}"
        )
    version = str(manifest["version"])
    if item.path.stem != version:
        raise RegistryError(
            f"release filename must match manifest version: {item.path}"
        )
    if manifest["package"] != package or manifest["app_id"] != item.app["app_id"]:
        raise RegistryError(
            f"release identity does not match {item.path.parent.parent / 'app.json'}"
        )

    permission_ids = [str(permission["id"]) for permission in manifest["permissions"]]
    if len(permission_ids) != len(set(permission_ids)):
        raise RegistryError(f"permission ids must be unique: {item.path}")

    artifact = release["artifact"]
    digest = str(artifact["sha256"])
    asset_name = f"sha256-{digest}.deb"
    source = artifact["source"]
    central = artifact["central"]
    if source["asset"] != asset_name or central["asset"] != asset_name:
        raise RegistryError(
            f"artifact asset name must be content-addressed: {item.path}"
        )
    if source["repository"].lower() != f"{release['submitted_by']}/packages".lower():
        raise RegistryError(
            f"source repository must be the submitter's packages fork: {item.path}"
        )
    expected_source = release_download_url(
        source["repository"], source["tag"], asset_name
    )
    if source["url"] != expected_source:
        raise RegistryError(f"source artifact URL is not canonical: {item.path}")
    expected_tag = f"apt-pool-{package}"
    expected_central = release_download_url(UPSTREAM, expected_tag, asset_name)
    if (
        central["repository"].lower() != UPSTREAM.lower()
        or central["tag"] != expected_tag
        or central["url"] != expected_central
    ):
        raise RegistryError(
            f"central artifact destination is not canonical: {item.path}"
        )
    if artifact["architecture"] not in manifest["compatibility"]["architectures"]:
        raise RegistryError(
            f"artifact architecture is not declared by manifest: {item.path}"
        )

    asset_paths = [manifest["assets"]["icon"]]
    asset_paths.extend(
        screenshot["path"] for screenshot in manifest["assets"]["screenshots"]
    )
    for index, relative in enumerate(asset_paths):
        asset_path = root / str(relative)
        try:
            asset_path.resolve().relative_to(root.resolve())
        except ValueError as exc:
            raise RegistryError(
                f"registry asset resolves outside the repository: {relative}"
            ) from exc
        if asset_path.is_symlink() or not asset_path.is_file():
            raise RegistryError(f"registry asset does not exist: {relative}")
        match = re.fullmatch(
            r"assets/sha256/([a-f0-9]{2})/([a-f0-9]{64})\.(png|jpg|jpeg|webp)",
            str(relative),
        )
        if not match or match.group(1) != match.group(2)[:2]:
            raise RegistryError(
                f"registry asset path is not content-addressed: {relative}"
            )
        if sha256_file(asset_path) != match.group(2):
            raise RegistryError(
                f"registry asset hash does not match its path: {relative}"
            )
        limit = MAX_ICON_BYTES if index == 0 else MAX_SCREENSHOT_BYTES
        if asset_path.stat().st_size > limit:
            raise RegistryError(f"registry asset exceeds {limit} bytes: {relative}")


def download_and_validate_artifact(item: ReleaseFile, target: Path) -> None:
    artifact = item.release["artifact"]
    source = artifact["source"]
    request = Request(
        source["url"], headers={"User-Agent": "lilygo-packages-validator"}
    )
    digest = hashlib.sha256()
    size = 0
    try:
        with urlopen(request, timeout=60) as response, target.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_DEB_BYTES:
                    raise RegistryError(
                        f"artifact exceeds the maximum size: {item.path}"
                    )
                digest.update(chunk)
                output.write(chunk)
    except OSError as exc:
        raise RegistryError(
            f"cannot download source artifact for {item.path}: {exc}"
        ) from exc
    if size != artifact["size"] or digest.hexdigest() != artifact["sha256"]:
        raise RegistryError(
            f"source artifact size or SHA-256 does not match: {item.path}"
        )
    manifest = item.release["manifest"]
    expected = [
        manifest["package"],
        manifest["version"],
        artifact["architecture"],
    ]
    fields = _read_debian_control_fields(target)
    if fields != expected:
        raise RegistryError(
            f"Debian control fields do not match {item.path}"
        )
    _validate_debian_archive(item, target)


def _read_debian_control_fields(package: Path) -> list[str]:
    values: list[str] = []
    for field_name in ("Package", "Version", "Architecture"):
        result = subprocess.run(
            ["dpkg-deb", "--field", str(package), field_name],
            check=False,
            text=True,
            capture_output=True,
        )
        value = result.stdout.strip()
        if result.returncode != 0 or not value or "\n" in value:
            detail = result.stderr.strip()
            raise RegistryError(
                f"cannot read Debian {field_name} field"
                + (f": {detail}" if detail else "")
            )
        values.append(value)
    return values


def _validate_debian_archive(item: ReleaseFile, package: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="lpm-deb-") as name:
        temporary = Path(name)
        data_tar = temporary / "data.tar"
        control_tar = temporary / "control.tar"
        for option, destination in (
            ("--fsys-tarfile", data_tar),
            ("--ctrl-tarfile", control_tar),
        ):
            with destination.open("wb") as output:
                result = subprocess.run(
                    ["dpkg-deb", option, str(package)],
                    check=False,
                    stdout=output,
                    stderr=subprocess.PIPE,
                )
            if result.returncode != 0:
                raise RegistryError(f"cannot inspect Debian archive for {item.path}")
        try:
            with tarfile.open(data_tar, "r:*") as archive:
                for member in archive:
                    normalized = member.name.removeprefix("./")
                    parts = Path(normalized).parts
                    if (
                        member.name.startswith("/")
                        or ".." in parts
                        or member.isdev()
                        or member.isfifo()
                        or member.mode & 0o6000
                    ):
                        raise RegistryError(
                            f"Debian archive contains an unsafe entry {member.name}: {item.path}"
                        )
                    if member.issym() or member.islnk():
                        link = member.linkname
                        if link.startswith("/") or ".." in Path(link).parts:
                            raise RegistryError(
                                f"Debian archive contains an unsafe link {member.name}: {item.path}"
                            )
            with tarfile.open(control_tar, "r:*") as archive:
                scripts = {
                    Path(member.name.removeprefix("./")).name
                    for member in archive
                    if member.isfile()
                }.intersection({"preinst", "postinst", "prerm", "postrm"})
                if scripts:
                    raise RegistryError(
                        f"third-party Debian packages may not contain maintainer scripts "
                        f"({', '.join(sorted(scripts))}): {item.path}"
                    )
        except (tarfile.TarError, OSError) as exc:
            raise RegistryError(
                f"cannot parse Debian archive for {item.path}: {exc}"
            ) from exc


def validate_source_fork(
    item: ReleaseFile, token: str, upstream: str = UPSTREAM
) -> None:
    repository = str(item.release["artifact"]["source"]["repository"])
    request = Request(
        f"https://api.github.com/repos/{repository}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "lilygo-packages-validator",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except (OSError, ValueError) as exc:
        raise RegistryError(f"cannot inspect source fork {repository}: {exc}") from exc
    parent = payload.get("parent") if isinstance(payload, dict) else None
    source = payload.get("source") if isinstance(payload, dict) else None
    names = {
        str(value.get("full_name", "")).lower()
        for value in (parent, source)
        if isinstance(value, dict)
    }
    if upstream.lower() not in names:
        raise RegistryError(
            f"source repository is not a fork of {upstream}: {repository}"
        )


def validate_remote_artifacts(
    releases: Iterable[ReleaseFile],
    token: str,
    *,
    changed_paths: set[str] | None = None,
) -> None:
    with tempfile.TemporaryDirectory(prefix="lpm-registry-") as temporary:
        for item in releases:
            relative = item.path.as_posix()
            if changed_paths is not None and relative not in changed_paths:
                continue
            if item.release["status"] != "published":
                continue
            validate_source_fork(item, token)
            target = Path(temporary) / f"{item.release['artifact']['sha256']}.deb"
            download_and_validate_artifact(item, target)


def git_changed_paths(
    root: Path, base_ref: str, head_ref: str = "HEAD"
) -> list[tuple[str, str]]:
    result = subprocess.run(
        ["git", "diff", "--name-status", "--find-renames", base_ref, head_ref],
        cwd=root,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RegistryError(f"cannot inspect registry changes: {result.stderr.strip()}")
    changes: list[tuple[str, str]] = []
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 2 or fields[0].startswith("R"):
            raise RegistryError(
                f"renames and malformed changes are not allowed: {line}"
            )
        changes.append((fields[0], fields[1]))
    return changes


def git_json(root: Path, ref: str, path: str) -> dict[str, Any] | None:
    result = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        return None
    try:
        value = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RegistryError(f"base registry file is invalid JSON: {path}") from exc
    return value if isinstance(value, dict) else None


def validate_pull_request(
    root: Path,
    base_ref: str,
    author: str,
    *,
    schema_root: Path | None = None,
) -> set[str]:
    changes = git_changed_paths(root, base_ref)
    changed_paths = {path for _, path in changes}
    if not changes:
        raise RegistryError("registry pull request contains no changes")
    releases = {
        item.path.relative_to(root).as_posix(): item
        for item in validate_registry(root, schema_root=schema_root)
    }
    referenced_assets: set[str] = set()
    added_apps: set[str] = set()
    added_release_packages: set[str] = set()

    for status, path in changes:
        if path.startswith("assets/sha256/"):
            if status != "A":
                raise RegistryError(
                    f"content-addressed assets may only be added: {path}"
                )
            continue
        match = re.fullmatch(r"apps/([^/]+)/app\.json", path)
        if match:
            if status != "A" or git_json(root, base_ref, path) is not None:
                raise RegistryError(
                    f"ownership files cannot be modified by publishing PRs: {path}"
                )
            app = load_json(root / path)
            if app["owners"] != [author] or app["package"] != match.group(1):
                raise RegistryError(
                    f"new application owner must be the PR author: {path}"
                )
            added_apps.add(str(app["package"]))
            continue
        match = re.fullmatch(r"apps/([^/]+)/releases/([^/]+)\.json", path)
        if not match or path not in releases:
            raise RegistryError(f"publishing PR contains an unsupported path: {path}")
        item = releases[path]
        owners = {str(owner).lower() for owner in item.app["owners"]}
        if author.lower() not in owners:
            raise RegistryError(
                f"PR author {author} does not own {item.app['package']}"
            )
        previous = git_json(root, base_ref, path)
        if status == "A":
            if previous is not None or item.release["status"] != "published":
                raise RegistryError(
                    f"new releases must be new published records: {path}"
                )
            if item.release["submitted_by"].lower() != author.lower():
                raise RegistryError(f"submitted_by must match the PR author: {path}")
            added_release_packages.add(str(item.app["package"]))
            manifest = item.release["manifest"]
            referenced_assets.add(str(manifest["assets"]["icon"]))
            referenced_assets.update(
                str(screenshot["path"])
                for screenshot in manifest["assets"]["screenshots"]
            )
        elif status == "M" and previous is not None:
            current_copy = dict(item.release)
            previous_copy = dict(previous)
            for value in (current_copy, previous_copy):
                value.pop("status", None)
                value.pop("yank", None)
            if current_copy != previous_copy or item.release["status"] != "yanked":
                raise RegistryError(f"existing releases may only be yanked: {path}")
            yank = item.release.get("yank", {})
            if str(yank.get("requested_by", "")).lower() != author.lower():
                raise RegistryError(f"yank requester must match the PR author: {path}")
        else:
            raise RegistryError(f"release change is not allowed: {path}")

    added_assets = {
        path for status, path in changes if status == "A" and path.startswith("assets/")
    }
    if not added_assets.issubset(referenced_assets):
        unused = ", ".join(sorted(added_assets - referenced_assets))
        raise RegistryError(f"publishing PR adds unreferenced assets: {unused}")
    if added_apps != added_release_packages.intersection(added_apps):
        missing = ", ".join(sorted(added_apps - added_release_packages))
        raise RegistryError(
            f"new ownership files require a release in the same PR: {missing}"
        )
    return changed_paths


def debian_version_compare(left: str, right: str) -> int:
    left_epoch, left_upstream, left_revision = _split_debian_version(left)
    right_epoch, right_upstream, right_revision = _split_debian_version(right)
    if left_epoch != right_epoch:
        return -1 if left_epoch < right_epoch else 1
    upstream = _compare_version_part(left_upstream, right_upstream)
    return (
        upstream if upstream else _compare_version_part(left_revision, right_revision)
    )


def _split_debian_version(value: str) -> tuple[int, str, str]:
    epoch_text, separator, remainder = value.partition(":")
    epoch = int(epoch_text) if separator else 0
    upstream_revision = remainder if separator else value
    upstream, separator, revision = upstream_revision.rpartition("-")
    return (
        epoch,
        upstream if separator else upstream_revision,
        revision if separator else "0",
    )


def _version_order(character: str) -> int:
    if character == "~":
        return -1
    if character == "":
        return 0
    if character.isalpha():
        return ord(character)
    return ord(character) + 256


def _compare_version_part(left: str, right: str) -> int:
    left_index = right_index = 0
    while left_index < len(left) or right_index < len(right):
        while (left_index < len(left) and not left[left_index].isdigit()) or (
            right_index < len(right) and not right[right_index].isdigit()
        ):
            left_char = (
                left[left_index]
                if left_index < len(left) and not left[left_index].isdigit()
                else ""
            )
            right_char = (
                right[right_index]
                if right_index < len(right) and not right[right_index].isdigit()
                else ""
            )
            if _version_order(left_char) != _version_order(right_char):
                return (
                    -1 if _version_order(left_char) < _version_order(right_char) else 1
                )
            left_index += bool(left_char)
            right_index += bool(right_char)
        left_start = left_index
        right_start = right_index
        while left_index < len(left) and left[left_index].isdigit():
            left_index += 1
        while right_index < len(right) and right[right_index].isdigit():
            right_index += 1
        left_digits = left[left_start:left_index].lstrip("0")
        right_digits = right[right_start:right_index].lstrip("0")
        if len(left_digits) != len(right_digits):
            return -1 if len(left_digits) < len(right_digits) else 1
        if left_digits != right_digits:
            return -1 if left_digits < right_digits else 1
    return 0
