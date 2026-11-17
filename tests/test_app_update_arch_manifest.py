"""Real manifest CLI checks. The build fallback test uses a stub compiler, not Xcode."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests.test_app_update_binary import ROOT, BINARY, thin, fat
sys.path.insert(0, str(ROOT / 'scripts'))
import app_update_lib as lib


class ArchitectureManifestTest(unittest.TestCase):
    def run_manifest(self, directory, binary, *flags):
        archive = directory / 'EasyColab-macOS.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            z.writestr(BINARY, binary)
        output = directory / 'manifest.json'
        command = [sys.executable, str(ROOT / 'scripts/make_macos_update_manifest.py'),
                   '--zip', str(archive), '--version', '1.2.0', '--tag', 'v1.2.0',
                   '--commit', 'a'*40, '--out', str(output), *flags]
        result = subprocess.run(command, capture_output=True, text=True)
        return result, output, archive

    def test_manifest_uses_actual_architectures(self):
        for name, binary, universal in [('arm64', thin(), False), ('intel', thin(0x01000007), False),
                                         ('universal', fat(), True), ('universal64', fat('<', True), True)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                result, output, archive = self.run_manifest(Path(tmp), binary)
                self.assertEqual(result.returncode, 0, result.stderr)
                doc = json.loads(output.read_text())
                self.assertEqual(doc['universal'], universal)
                self.assertEqual(doc['asset_sha256'], lib.sha256_file(str(archive)))
                self.assertEqual(doc['version'], '1.2.0')
                self.assertEqual(doc['git_commit'], 'a'*40)
                self.assertTrue(lib.is_allowed_download_url(doc['asset_url']))
                self.assertEqual(lib.validate_manifest(doc), [])

    def test_explicit_universal_cannot_mislabel_thin_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, output, _ = self.run_manifest(Path(tmp), thin(), '--universal')
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(output.exists())

    def test_controlled_arm64_build_fallback_is_not_universal(self):
        source = (ROOT / 'macos_app/build_macos_app.sh').read_text()
        block = source[source.index('if swiftc -O'):source.index('# 4. 本地签名')]
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            commands = tmp / 'commands'
            commands.mkdir()
            compiler = commands / 'swiftc'
            compiler.write_text('#!/bin/sh\nexit 1\n')
            compiler.chmod(0o755)
            binaries = tmp / 'bin'
            binaries.mkdir()
            (binaries / 'EasyColab_arm64').write_bytes(thin())
            env = dict(os.environ, PATH=str(commands) + ':' + os.environ['PATH'],
                       MACOS_DIR=str(binaries), EXEC_NAME='EasyColab', SCRIPT_DIR=str(ROOT / 'macos_app'))
            result = subprocess.run(['bash', '-e', '-c', block], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            final = binaries / 'EasyColab'
            self.assertEqual(final.read_bytes(), thin())
            result, output, _ = self.run_manifest(tmp, final.read_bytes())
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(json.loads(output.read_text())['universal'])
