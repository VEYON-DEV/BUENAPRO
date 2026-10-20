"""Explicit, finite DIN IA/industrial reader and matching coordinator."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers/seace/src"))

from buenapro_worker.prod2.din_sector_chain import main

if __name__ == "__main__":
    raise SystemExit(main(project_root=ROOT))
