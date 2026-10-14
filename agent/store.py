"""Run persistence: one JSON state file per run, written atomically (tmp + fsync + rename)."""
import json
import os
from pathlib import Path


class Store:
    def __init__(self, root: str):
        self.root = Path(root)

    def _write(self, path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def save(self, state: dict) -> None:
        self._write(self.root / state["run_id"] / "state.json", state)

    def load(self, run_id: str) -> dict:
        path = self.root / run_id / "state.json"
        if not path.exists():
            raise FileNotFoundError(f"run {run_id} not found in {self.root}")
        return json.loads(path.read_text())

    def put_blob(self, run_id: str, name: str, data) -> None:
        self._write(self.root / run_id / name, data)

    def get_blob(self, run_id: str, name: str):
        return json.loads((self.root / run_id / name).read_text())

    def runs(self) -> list[str]:
        return sorted(p.parent.name for p in self.root.glob("*/state.json"))
