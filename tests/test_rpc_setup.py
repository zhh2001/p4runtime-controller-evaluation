import hashlib
import importlib.util
import io
import json
from pathlib import Path
import random
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("bootstrap_rpc", ROOT / "scripts/bootstrap_rpc.py")
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


class SetupTests(unittest.TestCase):
    def test_parameters_and_order_match_published_runs(self):
        config = json.loads((ROOT / "config/rpc.json").read_text())
        dataset = json.loads((ROOT / "data/rpc/manifest.json").read_text())
        self.assertEqual(config["profiles"], dataset["profiles"])
        self.assertEqual(config["bootstrap"], dataset["bootstrap"])
        self.assertEqual(config["modes"]["full"], dict(
            repetitions=dataset["repetitions"], warmup_seconds=5, measurement_seconds=30))
        points = {point["id"]: point for point in dataset["points"]}
        self.assertEqual(set(points), {point["id"] for point in config["points"]})
        for point in config["points"]:
            saved = points[point["id"]]
            for key in ("n", "batch", "concurrency"):
                self.assertEqual(point[key], saved[key])
            rng = random.Random(config["random_seed"])
            order = []
            for _ in range(dataset["repetitions"]):
                profiles = list(config["profiles"])
                rng.shuffle(profiles)
                order.append(profiles)
            self.assertEqual(order, saved["profile_order"])

    def test_exported_sources_match_inventory(self):
        provenance = json.loads((ROOT / "config/source-provenance.json").read_text())
        for name, record in provenance["files"].items():
            path = ROOT / "experiments/performance" / name
            self.assertEqual(bootstrap.digest(path), record["export_sha256"], name)
            if record["unchanged"]:
                self.assertEqual(record["formal_sha256"], record["export_sha256"], name)
        for stack in ("go-sdk", "go-legacy"):
            for name in ("shared.go", "counts_test.go"):
                base = ROOT / "experiments/performance"
                self.assertEqual((base / name).read_bytes(), (base / stack / name).read_bytes())

    def test_archive_and_installed_source_are_verified(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)
            downloads = runtime / "downloads"
            downloads.mkdir()
            archive = downloads / "client.tar.gz"
            with tarfile.open(archive, "w:gz") as packed:
                item = tarfile.TarInfo("client-revision/client.py")
                content = b"VERSION = 1\n"
                item.size = len(content)
                packed.addfile(item, io.BytesIO(content))
            source = dict(name="client", sha256=bootstrap.digest(archive))
            with patch.object(bootstrap, "RUNTIME", runtime):
                expected = bootstrap.install_source(source)
                self.assertEqual(expected["client.py"], hashlib.sha256(content).hexdigest())
                self.assertEqual(bootstrap.install_source(source), expected)
                (runtime / "sources/client/client.py").write_text("VERSION = 2\n")
                with self.assertRaisesRegex(ValueError, "Source checkout differs"):
                    bootstrap.install_source(source)
                archive.write_bytes(b"changed archive")
                with self.assertRaisesRegex(ValueError, "Archive hash differs"):
                    bootstrap.install_source(source)


if __name__ == "__main__":
    unittest.main()
