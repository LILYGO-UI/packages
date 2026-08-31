from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    from .registry_common import (
        RegistryError,
        validate_pull_request,
        validate_registry,
        validate_remote_artifacts,
    )
except ImportError:
    from registry_common import (
        RegistryError,
        validate_pull_request,
        validate_registry,
        validate_remote_artifacts,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate the LILYGO packages registry"
    )
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--schema-root",
        type=Path,
        help="trusted directory containing registry schemas; defaults to --root",
    )
    parser.add_argument("--base-ref")
    parser.add_argument("--pr-author")
    parser.add_argument("--fetch-artifacts", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    schema_root = args.schema_root.resolve() if args.schema_root else root
    try:
        releases = validate_registry(root, schema_root=schema_root)
        changed = None
        if args.base_ref or args.pr_author:
            if not args.base_ref or not args.pr_author:
                raise RegistryError(
                    "--base-ref and --pr-author must be provided together"
                )
            changed = validate_pull_request(
                root,
                args.base_ref,
                args.pr_author,
                schema_root=schema_root,
            )
        if args.fetch_artifacts:
            token = os.environ.get("GITHUB_TOKEN", "")
            if not token:
                raise RegistryError("GITHUB_TOKEN is required to inspect source forks")
            relative_changes = (
                {str((root / path).resolve()) for path in changed} if changed else None
            )
            validate_remote_artifacts(releases, token, changed_paths=relative_changes)
    except RegistryError as exc:
        print(f"registry validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"Validated {len(releases)} registry releases.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
