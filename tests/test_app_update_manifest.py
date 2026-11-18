import io
import json
import os
import stat
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import app_update_lib as lib  # noqa: E402

VECTORS = json.loads((REPO_ROOT / "tests/fixtures/app_update/vectors.json").read_text(encoding="utf-8"))


class VersionCompareTest(unittest.TestCase):
    def test_vector_identities(self):
        for case in VECTORS["version_cases"]:
            with self.subTest(case["name"]):
                result = lib.compare_identities(case["current"], case["candidate"])
                self.assertEqual(result["newer"], case["expect_newer"], result)
                self.assertEqual(result["status"], case["expect_status"], result)

    def test_parse_semver_accepts_v_prefix(self):
        self.assertEqual(lib.parse_semver("v1.1.2"), (1, 1, 2))
        self.assertEqual(lib.parse_semver("1.1.2"), (1, 1, 2))
        self.assertIsNone(lib.parse_semver("1.1"))
        self.assertIsNone(lib.parse_semver("latest"))


class UrlAllowlistTest(unittest.TestCase):
    def test_vectors(self):
        for case in VECTORS["url_cases"]:
            with self.subTest(case["url"]):
                if case["kind"] == "api":
                    self.assertEqual(lib.is_allowed_releases_api_url(case["url"]), case["ok"])
                else:
                    self.assertEqual(lib.is_allowed_download_url(case["url"]), case["ok"])

    def test_never_accepts_upstream_wy51ai(self):
        self.assertFalse(
            lib.is_allowed_releases_api_url("https://api.github.com/repos/wy51ai/easycolab/releases")
        )
        self.assertFalse(
            lib.is_allowed_download_url(
                "https://github.com/wy51ai/easycolab/releases/download/1.1.1/EasyColab-macOS.zip"
            )
        )


class ManifestTest(unittest.TestCase):
    def test_valid_manifest_roundtrip(self):
        url = "https://github.com/zdxrockets/easycolab/releases/download/v1.1.2/EasyColab-macOS.zip"
        doc = lib.build_manifest(
            version="1.1.2",
            release_tag="v1.1.2",
            git_commit="a" * 40,
            sha256_hex="b" * 64,
            asset_url=url,
            notes="test",
        )
        self.assertEqual(lib.validate_manifest(doc), [])
        self.assertEqual(doc["repository"], "zdxrockets/easycolab")
        self.assertEqual(doc["signing"], "adhoc")
        self.assertIn("not Apple-notarized", doc["signing_note"])

    def test_rejects_upstream_asset_url(self):
        with self.assertRaises(ValueError):
            lib.build_manifest(
                version="1.1.2",
                release_tag="v1.1.2",
                git_commit="a" * 40,
                sha256_hex="b" * 64,
                asset_url="https://github.com/wy51ai/easycolab/releases/download/v1.1.2/EasyColab-macOS.zip",
            )

    def test_pick_latest_ignores_draft_prerelease_and_zip_without_manifest(self):
        releases = [
            {
                "tag_name": "v1.1.3",
                "draft": False,
                "prerelease": True,
                "assets": [{"name": "EasyColab-macOS.zip"}, {"name": "EasyColab-macOS.manifest.json"}],
            },
            {
                "tag_name": "v1.1.1",
                "draft": False,
                "prerelease": False,
                "assets": [{"name": "EasyColab-macOS.zip"}],
            },
            {
                "tag_name": "v1.1.2",
                "draft": False,
                "prerelease": False,
                "assets": [{"name": "EasyColab-macOS.zip"}, {"name": "EasyColab-macOS.manifest.json"}],
            },
            {
                "tag_name": "v9.9.9",
                "draft": True,
                "prerelease": False,
                "assets": [{"name": "EasyColab-macOS.zip"}, {"name": "EasyColab-macOS.manifest.json"}],
            },
        ]
        picked = lib.pick_latest_stable_release(releases)
        self.assertEqual(picked["tag_name"], "v1.1.2")


class ZipSafetyTest(unittest.TestCase):
    def test_path_vectors(self):
        for case in VECTORS["zip_path_cases"]:
            with self.subTest(case["name"]):
                ok, reason = lib.zip_entry_is_safe(case["name"])
                self.assertEqual(ok, case["ok"], reason)
                if not case["ok"]:
                    self.assertEqual(reason, case["reason"])

    def _make_zip(self, tmp: str, entries: dict, symlink_name: str | None = None) -> str:
        path = os.path.join(tmp, "sample.zip")
        with zipfile.ZipFile(path, "w") as zf:
            for name, data in entries.items():
                zf.writestr(name, data)
            if symlink_name:
                info = zipfile.ZipInfo(symlink_name)
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o644) << 16
                zf.writestr(info, "/tmp/evil")
        return path

    def test_clean_app_zip_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._make_zip(
                tmp,
                {
                    "EasyColab.app/Contents/MacOS/EasyColab": struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, 0, 0, 0, 0),
                    "EasyColab.app/Contents/Info.plist": b"<plist/>",
                    "__MACOSX/EasyColab.app/._Info.plist": b"rsrc",
                },
            )
            result = lib.inspect_zip(path)
            self.assertTrue(result["ok"], result["errors"])

    def test_zip_slip_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            slip = self._make_zip(
                tmp,
                {
                    "EasyColab.app/Contents/MacOS/EasyColab": struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, 0, 0, 0, 0),
                    "../outside.txt": b"pwn",
                },
            )
            result = lib.inspect_zip(slip)
            self.assertFalse(result["ok"])
            self.assertTrue(any("dotdot" in e for e in result["errors"]))

            link = self._make_zip(
                tmp,
                {"EasyColab.app/Contents/MacOS/EasyColab": struct.pack("<8I", 0xFEEDFACF, 0x0100000C, 0, 2, 0, 0, 0, 0)},
                symlink_name="EasyColab.app/Contents/Resources/link",
            )
            result = lib.inspect_zip(link)
            self.assertFalse(result["ok"])
            self.assertTrue(any("symlink" in e for e in result["errors"]))

    def test_sha256_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "f.bin")
            with open(path, "wb") as handle:
                handle.write(b"abc")
            self.assertEqual(
                lib.sha256_file(path),
                "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
            )


class SourcePushIsNotAReleaseTest(unittest.TestCase):
    def test_master_push_without_assets_is_not_installable(self):
        releases = [
            {
                "tag_name": "",
                "draft": False,
                "prerelease": False,
                "assets": [],
            }
        ]
        self.assertIsNone(lib.pick_latest_stable_release(releases))


if __name__ == "__main__":
    unittest.main()
