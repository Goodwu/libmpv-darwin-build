#!/usr/bin/env python3
"""Package default Nix XCFramework outputs with matching libmpv headers.

This exports a spec for media-kit's common package.py create command. The
shared-core/opengl-next candidate is a different recipe and is never enabled
or certified by this default producer.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_HEADERS = ("client.h", "render.h", "render_gl.h", "stream_cb.h")
EXPECTED = {
    "ios": {("ios", ""): {"arm64"}, ("ios", "simulator"): {"arm64", "x86_64"}},
    "macos": {("macos", ""): {"arm64", "x86_64"}},
}


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def command(*args: str, cwd: Path | None = None) -> str:
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def archive_hash(value: str) -> str:
    if re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    decoded = base64.b64decode(value.removeprefix("sha256-"), validate=True)
    if len(decoded) != 32:
        raise ValueError("Expected a complete SHA256 source archive hash")
    return decoded.hex()


def source_identity(root: Path, platform: str, frameworks: Path) -> dict:
    if command("git", "status", "--porcelain", "--untracked-files=normal", cwd=root):
        raise ValueError("Commit the producer recipe before exporting a reusable package")
    lock = json.loads(command("nix", "eval", "--json", "--file", str(root / "packages.lock.nix")))
    xcode = Path(os.environ["XCODE_PATH"])
    developer = xcode / "Contents/Developer"
    sdk_names = ["macosx"] if platform == "macos" else ["iphoneos", "iphonesimulator"]
    tools_env = os.environ.copy()
    tools_env["DEVELOPER_DIR"] = str(developer)
    sdk_versions = {
        name: subprocess.check_output(
            ["xcrun", "--sdk", name, "--show-sdk-build-version"],
            text=True, env=tools_env,
        ).strip()
        for name in sdk_names
    }
    return {
        "origin": "source",
        "sources": {
            name: {"repository": dep["url"], "version": dep["version"], "archive_sha256": archive_hash(dep["sha256"])}
            for name, dep in lock.items()
        },
        "recipe": {
            "repository": command("git", "config", "--get", "remote.origin.url", cwd=root).removesuffix(".git"),
            "commit": command("git", "rev-parse", "HEAD", cwd=root),
            "source_lock_sha256": digest(root / "packages.lock.nix"),
            "flake_lock_sha256": digest(root / "flake.lock"),
            "nix_derivation": command("nix", "path-info", "--derivation", str(frameworks)),
        },
        "toolchain": {
            "xcode": command(str(developer / "usr/bin/xcodebuild"), "-version"),
            "sdk_builds": sdk_versions,
            "nix": command("nix", "--version"),
        },
        "patches": {
            path.relative_to(root).as_posix(): digest(path)
            for path in sorted((root / "patches").glob("*.patch"))
        },
        "options": {
            "target": f"mk-out-xcframeworks-{platform}-universal-video-default",
            "variant": "video", "profile": "default", "renderer": "opengl",
            "shared_core": False,
        },
    }


def checked_child(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("XCFramework path escaped its root")
    return path


def inspect_binary(binary: Path, architectures: set[str], framework_names: set[str]) -> dict:
    actual = set(command("lipo", "-archs", str(binary)).split())
    if actual != architectures:
        raise ValueError("Mach-O architectures disagree with XCFramework metadata: " + str(binary))
    dependencies = set()
    output = command("otool", "-L", str(binary))
    for line in output.splitlines():
        match = re.match(r"\s+(\S+) \(compatibility version ", line)
        if not match:
            continue
        name = match.group(1)
        dependencies.add(name)
        if name.startswith(("/usr/lib/", "/System/Library/", "/Library/Apple/System/")):
            continue
        if not name.startswith("@rpath/") or name.split("/")[1] not in framework_names:
            raise ValueError("Unbundled/nonportable framework dependency: " + name)
    load_commands = command("otool", "-l", str(binary))
    if re.search(r"(?:/nix/store/|/opt/homebrew/|/usr/local/Cellar/)", load_commands):
        raise ValueError("Framework still contains a build-machine runtime path: " + str(binary))
    return {"architectures": sorted(actual), "dependencies": sorted(dependencies), "sha256": digest(binary)}


def inspect_frameworks(frameworks: Path, platform: str) -> tuple[dict, dict[str, bytes]]:
    bundles = sorted(frameworks.glob("*.xcframework"))
    if not bundles or not (frameworks / "Mpv.xcframework").is_dir():
        raise ValueError("Missing Mpv.xcframework")
    names = {bundle.stem + ".framework" for bundle in bundles}
    reports = {}
    canonical_headers = None
    for bundle in bundles:
        metadata = plistlib.loads((bundle / "Info.plist").read_bytes())
        libraries = metadata["AvailableLibraries"]
        seen = set()
        slices = []
        for entry in libraries:
            key = (entry["SupportedPlatform"], entry.get("SupportedPlatformVariant", ""))
            expected = EXPECTED[platform].get(key)
            if expected is None or key in seen or set(entry["SupportedArchitectures"]) != expected:
                raise ValueError("Missing, duplicate or unexpected platform/architecture slice: " + bundle.name)
            seen.add(key)
            directory = checked_child(bundle, entry["LibraryIdentifier"])
            framework = checked_child(directory, entry["LibraryPath"])
            if framework.name != bundle.stem + ".framework":
                raise ValueError("Framework name differs from its bundle")
            binary = checked_child(framework, bundle.stem)
            inspection = inspect_binary(binary, expected, names)
            slices.append({"identifier": entry["LibraryIdentifier"], "platform": key[0],
                           "variant": key[1], **inspection})
            if bundle.stem == "Mpv":
                headers = {
                    name: checked_child(framework, "Headers/" + name).read_bytes()
                    for name in PUBLIC_HEADERS
                }
                if canonical_headers is None:
                    canonical_headers = headers
                elif canonical_headers != headers:
                    raise ValueError("libmpv public headers differ across XCFramework slices")
                if not checked_child(framework, "Modules/module.modulemap").is_file():
                    raise ValueError("Mpv.framework is missing its module map")
        if seen != set(EXPECTED[platform]):
            raise ValueError("XCFramework does not cover every required slice: " + bundle.name)
        reports[bundle.name] = slices
    if canonical_headers is None:
        raise ValueError("Mpv public headers were not found")
    return reports, canonical_headers


def export(root: Path, frameworks: Path, platform: str, output: Path, spec_path: Path, identity: dict) -> dict:
    if output.exists():
        raise ValueError("Output already exists; native packages are immutable")
    if spec_path.resolve().is_relative_to(output.resolve()):
        raise ValueError("Keep the spec outside the package root")
    reports, headers = inspect_frameworks(frameworks, platform)
    output.mkdir(parents=True)
    (output / "Frameworks").mkdir()
    (output / "include/mpv").mkdir(parents=True)
    (output / "provenance").mkdir()
    for bundle in sorted(frameworks.glob("*.xcframework")):
        shutil.copytree(bundle, output / "Frameworks" / bundle.name, symlinks=True)
    for name, body in headers.items():
        (output / "include/mpv" / name).write_bytes(body)
    for name in ("packages.lock.nix", "flake.lock"):
        shutil.copy2(root / name, output / "provenance" / name)
    (output / "provenance/compiled-input-report.json").write_text(json.dumps({
        "scope": "native-binary-layout-and-header-identity",
        "frameworks": reports,
        "headers": {name: hashlib.sha256(body).hexdigest() for name, body in headers.items()},
        "runtime": "not-run", "device": "not-run", "shared_core": False,
    }, indent=2, sort_keys=True) + "\n")
    spec = {
        "schema": 1, "kind": "media-kit-native-package",
        "platform": platform, "architectures": ["arm64", "x86_64"],
        "profile": "default", "identity": identity,
        "layout": {"frameworks": ["Frameworks/" + name for name in sorted(reports)], "include": "include"},
        "capabilities": ["libmpv", "opengl"],
        "validation": {"compiled": True, "runtime": False, "device": False},
    }
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
    return spec


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=EXPECTED, required=True)
    parser.add_argument("--frameworks", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    args = parser.parse_args()
    os.environ["DEVELOPER_DIR"] = str(Path(os.environ["XCODE_PATH"]) / "Contents/Developer")
    identity = source_identity(ROOT, args.platform, args.frameworks)
    export(ROOT, args.frameworks, args.platform, args.output, args.spec, identity)
    print(json.dumps({"root": str(args.output.resolve()), "spec": str(args.spec.resolve())}))


if __name__ == "__main__":
    main()
