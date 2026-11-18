"""Shared rules for EasyColab macOS self-update manifests, versions, and URLs.

The macOS client reimplements these rules in Swift (AppUpdateCore.swift).
Keep the two sides aligned via tests/fixtures/app_update/vectors.json.
This module does not talk to GitHub and does not install anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse

UPDATE_REPO_OWNER = "zdxrockets"
UPDATE_REPO_NAME = "easycolab"
BUNDLE_ID = "app.easycolab.studio.macos"
ASSET_NAME = "EasyColab-macOS.zip"
MANIFEST_NAME = "EasyColab-macOS.manifest.json"
MIN_OS = "13.0"
MANIFEST_SCHEMA = 1

_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
_SAFE_TAG_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SAFE_ASSET_RE = re.compile(r"^[A-Za-z0-9._-]+\.zip$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def parse_semver(text: str) -> Optional[Tuple[int, int, int]]:
    raw = (text or "").strip()
    match = _SEMVER_RE.match(raw)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def compare_semver(left: str, right: str) -> Optional[int]:
    """Return -1/0/1 if both parse, else None."""
    a = parse_semver(left)
    b = parse_semver(right)
    if a is None or b is None:
        return None
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


def normalize_commit(commit: str) -> str:
    return re.sub(r"[^0-9a-fA-F]", "", commit or "").lower()


def commits_match(current: str, candidate: str) -> bool:
    a = normalize_commit(current)
    b = normalize_commit(candidate)
    if len(a) < 7 or len(b) < 7:
        return False
    return a.startswith(b) or b.startswith(a)


def compare_identities(current: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Decide whether candidate is a newer installable build than the running app."""
    current_version = str(current.get("version") or "")
    candidate_version = str(candidate.get("version") or current.get("release_tag") or candidate.get("release_tag") or "")
    if not candidate.get("version") and candidate.get("release_tag"):
        candidate_version = str(candidate.get("release_tag"))
    # Prefer explicit version, fall back to release_tag for either side.
    if not current_version:
        current_version = str(current.get("release_tag") or "")
    if not candidate_version:
        candidate_version = str(candidate.get("release_tag") or "")

    current_commit = str(current.get("git_commit") or "")
    candidate_commit = str(candidate.get("git_commit") or "")
    stamped = bool(normalize_commit(current_commit) and len(normalize_commit(current_commit)) >= 7)

    cmp = compare_semver(current_version, candidate_version)
    if cmp is None:
        return {
            "status": "incomparable",
            "newer": False,
            "reason": "unparsed_version",
            "current_stamped": stamped,
        }
    if cmp < 0:
        return {"status": "newer_version", "newer": True, "reason": "semver", "current_stamped": stamped}
    if cmp > 0:
        return {"status": "older_or_equal", "newer": False, "reason": "installed_is_newer", "current_stamped": stamped}
    if stamped and candidate_commit:
        if commits_match(current_commit, candidate_commit):
            return {"status": "same_build", "newer": False, "reason": "same_commit", "current_stamped": True}
        return {
            "status": "same_version_different_build",
            "newer": True,
            "reason": "commit_differs",
            "current_stamped": True,
        }
    # Equal version strings, but the running app has no commit stamp (the 1.1.0/1.1.1
    # mis-label case). Do not loop-offer the same version as an update.
    return {
        "status": "same_version_unstamped",
        "newer": False,
        "reason": "equal_version_without_commit_stamp",
        "current_stamped": False,
    }


def is_allowed_releases_api_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "api.github.com":
        return False
    if parsed.username or parsed.password or (parsed.port not in (None, 443)):
        return False
    path = parsed.path.rstrip("/")
    prefix = f"/repos/{UPDATE_REPO_OWNER}/{UPDATE_REPO_NAME}/releases"
    return path == prefix or path.startswith(prefix + "/")


def is_allowed_download_url(url: str, asset_name: str = ASSET_NAME) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "github.com":
        return False
    if parsed.username or parsed.password or (parsed.port not in (None, 443)):
        return False
    if parsed.query or parsed.fragment:
        return False
    parts = [p for p in parsed.path.split("/") if p]
    # /zdxrockets/easycolab/releases/download/<tag>/<asset>
    if len(parts) != 6:
        return False
    if parts[0] != UPDATE_REPO_OWNER or parts[1] != UPDATE_REPO_NAME:
        return False
    if parts[2] != "releases" or parts[3] != "download":
        return False
    tag, asset = parts[4], parts[5]
    if not _SAFE_TAG_RE.match(tag) or ".." in tag:
        return False
    expected = asset_name or ASSET_NAME
    if asset != expected or not _SAFE_ASSET_RE.match(asset):
        return False
    return True


def validate_manifest(doc: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if int(doc.get("schema") or 0) != MANIFEST_SCHEMA:
        errors.append("unsupported_schema")
    version = str(doc.get("version") or "")
    tag = str(doc.get("release_tag") or "")
    if parse_semver(version) is None:
        errors.append("invalid_version")
    if not _SAFE_TAG_RE.match(tag) or ".." in tag:
        errors.append("invalid_release_tag")
    if str(doc.get("bundle_id") or "") != BUNDLE_ID:
        errors.append("bundle_id_mismatch")
    if str(doc.get("asset_name") or "") != ASSET_NAME:
        errors.append("unexpected_asset_name")
    if not _SHA256_RE.match(str(doc.get("asset_sha256") or "")):
        errors.append("invalid_sha256")
    asset_url = str(doc.get("asset_url") or "")
    if asset_url and not is_allowed_download_url(asset_url, ASSET_NAME):
        errors.append("asset_url_not_allowed")
    min_os = str(doc.get("min_os") or MIN_OS)
    if parse_semver(min_os if min_os.count(".") == 2 else min_os + ".0") is None and parse_semver(min_os + ".0") is None:
        # allow 13.0
        if not re.match(r"^\d+\.\d+$", min_os):
            errors.append("invalid_min_os")
    return errors


def build_manifest(
    *,
    version: str,
    release_tag: str,
    git_commit: str,
    sha256_hex: str,
    asset_url: str,
    notes: str = "",
    min_os: str = MIN_OS,
    universal: bool = True,
    built_at: str = "",
) -> Dict[str, Any]:
    doc = {
        "schema": MANIFEST_SCHEMA,
        "repository": f"{UPDATE_REPO_OWNER}/{UPDATE_REPO_NAME}",
        "release_tag": release_tag,
        "version": version,
        "git_commit": git_commit,
        "asset_name": ASSET_NAME,
        "asset_sha256": sha256_hex.lower(),
        "asset_url": asset_url,
        "bundle_id": BUNDLE_ID,
        "min_os": min_os,
        "universal": bool(universal),
        "notes": notes,
        "built_at": built_at,
        "signing": "adhoc",
        "signing_note": (
            "This build is ad-hoc signed (codesign --sign -). It is not Apple-notarized. "
            "SHA-256 only proves the zip matches this manifest from the same GitHub release."
        ),
    }
    errors = validate_manifest(doc)
    if errors:
        raise ValueError(f"invalid manifest: {errors}")
    return doc


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def zip_entry_is_safe(name: str, is_symlink: bool = False, is_encrypted: bool = False) -> Tuple[bool, str]:
    if is_encrypted:
        return False, "encrypted"
    if is_symlink:
        return False, "symlink"
    if not name or "\x00" in name:
        return False, "empty_or_null"
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("~"):
        return False, "absolute"
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return False, "dotdot"
    if not parts:
        return False, "empty"
    top = parts[0]
    if top not in ("EasyColab.app", "__MACOSX", ".DS_Store"):
        return False, "unexpected_root"
    return True, "ok"


def inspect_macho_architectures(data: bytes) -> List[str]:
    """Read supported 64-bit executable headers and validate fat slice bounds.

    Keep in sync with inspectMachOArchitectures in AppUpdateCore.swift.
    This is structural architecture inspection, not a code-signature check.
    """
    import struct

    def thin(start: int, size: int, expected_cpu: Optional[int] = None) -> str:
        if size < 32:
            raise ValueError("truncated_macho_header")
        magic = data[start:start + 4]
        endian = {b'\xcf\xfa\xed\xfe': '<', b'\xfe\xed\xfa\xcf': '>'}.get(magic)
        if endian is None:
            raise ValueError("unsupported_macho_magic")
        _, cpu, _, filetype, ncmds, command_bytes, _, _ = struct.unpack_from(endian + '8I', data, start)
        arch = {0x0100000C: 'arm64', 0x01000007: 'x86_64'}.get(cpu)
        if arch is None or (expected_cpu is not None and cpu != expected_cpu) or filetype != 2:
            raise ValueError("unsupported_macho_cpu_or_type")
        if command_bytes > size - 32 or ncmds > command_bytes // 8:
            raise ValueError("truncated_load_commands")
        cursor, end = start + 32, start + 32 + command_bytes
        for _ in range(ncmds):
            if end - cursor < 8:
                raise ValueError("truncated_load_command")
            cmd_size = struct.unpack_from(endian + 'I', data, cursor + 4)[0]
            if cmd_size < 8 or cmd_size % 8 or cmd_size > end - cursor:
                raise ValueError("invalid_load_command_size")
            cursor += cmd_size
        if cursor != end:
            raise ValueError("load_command_size_mismatch")
        return arch

    if len(data) < 8:
        raise ValueError("binary_too_small")
    magic = struct.unpack_from('>I', data)[0]
    formats = {0xCAFEBABE: ('>', False), 0xBEBAFECA: ('<', False),
               0xCAFEBABF: ('>', True), 0xBFBAFECA: ('<', True)}
    if magic not in formats:
        return [thin(0, len(data))]
    endian, wide = formats[magic]
    count = struct.unpack_from(endian + 'I', data, 4)[0]
    row_size = 32 if wide else 20
    if count == 0 or count > (len(data) - 8) // row_size:
        raise ValueError("invalid_fat_table")
    table_end = 8 + count * row_size
    arches, ranges = [], []
    for i in range(count):
        row = 8 + i * row_size
        cpu, _, start, size, align, *_ = struct.unpack_from(endian + ('IIQQII' if wide else 'IIIII'), data, row)
        if (start < table_end or start > len(data) or size > len(data) - start
                or size < 32 or align > 63 or start % (1 << align)):
            raise ValueError("invalid_fat_slice_bounds")
        if any(start < end and prior < start + size for prior, end in ranges):
            raise ValueError("overlapping_fat_slices")
        ranges.append((start, start + size))
        arch = thin(start, size, cpu)
        if arch in arches:
            raise ValueError("duplicate_fat_architecture")
        arches.append(arch)
    return arches


def inspect_zip(path: str) -> Dict[str, Any]:
    entries = []
    errors = []
    has_binary = False
    architectures = []
    with zipfile.ZipFile(path, "r") as zf:
        for info in zf.infolist():
            # Unix symlink: symlinks have create_system == 3 and (external_attr >> 16) & 0o170000 == 0o120000
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            is_symlink = (unix_mode & 0o170000) == 0o120000
            is_encrypted = bool(info.flag_bits & 0x1)
            ok, reason = zip_entry_is_safe(info.filename, is_symlink=is_symlink, is_encrypted=is_encrypted)
            entries.append(
                {
                    "name": info.filename,
                    "is_symlink": is_symlink,
                    "is_encrypted": is_encrypted,
                    "ok": ok,
                    "reason": reason,
                }
            )
            if not ok:
                errors.append(f"{info.filename}:{reason}")
            if info.filename.replace("\\", "/") == "EasyColab.app/Contents/MacOS/EasyColab":
                has_binary = True
                if ok:
                    try:
                        architectures = inspect_macho_architectures(zf.read(info))
                    except ValueError as exc:
                        errors.append(f"invalid_app_binary:{exc}")
    if not has_binary:
        errors.append("missing_app_binary")
    return {"entries": entries, "errors": errors, "ok": not errors, "architectures": architectures}


def pick_latest_stable_release(releases: Iterable[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """releases: GitHub API release objects. Ignore drafts and prereleases."""
    best = None
    best_ver = None
    for rel in releases:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        tag = str(rel.get("tag_name") or "")
        ver = parse_semver(tag)
        if ver is None:
            continue
        asset_names = {a.get("name") for a in (rel.get("assets") or [])}
        if ASSET_NAME not in asset_names or MANIFEST_NAME not in asset_names:
            # Source-only or legacy zip without a manifest cannot be auto-installed.
            continue
        if best_ver is None or ver > best_ver:
            best = rel
            best_ver = ver
    return best
