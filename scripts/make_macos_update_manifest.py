#!/usr/bin/env python3
"""Build EasyColab-macOS.manifest.json next to a zip. Used by the tag/workflow_dispatch CI job."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import app_update_lib as lib


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--notes", default="")
    parser.add_argument("--built-at", default="")
    parser.add_argument("--universal", action="store_true", default=True)
    args = parser.parse_args()

    zip_path = Path(args.zip)
    if not zip_path.is_file():
        print(f"zip not found: {zip_path}", file=sys.stderr)
        return 1
    inspect = lib.inspect_zip(str(zip_path))
    if not inspect["ok"]:
        print(f"zip failed safety inspection: {inspect['errors']}", file=sys.stderr)
        return 1
    sha = lib.sha256_file(str(zip_path))
    url = (
        f"https://github.com/{lib.UPDATE_REPO_OWNER}/{lib.UPDATE_REPO_NAME}"
        f"/releases/download/{args.tag}/{lib.ASSET_NAME}"
    )
    doc = lib.build_manifest(
        version=args.version,
        release_tag=args.tag,
        git_commit=args.commit,
        sha256_hex=sha,
        asset_url=url,
        notes=args.notes,
        built_at=args.built_at,
        universal=True,
    )
    out = Path(args.out)
    out.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"sha256": sha, "manifest": str(out)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
