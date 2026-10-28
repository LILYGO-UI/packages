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

## Debian architectures

Release artifacts accept `all` for architecture-independent packages or a
concrete Debian architecture name, such as `arm64`, `amd64`, `armhf`, `riscv64`,
or `linux-arm64`. Architecture names use lowercase ASCII letters and digits,
optionally separated by single hyphens, and contain at most 64 characters.
There is no CPU architecture allowlist. Empty names, whitespace, uppercase
letters, underscores, leading or trailing hyphens, repeated hyphens, `source`,
and wildcard names containing an `any` segment (such as `any`, `linux-any`, or
`any-arm64`) are rejected.

Each package version still identifies exactly one Debian artifact, stored as
`apps/<package>/releases/<version>.json`. Its manifest must declare exactly one
entry in `compatibility.architectures`, which must match both
`artifact.architecture` and the Debian `Architecture` control field exactly.
An `all` artifact therefore declares `["all"]`. Publishing another architecture
under an existing package version is not supported. The architecture remains
fixed even when a yanked release is republished with a replacement artifact.

These schema changes must land on `main` before a publishing PR can use new
architecture values. Update the publishing branch against `main` afterward so
validation uses the new protected base revision; rerunning an old workflow
keeps its old base revision.

## Debian maintainer scripts

All packages may contain `preinst`, `postinst`, `prerm`, and `postrm` scripts,
including third-party applications. No package or publisher allowlist is
required. Validation inspects the Debian archives without executing any
maintainer scripts.

This policy is shared by PR validation and release promotion. Ownership,
artifact hashes, Debian identity fields, and archive safety checks still apply.
Validator changes must land on `main` before a publishing PR can use them.
After that change lands, update the publishing branch against `main` to trigger
validation with the new base revision; rerunning an old workflow keeps its old
base revision.

## Source layout

```text
apps/<package>/app.json
apps/<package>/releases/<version>.json
assets/sha256/<prefix>/<sha256>.<extension>
```

Application ownership is established by the first reviewed `app.json`.
`lpm unpublish` creates another pull request that changes only the selected
release status to `yanked` and adds the public reason. The historical record
and central Debian asset remain.

After the unpublish PR is merged, an application owner can use
`lpm publish --force` to republish that yanked version with a replacement Debian
package and updated manifest. The package name, version, app ID, and architecture
must stay the same. The release returns to `published`, its yank reason is
removed, and `submitted_by` must identify the republishing PR's author. Published
or revoked versions cannot be replaced directly; ordinary `lpm publish` continues
to reject existing versions.

Replacement Debian packages and images keep their content-addressed names. A
different Debian SHA-256 produces a new central asset; the old asset remains,
and Git retains the prior release record. Promotion includes modified published
records, and the next Pages snapshot points to the replacement package's URL and
SHA-256. Clients that already installed the same version may need to reinstall
it, since its version number has not increased.

The registry validator changes that permit republishing must land on `main`
before a force-publish PR can pass validation. Update the publishing branch
against `main` afterward so validation uses the new protected base revision;
rerunning an old workflow keeps its old base revision.

## AppStore endpoints

The generated Pages deployment exposes:

```text
/v1/root.json
/v1/snapshots/<git-sha>/index.json
/v1/snapshots/<git-sha>/apps/<app-id>.json
```

The AppStore must verify the index SHA-256 from `root.json`, then verify every
downloaded Debian package against the size and SHA-256 in the application detail.

## APT repository

GitHub Pages also publishes a standard Debian repository at the site root. The
promotion workflow downloads every published central `.deb` into a content-
addressed `pool/` path, generates `Packages`, `Packages.gz`, and `Packages.xz`
under `dists/stable/main/binary-<architecture>/`, and writes a hashed
`dists/stable/Release` file. Configure the `APT_GPG_PRIVATE_KEY` and
`APT_GPG_KEY_ID` Actions secrets to publish a signed `InRelease` and the public
key at `apt-keyring.gpg`. `APT_GPG_PRIVATE_KEY` is an ASCII-armored private key;
`APT_GPG_KEY_ID` may be its fingerprint and is optional when the keyring contains
only one signing key. The Pages workflow requires these signing credentials and
will fail before deployment when they are missing.

Install the public key and configure APT on a device with:

```sh
curl -fsSL https://lilygo-ui.github.io/packages/apt-keyring.gpg \
  | sudo tee /usr/share/keyrings/lilygo-ui-archive-keyring.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/lilygo-ui-archive-keyring.gpg] https://lilygo-ui.github.io/packages stable main" \
  | sudo tee /etc/apt/sources.list.d/lilygo-ui.list
sudo apt update
sudo apt install lilygo-ui-store
```

Only `published` release records are placed in APT `Packages` indexes. Yanked
and revoked records remain available through the AppStore history but are not
offered to APT clients. The repository currently keeps one architecture per
package version, matching the registry schema; publish separate versions when
different device architectures need independent artifacts.

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
