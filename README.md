# LILYGO UI Packages Registry

This repository is the reviewed source of truth for the LILYGO UI application
registry. GitHub Releases store Debian artifacts and GitHub Pages serves the
generated, read-only AppStore protocol.

## Publishing model

Third-party publishers do not receive write access to this repository.
`lpm publish` performs these operations with the publisher's GitHub identity:

1. Validate the project manifest and Debian control fields locally.
2. Fork this repository if necessary.
3. Upload the content-addressed Debian package to a public prerelease in the fork.
4. Commit the release record and content-addressed images to a fork branch.
5. Open a pull request against `main`.

The pull request workflow uses the validator and schemas from the protected base
revision. It validates ownership, asset hashes, the source fork, the downloaded
Debian SHA-256, and its `Package`, `Version`, and `Architecture` fields. It never
executes code from the contributor's branch and receives no release-write token.

After review and merge, the trusted promotion workflow downloads and validates
the package again, uploads it to `apt-pool-<package>` in this repository's GitHub
Releases, builds an immutable registry snapshot, and deploys GitHub Pages.

## Source layout

```text
apps/<package>/app.json
apps/<package>/releases/<version>.json
assets/sha256/<prefix>/<sha256>.<extension>
```

Application ownership is established by the first reviewed `app.json`.
Published release coordinates are immutable. `lpm unpublish` creates another
pull request that changes only the selected release status to `yanked` and adds
the public reason. The historical record and central Debian asset remain.

## AppStore endpoints

The generated Pages deployment exposes:

```text
/v1/root.json
/v1/snapshots/<git-sha>/index.json
/v1/snapshots/<git-sha>/apps/<app-id>.json
```

The AppStore must verify the index SHA-256 from `root.json`, then verify every
downloaded Debian package against the size and SHA-256 in the application detail.

## Repository protection

Enable repository forking and allow Actions to create and update releases. Enable
branch protection for `main`, require `Validate registry submission`, require
branches to be up to date, require review from CODEOWNERS, prevent force pushes,
and configure GitHub Pages to deploy through GitHub Actions. Set the workflow
token permission to read and write so the trusted promotion job can publish
Release assets. Do not enable automatic merging for failed validation checks.

Run the local checks with:

```sh
python -m pip install --requirement requirements.txt
python -m unittest discover -s tests -v
python scripts/validate_registry.py --root .
python scripts/build_pages.py --root . --output public
```
