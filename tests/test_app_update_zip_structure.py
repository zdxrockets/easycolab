"""Runs the exact Swift ZIP parser when swiftc is available, never a Python port."""
import io
import shutil
import stat
import struct
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

from tests.test_app_update_binary import ROOT, BINARY, thin, swift_function


def ordinary_zip(comment=b'', payload=b'', entry_comment=b''):
    out = io.BytesIO()
    with zipfile.ZipFile(out, 'w', allowZip64=False) as z:
        z.writestr(BINARY, thin())
        info = zipfile.ZipInfo('EasyColab.app/Contents/Resources/note')
        info.comment = entry_comment
        z.writestr(info, payload)
        z.comment = comment
    return out.getvalue()


def zip_vectors():
    ordinary = ordinary_zip()
    eocd = len(ordinary) - 22
    _, _, _, n1, n2, cdsize, cdoffset, _ = struct.unpack_from('<4s4H2IH', ordinary, eocd)
    record = struct.pack('<IQ2H2I4Q', 0x06064b50, 44, 45, 45, 0, 0, n1, n2, cdsize, cdoffset)
    locator = struct.pack('<IIQI', 0x07064b50, 0, eocd, 1)
    zip64 = ordinary[:eocd] + record + locator + ordinary[eocd:]
    sentinel = bytearray(ordinary)
    struct.pack_into('<HHII', sentinel, eocd + 8, 0xffff, 0xffff, 0xffffffff, 0xffffffff)
    # Separate unsafe archive fixtures avoid changing name lengths by replacement.
    def unsafe(name, mode=0o100644):
        out = io.BytesIO()
        with zipfile.ZipFile(out, 'w') as z:
            z.writestr(BINARY, thin())
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = mode << 16
            z.writestr(info, b'bad')
        return out.getvalue()
    encrypted = bytearray(ordinary)
    # Encryption flag in the first central directory entry.
    struct.pack_into('<H', encrypted, cdoffset + 8, 1)
    return [('ordinary', ordinary, True),
            ('payload_magic', ordinary_zip(payload=b'PK\x06\x07' + bytes(20)), True),
            ('archive_comment_magic', ordinary_zip(comment=b'PK\x06\x07 comment'), True),
            ('entry_comment_magic_at_locator_position', ordinary_zip(entry_comment=b'PK\x06\x07' + bytes(16)), True),
            ('eocd_magic_in_comment', ordinary_zip(comment=b'PK\x05\x06 text'), True),
            ('zip64_locator', zip64, False), ('zip64_sentinels', bytes(sentinel), False),
            ('slip', unsafe('../evil'), False),
            ('symlink', unsafe('EasyColab.app/link', stat.S_IFLNK | 0o777), False),
            ('encrypted', bytes(encrypted), False)]


def swift_struct(source, name):
    start = source.index('struct ' + name + ':') if 'struct ' + name + ':' in source else source.index('struct ' + name + ' {')
    start = source.rfind('\n', 0, start) + 1
    end = source.index('\n}', start) + 2
    return source[start:end]


class SwiftZipStructureTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which('swiftc'), 'swiftc unavailable; ZIP parser not compiled/run')
    def test_real_swift_zip_vectors(self):
        core = (ROOT / 'macos_app/Sources/AppUpdateCore.swift').read_text()
        functions = ['inspectZipCentralDirectory', 'findEndOfCentralDirectory', 'zipEntryIsSafe',
                     'validateZipEntries', 'u16', 'u32']
        if 'func hasZip64Locator(' in core:
            functions += ['hasZip64Locator', 'u64Value', 'u32BE']
        source = 'import Foundation\nenum AppUpdateError: Error { case unsafeZip([String]) }\n'
        source += '\n'.join(swift_struct(core, n) for n in ('EOCD', 'ZipEntryInfo'))
        source += '\n' + '\n'.join(swift_function(core, n) for n in functions)
        source += '''
for path in CommandLine.arguments.dropFirst() {
    do { print(validateZipEntries(try inspectZipCentralDirectory(at: URL(fileURLWithPath: path))).isEmpty ? "OK" : "REJECT") }
    catch { print("REJECT") }
}
'''
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / 'main.swift').write_text(source)
            subprocess.run(['swiftc', str(tmp / 'main.swift'), '-o', str(tmp / 'check')], check=True, capture_output=True)
            vectors = zip_vectors()
            paths = []
            for i, (_, data, _) in enumerate(vectors):
                path = tmp / str(i)
                path.write_bytes(data)
                paths.append(str(path))
            result = subprocess.run([str(tmp / 'check'), *paths], check=True, capture_output=True, text=True)
            self.assertEqual(result.stdout.splitlines(), ['OK' if ok else 'REJECT' for _, _, ok in vectors])
