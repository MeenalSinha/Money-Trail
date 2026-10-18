"""Shared test support. All data here is generated test data, never real financial records."""
import copy
import json
from pathlib import Path

import pytest

from money_trail.generation import generate, load_generator_config, write_dataset
from support import generator_dict


class Dataset:
    def __init__(self, directory: Path, ingest_config: Path, manifest, gen_cfg):
        self.dir, self.ingest_config, self.manifest, self.gen_cfg = directory, ingest_config, manifest, gen_cfg

    def edit_ingest(self, **ingestion_overrides) -> None:
        raw = json.loads(self.ingest_config.read_text())
        raw["ingestion"].update(ingestion_overrides)
        self.ingest_config.write_text(json.dumps(raw))

    def rewrite_csv(self, name: str, fn) -> None:
        """Apply fn(list_of_rows_including_header) -> rows to one CSV, preserving the format."""
        import csv, io
        path = self.dir / name
        rows = list(csv.reader(io.StringIO(path.read_text())))
        rows = fn(rows)
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerows(rows)
        path.write_text(buf.getvalue())

    def rows(self, name: str):
        import csv
        with open(self.dir / name, newline="") as f:
            return list(csv.DictReader(f))


@pytest.fixture
def make_dataset(tmp_path):
    counter = {"n": 0}

    def _make(namespace="test-ns", seed=1, **over) -> Dataset:
        counter["n"] += 1
        root = tmp_path / f"ds{counter['n']}"
        root.mkdir()
        raw = generator_dict(namespace, seed, **over)
        (root / "gen.json").write_text(json.dumps(raw))
        cfg = load_generator_config(root / "gen.json")
        manifest = write_dataset(cfg, generate(cfg))
        d = cfg.output_dir / namespace
        return Dataset(d, d / "ingest_config.json", manifest, cfg)

    return _make


@pytest.fixture(autouse=True)
def _restore_logging():
    """CLI commands reconfigure logging; keep that from leaking handlers into other tests."""
    import logging
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
