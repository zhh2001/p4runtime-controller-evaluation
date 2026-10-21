"""Download selected RPC trace archives and check their SHA-256 hashes."""

import argparse
import hashlib
from pathlib import Path
import urllib.request

from rpc_trace_files import ROOT, check_file, load_catalog, require, selected_points


def fetch_asset(url, destination, record):
    if destination.exists():
        check_file(destination, record)
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    require(not temporary.exists(), f"Partial download exists: {temporary}")
    digest = hashlib.sha256()
    size = 0
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "p4runtime-controller-evaluation"})
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open("xb") as stream:
            while block := response.read(1024 * 1024):
                size += len(block)
                require(size <= record["bytes"], "Download exceeds the recorded size")
                digest.update(block)
                stream.write(block)
        require(size == record["bytes"] and digest.hexdigest() == record["sha256"],
                f"Downloaded size or SHA-256 differs: {destination.name}")
        temporary.replace(destination)
        return True
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--points", nargs="+", default=["default"], help="Parameter names, or all")
    parser.add_argument("--directory", type=Path, default=ROOT / "results/rpc-traces")
    args = parser.parse_args()
    catalog, _ = load_catalog()
    for point in selected_points(catalog, args.points):
        record = point["archive"]
        destination = args.directory / record["name"]
        print(f"{point['id']}: {record['bytes'] / 1024**2:.1f} MiB", flush=True)
        downloaded = fetch_asset(catalog["download_base"] + "/" + record["name"], destination, record)
        print(f"{'Downloaded and verified' if downloaded else 'Already verified'} {destination}", flush=True)


if __name__ == "__main__":
    main()
