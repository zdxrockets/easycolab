import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MACOS = ROOT / "macos_app"
WORKFLOW = ROOT / "docs/github-workflows/macos-release.yml"
PY_WORKFLOW = ROOT / "docs/github-workflows/python-tests.yml"


class PackagingStaticTest(unittest.TestCase):
    def test_swift_update_sources_are_included_by_build_glob(self):
        sources = list((MACOS / "Sources").glob("AppUpdate*.swift"))
        names = sorted(p.name for p in sources)
        self.assertEqual(names, ["AppUpdateCore.swift", "AppUpdateService.swift", "AppUpdateViews.swift"])
        script = (MACOS / "build_macos_app.sh").read_text(encoding="utf-8")
        self.assertIn('"${SCRIPT_DIR}"/Sources/*.swift', script)
        self.assertEqual(script.count("-framework CryptoKit"), 2)
        self.assertIn("EasyColabBuildInfo.json", script)
        self.assertIn("EasyColabGitCommit", script)
        self.assertIn("codesign --force --deep --sign -", script)

    def test_core_targets_user_fork_not_upstream(self):
        core = (MACOS / "Sources/AppUpdateCore.swift").read_text(encoding="utf-8")
        self.assertIn('static let owner = "zdxrockets"', core)
        self.assertIn('static let repository = "easycolab"', core)
        self.assertIn("app.easycolab.studio.macos", core)
        self.assertNotIn("wy51ai", core)
        service = (MACOS / "Sources/AppUpdateService.swift").read_text(encoding="utf-8")
        self.assertNotIn("Authorization", service)
        self.assertNotIn("gho_", service)
        self.assertNotIn("ghp_", service)
        self.assertNotIn("token", service.lower())

    def test_download_destination_is_task_bound_and_moved_before_actor_hop(self):
        service = (MACOS / "Sources/AppUpdateService.swift").read_text(encoding="utf-8")
        setup = service.split("private func downloadFile(", 1)[1].split("private func installReplacingCurrentApp", 1)[0]
        callback = service.split("didFinishDownloadingTo location: URL)", 1)[1].split("nonisolated func urlSession", 1)[0]
        self.assertNotIn("downloadDestinationPath", service)
        self.assertNotIn("downloadLock", service)
        self.assertIn("task.taskDescription = destination.path", setup)
        self.assertLess(setup.index("task.taskDescription = destination.path"), setup.index("task.resume()"))
        self.assertIn("let destPath = downloadTask.taskDescription", callback)
        self.assertLess(callback.index("let destPath = downloadTask.taskDescription"), callback.index("moveItem(at: location, to: dest)"))
        self.assertLess(callback.index("moveItem(at: location, to: dest)"), callback.index("Task { @MainActor"))
        self.assertNotIn("nonisolated(unsafe)", service)

    def test_ui_hooks_exist(self):
        main = (MACOS / "Sources/MainStudioView.swift").read_text(encoding="utf-8")
        studio = (MACOS / "EasyColabStudio.swift").read_text(encoding="utf-8")
        views = (MACOS / "Sources/AppUpdateViews.swift").read_text(encoding="utf-8")
        self.assertIn("AppUpdateController()", main)
        self.assertIn("AppUpdateSheet", main)
        self.assertIn("AppUpdateSidebarFooter", main)
        self.assertIn("openEasyColabUpdater", studio)
        self.assertIn("检查更新", views)
        self.assertIn("Download & Install", views)

    def test_macos_release_workflow_is_tag_or_dispatch_only(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch", text)
        self.assertIn("v*.*.*", text)
        self.assertNotRegex(text, r"branches:\s*\n\s*-\s*master")
        self.assertIn("macos-14", text)
        self.assertIn("make_macos_update_manifest.py", text)
        # Must not start a macOS build on every fork sync / PR.
        py = PY_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("ubuntu-latest", py)
        self.assertNotIn("macos-14", py)
        self.assertIn("contents: read", py)

    def test_docs_and_scripts_exist(self):
        self.assertTrue((ROOT / "docs/macos-self-update.md").is_file())
        self.assertTrue((ROOT / "scripts/app_update_lib.py").is_file())
        self.assertTrue((ROOT / "scripts/make_macos_update_manifest.py").is_file())


if __name__ == "__main__":
    unittest.main()
