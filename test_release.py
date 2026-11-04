from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

import package_release


class ReleaseTests(unittest.TestCase):
    def test_curated_source_package_is_complete_and_excludes_private_files(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            source = folder / "source"
            source.mkdir()
            paths = list(package_release.APP_FILES) + list(package_release.TEST_FILES)
            paths += ["static/" + name for name in package_release.STATIC_FILES]
            for name in paths:
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("fixture", encoding="utf-8")
            for name in (".env", ".cache/places.json", "model.bin", "local-generation.json",
                         "test_private_credentials.py", "static/private.key"):
                path = source / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("PRIVATE", encoding="utf-8")
            output = folder / "release.zip"
            with mock.patch.object(package_release, "ROOT", source):
                package_release.build_release(output)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(set(archive.namelist()), set(paths))
                self.assertIsNone(archive.testzip())
                self.assertTrue(all(b"PRIVATE" not in archive.read(name) for name in archive.namelist()))

    def test_missing_input_refuses_to_create_a_partial_release(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "release.zip"
            with mock.patch.object(package_release, "ROOT", Path(temp)):
                with self.assertRaisesRegex(ValueError, "Release input missing"):
                    package_release.build_release(output)
            self.assertFalse(output.exists())

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "release.zip"
            output.write_bytes(b"Existing user file")
            with self.assertRaisesRegex(ValueError, "already exists"):
                package_release.build_release(output)
            self.assertEqual(output.read_bytes(), b"Existing user file")

    def test_deployment_uses_explicit_free_eu_region_and_health_endpoint(self):
        config = (package_release.ROOT / "render.yaml").read_text(encoding="utf-8")
        for field in ("region: frankfurt", "plan: free", "runtime: docker",
                      "dockerContext: .", "healthCheckPath: /api/health"):
            self.assertIn(field, config)
        rules = (package_release.ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        self.assertEqual(rules[0], "**")
        self.assertNotIn("!.env", rules)
        for name in package_release.STATIC_FILES:
            self.assertIn("!static/" + name, rules)


if __name__ == "__main__":
    unittest.main()
