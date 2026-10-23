import base64
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

import package_native


class FrameworkPackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.frameworks = self.root / "frameworks"
        self.bundle = self.frameworks / "Mpv.xcframework"
        self.bundle.mkdir(parents=True)
        (self.root / "packages.lock.nix").write_text("{}")
        (self.root / "flake.lock").write_text("{}")
        self.libraries = [
            {"LibraryIdentifier": "ios-arm64", "LibraryPath": "Mpv.framework",
             "SupportedPlatform": "ios", "SupportedArchitectures": ["arm64"]},
            {"LibraryIdentifier": "ios-arm64_x86_64-simulator", "LibraryPath": "Mpv.framework",
             "SupportedPlatform": "ios", "SupportedPlatformVariant": "simulator",
             "SupportedArchitectures": ["arm64", "x86_64"]},
        ]
        self.write_metadata()
        for library in self.libraries:
            framework = self.bundle / library["LibraryIdentifier"] / "Mpv.framework"
            (framework / "Headers").mkdir(parents=True)
            (framework / "Modules").mkdir()
            (framework / "Mpv").write_bytes(b"Mach-O-fixture")
            for name in package_native.PUBLIC_HEADERS:
                (framework / "Headers" / name).write_text("compiled " + name)
            (framework / "Modules/module.modulemap").write_text("framework module Mpv {}")
        self.inspector = patch.object(package_native, "inspect_binary", side_effect=lambda binary, archs, names: {
            "architectures": sorted(archs), "dependencies": [], "sha256": package_native.digest(binary),
        })
        self.inspector.start()

    def tearDown(self):
        self.inspector.stop()
        self.temporary.cleanup()

    def write_metadata(self):
        (self.bundle / "Info.plist").write_bytes(plistlib.dumps({"AvailableLibraries": self.libraries}))

    def test_exports_device_simulator_and_matching_headers_without_runtime_claim(self):
        output = self.root / "package"
        spec = package_native.export(self.root, self.frameworks, "ios", output, self.root / "spec.json", {"origin": "source"})
        self.assertEqual(spec["validation"], {"compiled": True, "runtime": False, "device": False})
        self.assertEqual(spec["profile"], "default")
        self.assertEqual(spec["layout"]["frameworks"], ["Frameworks/Mpv.xcframework"])
        self.assertEqual((output / "include/mpv/client.h").read_text(), "compiled client.h")
        report = json.loads((output / "provenance/compiled-input-report.json").read_text())
        self.assertFalse(report["shared_core"])

    def test_missing_simulator_slice_fails(self):
        self.libraries.pop()
        self.write_metadata()
        with self.assertRaisesRegex(ValueError, "every required slice"):
            package_native.inspect_frameworks(self.frameworks, "ios")

    def test_device_slice_cannot_be_x86(self):
        self.libraries[0]["SupportedArchitectures"] = ["x86_64"]
        self.write_metadata()
        with self.assertRaisesRegex(ValueError, "architecture slice"):
            package_native.inspect_frameworks(self.frameworks, "ios")

    def test_headers_must_match_all_slices(self):
        (self.bundle / self.libraries[1]["LibraryIdentifier"] / "Mpv.framework/Headers/client.h").write_text("old headers")
        with self.assertRaisesRegex(ValueError, "headers differ"):
            package_native.inspect_frameworks(self.frameworks, "ios")

    def test_framework_path_cannot_escape_package(self):
        self.libraries[0]["LibraryPath"] = "../../outside.framework"
        self.write_metadata()
        with self.assertRaisesRegex(ValueError, "escaped"):
            package_native.inspect_frameworks(self.frameworks, "ios")

    def test_duplicate_device_slice_is_rejected(self):
        self.libraries.append(self.libraries[0])
        self.write_metadata()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            package_native.inspect_frameworks(self.frameworks, "ios")

    def test_macos_versioned_framework_links_survive_export(self):
        import shutil
        shutil.rmtree(self.bundle)
        self.bundle.mkdir()
        self.libraries = [{
            "LibraryIdentifier": "macos-arm64_x86_64", "LibraryPath": "Mpv.framework",
            "SupportedPlatform": "macos", "SupportedArchitectures": ["arm64", "x86_64"],
        }]
        self.write_metadata()
        framework = self.bundle / "macos-arm64_x86_64/Mpv.framework"
        version = framework / "Versions/A"
        (version / "Headers").mkdir(parents=True)
        (version / "Modules").mkdir()
        (version / "Mpv").write_bytes(b"universal-Mach-O-fixture")
        for name in package_native.PUBLIC_HEADERS:
            (version / "Headers" / name).write_text("compiled " + name)
        (version / "Modules/module.modulemap").write_text("framework module Mpv {}")
        (framework / "Versions/Current").symlink_to("A", target_is_directory=True)
        for name in ("Mpv", "Headers", "Modules"):
            (framework / name).symlink_to("Versions/Current/" + name, target_is_directory=name != "Mpv")
        output = self.root / "macos-package"
        package_native.export(self.root, self.frameworks, "macos", output, self.root / "spec.json", {"origin": "source"})
        copied = output / "Frameworks/Mpv.xcframework/macos-arm64_x86_64/Mpv.framework"
        self.assertTrue((copied / "Headers").is_symlink())
        self.assertEqual((copied / "Headers/client.h").read_text(), "compiled client.h")


class NativeInspectionTests(unittest.TestCase):
    def test_unbundled_dylib_is_rejected(self):
        with patch.object(package_native, "command", side_effect=[
            "arm64 x86_64",
            "Mpv:\n\t@rpath/Mpv.framework/Mpv (compatibility version 1.0.0, current version 1.0.0)\n"
            "\t/nix/store/old/libfoo.dylib (compatibility version 1.0.0, current version 1.0.0)",
        ]):
            with self.assertRaisesRegex(ValueError, "Unbundled"):
                package_native.inspect_binary(Path("Mpv"), {"arm64", "x86_64"}, {"Mpv.framework"})

    def test_macho_and_plist_architectures_must_agree(self):
        with patch.object(package_native, "command", return_value="arm64"):
            with self.assertRaisesRegex(ValueError, "architectures disagree"):
                package_native.inspect_binary(Path("Mpv"), {"arm64", "x86_64"}, {"Mpv.framework"})

    def test_build_rpath_is_rejected_even_when_dylib_dependencies_are_bundled(self):
        with patch.object(package_native, "command", side_effect=[
            "arm64 x86_64",
            "Mpv:\n\t@rpath/Mpv.framework/Mpv (compatibility version 1.0.0, current version 1.0.0)",
            "Load command 20\n          cmd LC_RPATH\n         path /nix/store/build-input/lib (offset 12)\n",
        ]):
            with self.assertRaisesRegex(ValueError, "build-machine runtime path"):
                package_native.inspect_binary(Path("Mpv"), {"arm64", "x86_64"}, {"Mpv.framework"})

    def test_nix_base64_archive_hash_is_normalized(self):
        value = bytes(range(32))
        self.assertEqual(package_native.archive_hash(base64.b64encode(value).decode()), value.hex())


if __name__ == "__main__":
    unittest.main()
