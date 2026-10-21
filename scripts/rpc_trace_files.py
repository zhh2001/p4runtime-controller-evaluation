"""Read the RPC trace catalog and its hashed archive members."""

import hashlib
import json
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]
TRACE_DATA = ROOT / "data/rpc/traces"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check_file(path, record):
    require(path.stat().st_size == record["bytes"], f"File size differs: {path}")
    require(file_hash(path) == record["sha256"], f"SHA-256 differs: {path}")


def load_catalog():
    catalog = json.loads((TRACE_DATA / "manifest.json").read_text())
    require(catalog["schema_version"] == 1, "Unknown trace catalog version")
    dataset = json.loads((ROOT / "data/rpc/manifest.json").read_text())
    require(file_hash(ROOT / "data/rpc/manifest.json") == catalog["dataset_manifest_sha256"],
            "Measurement manifest differs from the trace catalog")
    require([point["id"] for point in catalog["points"]] ==
            [point["id"] for point in dataset["points"]], "Trace parameter points differ")
    check_file(TRACE_DATA / "control.p4info.txt", catalog["p4info"])
    return catalog, dataset


def load_inventory(point):
    path = TRACE_DATA / (point["id"] + ".json")
    check_file(path, point["inventory"])
    inventory = json.loads(path.read_text())
    require(inventory["point"] == point["id"], "Inventory point differs")
    names = [item["name"] for item in inventory["files"]]
    require(len(names) == len(set(names)), "Duplicate archive member")
    for name in names:
        require(name.startswith(point["id"] + "/") and
                len(Path(name).parts) == 2 and ".." not in Path(name).parts,
                f"Invalid archive member name: {name}")
    return inventory


def archive_members(path, point, inventory):
    """Yield verified member bytes without extracting the archive."""
    check_file(path, point["archive"])
    with tarfile.open(path, mode="r|gz") as archive:
        for record in inventory["files"]:
            member = archive.next()
            require(member is not None and member.name == record["name"],
                    f"Archive member order differs: {record['name']}")
            require(member.isfile() and member.size == record["bytes"],
                    f"Archive member type or size differs: {member.name}")
            stream = archive.extractfile(member)
            require(stream is not None, f"Cannot read archive member: {member.name}")
            with stream:
                data = stream.read()
            require(len(data) == record["bytes"] and
                    hashlib.sha256(data).hexdigest() == record["sha256"],
                    f"Archive member hash differs: {member.name}")
            yield record, data
        require(archive.next() is None, "Unexpected extra archive member")


def selected_points(catalog, names):
    require(len(names) == len(set(names)), "Choose distinct parameter points")
    require(names == ["all"] or "all" not in names, "Use all on its own")
    known = {point["id"] for point in catalog["points"]}
    require(names == ["all"] or set(names) <= known, "Unknown parameter point")
    return [point for point in catalog["points"] if names == ["all"] or point["id"] in names]
