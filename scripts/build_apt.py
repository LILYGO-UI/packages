from __future__ import annotations

import argparse
import email.utils
from functools import cmp_to_key
import hashlib
import gzip
import lzma
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

try:
    from .registry_common import (
        RegistryError,
        ReleaseFile,
        debian_version_compare,
        sha256_file,
        validate_registry,
    )
except ImportError:
    from registry_common import (
        RegistryError,
        ReleaseFile,
        debian_version_compare,
        sha256_file,
        validate_registry,
    )

DEFAULT_SUITE = "stable"
DEFAULT_COMPONENT = "main"
MAX_DEB_BYTES = 2 * 1024 * 1024 * 1024 - 1


def _digest_file(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _download_artifact(item: ReleaseFile, destination: Path) -> None:
    artifact = item.release["artifact"]
    request = Request(
        str(artifact["central"]["url"]),
        headers={"User-Agent": "lilygo-packages-apt-builder"},
    )
    temporary = destination.with_name(destination.name + ".part")
    digest = hashlib.sha256()
    size = 0
    try:
        with urlopen(request, timeout=120) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_DEB_BYTES:
                    raise RegistryError(f"central artifact exceeds the maximum size: {item.path}")
                digest.update(chunk)
                output.write(chunk)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise RegistryError(f"cannot download central artifact for {item.path}: {exc}") from exc
    if size != artifact["size"] or digest.hexdigest() != artifact["sha256"]:
        temporary.unlink(missing_ok=True)
        raise RegistryError(f"central artifact size or SHA-256 does not match: {item.path}")
    package = str(item.release["manifest"]["package"])
    version = str(item.release["manifest"]["version"])
    architecture = str(artifact["architecture"])
    try:
        lines = _control_record(temporary)
    except RegistryError:
        temporary.unlink(missing_ok=True)
        raise
    fields = {
        line.split(":", 1)[0]: line.split(":", 1)[1].strip()
        for line in lines
        if line and not line[0].isspace() and ":" in line
    }
    if (
        fields.get("Package") != package
        or fields.get("Version") != version
        or fields.get("Architecture") != architecture
    ):
        temporary.unlink(missing_ok=True)
        raise RegistryError(f"central Debian control fields do not match: {item.path}")
    temporary.replace(destination)


def _control_record(package: Path) -> list[str]:
    result = subprocess.run(
        ["dpkg-deb", "--field", str(package)],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        detail = result.stderr.strip()
        raise RegistryError(
            f"cannot read Debian control fields from {package}"
            + (f": {detail}" if detail else "")
        )

    # Packages entries contain the Debian control record plus repository fields.
    # Drop fields that a package may already contain so the generated values are authoritative.
    removed = {"Filename", "Size", "MD5sum", "SHA256"}
    lines: list[str] = []
    current: str | None = None
    for line in result.stdout.splitlines():
        if line and not line[0].isspace() and ":" in line:
            current = line.split(":", 1)[0]
        if current not in removed:
            lines.append(line)
    while lines and not lines[-1].strip():
        lines.pop()
    required = {"Package", "Version", "Architecture", "Maintainer", "Description"}
    present = {
        line.split(":", 1)[0]
        for line in lines
        if line and not line[0].isspace() and ":" in line
    }
    missing = sorted(required - present)
    if missing:
        raise RegistryError(f"Debian control record is missing {', '.join(missing)}: {package}")
    return lines


def _package_entry(item: ReleaseFile, relative: str, local_path: Path) -> str:
    artifact = item.release["artifact"]
    lines = _control_record(local_path)
    fields = {
        line.split(":", 1)[0]: line.split(":", 1)[1].strip()
        for line in lines
        if line and not line[0].isspace() and ":" in line
    }
    expected = {
        "Package": str(item.release["manifest"]["package"]),
        "Version": str(item.release["manifest"]["version"]),
        "Architecture": str(artifact["architecture"]),
    }
    for field, value in expected.items():
        if fields.get(field) != value:
            raise RegistryError(
                f"Debian control {field} does not match registry metadata: {item.path}"
            )
    lines.extend(
        [
            f"Filename: {relative}",
            f"Size: {artifact['size']}",
            f"MD5sum: {_digest_file(local_path, 'md5')}",
            f"SHA256: {artifact['sha256']}",
        ]
    )
    return "\n".join(lines) + "\n"


def _release_timestamp(value: str | None) -> str:
    if value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise RegistryError(f"generated-at is not an ISO-8601 timestamp: {value}") from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = datetime.now(timezone.utc)
    return email.utils.format_datetime(parsed.astimezone(timezone.utc), usegmt=True)


def _write_release(
    root: Path,
    *,
    suite: str,
    component: str,
    architectures: list[str],
    timestamp: str,
) -> Path:
    dists = root / "dists" / suite
    files = sorted(
        path for path in dists.rglob("*") if path.is_file() and path.name not in {"Release", "InRelease", "Release.gpg"}
    )
    relative = [path.relative_to(dists).as_posix() for path in files]
    lines = [
        "Origin: LILYGO UI",
        "Label: LILYGO UI",
        f"Suite: {suite}",
        f"Codename: {suite}",
        f"Date: {timestamp}",
        f"Architectures: {' '.join(architectures)}",
        f"Components: {component}",
        "Description: LILYGO UI Debian packages",
        "",
        "MD5Sum:",
    ]
    for path, name in zip(files, relative):
        lines.append(f" {hashlib.md5(path.read_bytes()).hexdigest()} {path.stat().st_size} {name}")
    lines.append("SHA256:")
    for path, name in zip(files, relative):
        lines.append(f" {sha256_file(path)} {path.stat().st_size} {name}")
    release = dists / "Release"
    release.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return release


def _sign_release(release: Path, signing_key: str) -> None:
    output = release.with_name("InRelease")
    result = subprocess.run(
        [
            "gpg",
            "--batch",
            "--yes",
            "--pinentry-mode",
            "loopback",
            "--digest-algo",
            "SHA256",
            "--local-user",
            signing_key,
            "--clearsign",
            "--output",
            str(output),
            str(release),
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RegistryError(f"cannot create InRelease: {(result.stderr or result.stdout).strip()}")
    result = subprocess.run(
        [
            "gpg",
            "--batch",
            "--yes",
            "--pinentry-mode",
            "loopback",
            "--digest-algo",
            "SHA256",
            "--local-user",
            signing_key,
            "--armor",
            "--detach-sign",
            "--output",
            str(release.with_name("Release.gpg")),
            str(release),
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RegistryError(f"cannot create Release.gpg: {(result.stderr or result.stdout).strip()}")
    result = subprocess.run(
        ["gpg", "--batch", "--armor", "--export", signing_key],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0 or not result.stdout:
        raise RegistryError(f"cannot export APT public key: {(result.stderr or b'').decode(errors='replace').strip()}")
    release.parents[2].joinpath("apt-key.asc").write_bytes(result.stdout)


def build_apt_repository(
    root: Path,
    output: Path,
    *,
    suite: str = DEFAULT_SUITE,
    component: str = DEFAULT_COMPONENT,
    generated_at: str | None = None,
    signing_key: str | None = None,
) -> None:
    if not suite or "/" in suite or ".." in Path(suite).parts:
        raise RegistryError("suite must be a non-empty repository path component")
    if not component or "/" in component or ".." in Path(component).parts:
        raise RegistryError("component must be a non-empty repository path component")

    # validate_registry returns both application and runtime records. Both use
    # the same Debian artifact contract and therefore share one APT pool.
    releases = [item for item in validate_registry(root) if item.release["status"] == "published"]
    by_arch: dict[str, list[ReleaseFile]] = defaultdict(list)
    pool_paths: dict[str, Path] = {}
    for item in releases:
        artifact = item.release["artifact"]
        package = str(item.release["manifest"]["package"])
        digest = str(artifact["sha256"])
        asset = f"sha256-{digest}.deb"
        relative = f"pool/{component}/{package[0]}/{package}/{asset}"
        destination = output / relative
        if digest not in pool_paths:
            destination.parent.mkdir(parents=True, exist_ok=True)
            _download_artifact(item, destination)
            pool_paths[digest] = destination
        elif not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(pool_paths[digest], destination)
        by_arch[str(artifact["architecture"])].append(item)

    dists = output / "dists" / suite / component
    architectures = sorted(by_arch)
    if not architectures:
        raise RegistryError("cannot build an APT repository with no published releases")
    all_items = list(by_arch.get("all", []))
    for architecture, architecture_items in by_arch.items():
        index_dir = dists / f"binary-{architecture}"
        index_dir.mkdir(parents=True, exist_ok=True)
        items = (
            architecture_items
            if architecture == "all"
            else [*architecture_items, *all_items]
        )
        sorted_items = sorted(
            items,
            key=cmp_to_key(
                lambda left, right: (
                    (str(left.release["manifest"]["package"]) > str(right.release["manifest"]["package"]))
                    - (str(left.release["manifest"]["package"]) < str(right.release["manifest"]["package"]))
                    or debian_version_compare(
                        str(left.release["manifest"]["version"]),
                        str(right.release["manifest"]["version"]),
                    )
                )
            ),
        )
        entries: list[str] = []
        for item in sorted_items:
            artifact = item.release["artifact"]
            package = str(item.release["manifest"]["package"])
            digest = str(artifact["sha256"])
            relative = f"pool/{component}/{package[0]}/{package}/sha256-{digest}.deb"
            entries.append(_package_entry(item, relative, pool_paths[digest]))
        content = "\n".join(entries).encode("utf-8")
        (index_dir / "Packages").write_bytes(content)
        with (index_dir / "Packages.gz").open("wb") as handle:
            with gzip.GzipFile(fileobj=handle, mode="wb", compresslevel=9, mtime=0) as compressed:
                compressed.write(content)
        (index_dir / "Packages.xz").write_bytes(lzma.compress(content, format=lzma.FORMAT_XZ))

    release = _write_release(
        output,
        suite=suite,
        component=component,
        architectures=architectures,
        timestamp=_release_timestamp(generated_at),
    )
    if signing_key:
        _sign_release(release, signing_key)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a static Debian APT repository")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--suite", default=DEFAULT_SUITE)
    parser.add_argument("--component", default=DEFAULT_COMPONENT)
    parser.add_argument("--generated-at")
    parser.add_argument("--signing-key", "--signing-key-id", dest="signing_key")
    parser.add_argument("--download", action="store_true", help="download central Debian assets (the default)")
    args = parser.parse_args()
    try:
        build_apt_repository(
            args.root.resolve(),
            args.output.resolve(),
            suite=args.suite,
            component=args.component,
            generated_at=args.generated_at,
            signing_key=args.signing_key,
        )
    except RegistryError as exc:
        print(f"APT build failed: {exc}", file=sys.stderr)
        return 1
    print(f"Built APT repository in {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
