import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import build_public_release as release
import launcher_core


BUILD_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "fontes_nativos"
    / "build_native.ps1"
).read_text(encoding="utf-8")


class NativeBuildProvenanceTests(unittest.TestCase):
    def test_launcher_profile_matches_the_published_autostart_payload(self):
        payload = release.ROOT / "ivalice-together-autostart.dll"
        actual = hashlib.sha256(payload.read_bytes()).hexdigest()

        self.assertEqual(actual, launcher_core.AUTOSTART_MODULE_SHA256)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.native = root / "native"
        self.build = self.native / "build"
        self.deployed = root / "source"
        self.build.mkdir(parents=True)
        self.deployed.mkdir()
        for index, name in enumerate(release.NATIVE_SOURCE_NAMES):
            (self.native / name).write_bytes(f"source-{index}".encode())
        for index, name in enumerate(release.NATIVE_ARTIFACT_NAMES):
            payload = f"artifact-{index}".encode()
            (self.build / name).write_bytes(payload)
            (self.deployed / name).write_bytes(payload)
        self.manifest = self.build / "native_build_manifest.json"
        self.write_manifest()

    def write_manifest(self, **changes):
        report = {
            "schema": 1,
            "sources": {
                name: release.sha256(self.native / name)
                for name in release.NATIVE_SOURCE_NAMES
            },
            "artifacts": {
                name: release.sha256(self.build / name)
                for name in release.NATIVE_ARTIFACT_NAMES
            },
        }
        report.update(changes)
        self.manifest.write_text(json.dumps(report), encoding="utf-8")

    def verify(self):
        release.verify_native_build_provenance(
            self.manifest,
            self.native,
            self.deployed,
        )

    def test_exact_atomic_native_build_is_accepted(self):
        self.verify()

    def test_source_edit_after_compilation_is_rejected(self):
        (self.native / release.NATIVE_SOURCE_NAMES[0]).write_bytes(b"changed")
        with self.assertRaisesRegex(RuntimeError, "source changed"):
            self.verify()

    def test_mixed_or_unpublished_native_payload_is_rejected(self):
        (self.deployed / release.NATIVE_ARTIFACT_NAMES[1]).write_bytes(b"old")
        with self.assertRaisesRegex(RuntimeError, "atomically published"):
            self.verify()

    def test_staged_artifact_edit_after_manifest_is_rejected(self):
        (self.build / release.NATIVE_ARTIFACT_NAMES[2]).write_bytes(b"corrupt")
        with self.assertRaisesRegex(RuntimeError, "staged native artifact is stale"):
            self.verify()

    def test_unknown_manifest_fields_fail_closed(self):
        report = json.loads(self.manifest.read_text(encoding="utf-8"))
        report["unreviewed"] = True
        self.manifest.write_text(json.dumps(report), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "unexpected schema"):
            self.verify()

    def test_native_script_stages_validates_and_commits_manifest_last(self):
        self.assertIn(".native-stage-$buildToken", BUILD_SCRIPT)
        self.assertIn("Assert-ToolIdentity $assembler", BUILD_SCRIPT)
        self.assertIn("Assert-ToolIdentity $objcopy", BUILD_SCRIPT)
        self.assertIn("Count-BytePattern", BUILD_SCRIPT)
        self.assertIn("native_build_manifest.json", BUILD_SCRIPT)
        self.assertIn("[switch]$PublishToSource", BUILD_SCRIPT)
        self.assertGreaterEqual(BUILD_SCRIPT.count("/Brepro"), 2)
        moves = BUILD_SCRIPT.index("Move-StagedArtifact $stageDirectory")
        manifest = BUILD_SCRIPT.index("native_build_manifest.json")
        self.assertLess(moves, manifest)
        self.assertIn("Remove-Item -LiteralPath $resolvedStage -Recurse", BUILD_SCRIPT)


if __name__ == "__main__":
    unittest.main()
