"""Binary vectors shared by the real Python packager and Foundation-only Swift parser.

Swift checks are skipped when swiftc is absent. No Python port is a Swift test.
"""
import json
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import app_update_lib as lib

BINARY = 'EasyColab.app/Contents/MacOS/EasyColab'


def thin(cpu=0x0100000C, endian='<'):
    return struct.pack(endian + '8I', 0xFEEDFACF, cpu, 0, 2, 0, 0, 0, 0)


def fat(endian='>', wide=False):
    rows = []
    slices = [thin(), thin(0x01000007)]
    offset = 8 + (32 if wide else 20) * 2
    for cpu, data in zip((0x0100000C, 0x01000007), slices):
        rows.append(struct.pack(endian + ('IIQQII' if wide else 'IIIII'),
                                cpu, 0, offset, len(data), 0, *([0] if wide else [])))
        offset += len(data)
    return struct.pack(endian + 'II', 0xCAFEBABF if wide else 0xCAFEBABE, 2) + b''.join(rows + slices)


def binary_vectors():
    vectors = [('thin_arm64', thin(), ['arm64']),
               ('thin_x86_64', thin(0x01000007), ['x86_64']),
               ('thin_big_endian', thin(endian='>'), ['arm64'])]
    for endian in ('<', '>'):
        for wide in (False, True):
            vectors.append((f'fat_{endian}_{wide}', fat(endian, wide), ['arm64', 'x86_64']))
    vectors += [('truncated', thin()[:8], []),
                ('i386', struct.pack('<7I', 0xFEEDFACE, 7, 3, 2, 0, 0, 0), []),
                ('arm32', struct.pack('<7I', 0xFEEDFACE, 12, 0, 2, 0, 0, 0), []),
                ('bad_abi', thin(7), []),
                ('empty_fat', struct.pack('>II', 0xCAFEBABE, 0), []),
                ('truncated_table', struct.pack('>II', 0xCAFEBABE, 2) + bytes(20), []),
                ('zero_slices', struct.pack('>II', 0xCAFEBABE, 2) + b''.join(
                    struct.pack('>IIIII', cpu, 0, 0, 0, 0) for cpu in (0x0100000C, 0x01000007)), [])]
    for name, at, value in [('past_end', 16, 2**32 - 1), ('wrong_cpu', 8, 0x01000007),
                            ('overlap', 36, 48), ('unaligned', 24, 12)]:
        data = bytearray(fat())
        struct.pack_into('>I', data, at, value)
        vectors.append((name, bytes(data), []))
    vectors.append(('truncated_load_commands', struct.pack('<8I', 0xFEEDFACF, 0x0100000C, 0, 2, 1, 8, 0, 0), []))
    return vectors


def swift_function(source, name):
    start = source.index('func ' + name + '(')
    private = source.rfind('\n', 0, start) + 1
    start = private
    brace = source.index('{', start)
    depth = 1
    end = brace + 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start:end]


class MachORegressionTest(unittest.TestCase):
    def test_packager_rejects_invalid_executables(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'test.zip'
            for name, data, expected in binary_vectors():
                with self.subTest(name=name):
                    with zipfile.ZipFile(path, 'w') as archive:
                        archive.writestr(BINARY, data)
                    result = lib.inspect_zip(str(path))
                    self.assertEqual(result['ok'], bool(expected), result)
                    if expected:
                        self.assertEqual(sorted(result['architectures']), sorted(expected))

    @unittest.skipUnless(shutil.which('swiftc'), 'swiftc unavailable; not a Swift compile/run')
    def test_real_swift_macho_vectors(self):
        core = (ROOT / 'macos_app/Sources/AppUpdateCore.swift').read_text()
        names = ['inspectMachOArchitectures', 'u32', 'u32BE', 'cpuTypeName']
        # The fixed parser factors out thin-slice validation and 64-bit fat reads.
        for name in ('inspectThinMachO', 'u64Value'):
            if 'func ' + name + '(' in core:
                names.append(name)
        source = 'import Foundation\nenum AppUpdateError: Error { case bundleValidation([String]) }\n'
        source += '\n'.join(swift_function(core, name) for name in names)
        source += '''
for path in CommandLine.arguments.dropFirst() {
    do { print(try inspectMachOArchitectures(at: URL(fileURLWithPath: path)).sorted().joined(separator: ",")) }
    catch { print("REJECT") }
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / 'main.swift').write_text(source)
            subprocess.run(['swiftc', str(tmp / 'main.swift'), '-o', str(tmp / 'check')], check=True, capture_output=True)
            vectors = binary_vectors()
            paths = []
            for i, (_, data, _) in enumerate(vectors):
                path = tmp / str(i)
                path.write_bytes(data)
                paths.append(str(path))
            result = subprocess.run([str(tmp / 'check'), *paths], check=True, capture_output=True, text=True)
            self.assertEqual(result.stdout.splitlines(), [','.join(sorted(e)) if e else 'REJECT' for _, _, e in vectors])


if __name__ == '__main__':
    unittest.main()
