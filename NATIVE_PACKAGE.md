# Reusable Apple native SDK packages

This producer exposes the existing **default** Nix build as a complete,
verifiable iOS or macOS SDK. `packages.lock.nix`, `flake.lock`, the producer
commit and its patches select the source inputs. Nix may reuse a matching
derivation from its cache; the application can reuse the resulting SDK without
running the native build again.

The shared-core `opengl-next` renderer is a separate media-kit candidate. This
recipe uses the default renderer and never enables or certifies that candidate.
Changing a version number or substituting a newer libmpv file does not add the
candidate's private render ABI.

## Build one complete target

Use a committed producer checkout on macOS with Nix and the selected Xcode:

```sh
export XCODE_PATH=/Applications/Xcode_16.1.0.app
export DEVELOPER_DIR="$XCODE_PATH/Contents/Developer"
nix develop --no-update-lock-file -c make \
  XCODE_PATH="$XCODE_PATH" \
  TARGET=mk-out-xcframeworks-ios-universal-video-default
python3 scripts/package_native.py \
  --platform ios --frameworks result \
  --output /absolute/path/native-ios \
  --spec /absolute/path/native-ios-spec.json
python3 /path/to/media-kit/tool/native_package/package.py create \
  --root /absolute/path/native-ios \
  --spec /absolute/path/native-ios-spec.json
```

For macOS, select `mk-out-xcframeworks-macos-universal-video-default` and
`--platform macos`. The iOS package covers the ARM64 device slice and combined
ARM64/x86_64 simulator slice. The macOS package covers ARM64/x86_64. Each
package contains all generated dependency XCFrameworks, not only Mpv.

The exporter verifies architecture metadata against each Mach-O binary,
requires all target slices, rejects nonportable or unbundled dynamic library
references, and checks that the public mpv headers agree across slices. It
copies those same headers into `include/mpv`; the application must use them
instead of a separate source download. `provenance` contains the source/flake
locks and the compiled input inspection report.

Before combining architectures, the native recipe removes Nix/Homebrew build
paths from each dylib's `LC_RPATH` commands. The application provides the
runtime framework search path; the package must not depend on the producer's
machine. This cleanup is part of the source recipe and changes package identity.

The common media-kit packer writes a manifest containing actual file hashes,
internal symbolic links, source/toolchain identity and explicit validation
levels. Keep the spec outside the package directory. Transport the package in
a tar archive to preserve framework symbolic links and modes.

## Reusable workflow interface

`.github/workflows/native-package.yml` accepts:

| Input | Meaning |
| --- | --- |
| `producer_repository` | Repository containing this recipe, default `Goodwu/libmpv-darwin-build` |
| `producer_ref` | Complete 40-character producer commit |
| `media_kit_ref` | Complete media-kit commit providing the common package verifier |
| `platform` | `ios` or `macos` |
| `artifact_name` | Unique artifact name in the caller's workflow run |
| `xcode_path` | Installed selected Xcode, default `/Applications/Xcode_16.1.0.app` |

The workflow explicitly checks out the producer; it never accidentally builds
the calling application's repository. It uploads `native-package.tar.gz`,
`native-package-spec.json`, and `native-package-receipt.json`, and exposes
`manifest_sha256`, `identity_sha256`, and `archive_sha256` as outputs. No release
or application signing action occurs.

Compilation and package identity are recorded separately from runtime/device
acceptance. iOS installation/signing, macOS signing/notarization and physical
HDR/display behavior belong to the application validation/release workflow.

Run the portable inspection tests without a native build:

```sh
python3 -m unittest discover -s scripts -p 'test_*.py'
```
