#!/usr/bin/env python3
import argparse
from pathlib import Path
import platform
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from job_scout.portability import install_tectonic_archive, tectonic_release


def main() -> int:
    parser = argparse.ArgumentParser(description="Install a pinned verified Tectonic binary")
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path.home() / ".local/bin/tectonic",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.destination.exists() and not args.force:
        print(f"Tectonic already exists: {args.destination}")
        return 0
    release = tectonic_release(platform.system(), platform.machine())
    request = urllib.request.Request(
        release.url, headers={"User-Agent": "job-search-portability-installer/1.0"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        declared = response.headers.get("Content-Length")
        if declared is not None and int(declared) > 64 * 1024 * 1024:
            raise ValueError("Tectonic download exceeds size limit")
        archive = response.read(64 * 1024 * 1024 + 1)
    install_tectonic_archive(
        archive,
        expected_sha256=release.sha256,
        destination=args.destination,
    )
    print(f"Installed verified Tectonic at {args.destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
