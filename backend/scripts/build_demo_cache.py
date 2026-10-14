"""Build a SYNTHETIC demo data directory for the frontend and its browser tests.

Runs the real Phase 2-5 pipelines on the ~1 km synthetic site used by
``tests/test_api.py`` (pond, accelerating block, seepage toe, a vegetated patch
and a decorrelated patch) inside the Sierrita bbox. Every product is labelled
synthetic/mock, so the API and the map watermark report it as simulated.

Usage::

    python backend/scripts/build_demo_cache.py --data-dir /tmp/lithocreep-demo
    DATA_DIR=/tmp/lithocreep-demo uvicorn app.main:app --port 8000   # from backend/
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.config import Settings  # noqa: E402
from tests.test_api import DECORRELATED, SITE, VEGETATED  # noqa: E402
from tests.test_coupling import build_full_chain  # noqa: E402


def stamp_synthetic(path: Path) -> None:
    """Mark a product as synthetic so the API (and the map watermark) never call it authentic.

    The demo DISP-S1 and DSWx inputs reuse real OPERA file-name patterns, so without the
    stamp their provenance would read as genuine OPERA products.
    """
    import zarr

    group = zarr.open_group(str(path), mode="r+")
    group.attrs.update({"synthetic_or_mock": 1, "synthetic_note": "demo site from backend/scripts/build_demo_cache.py"})
    zarr.consolidate_metadata(str(path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", type=Path, required=True, help="DATA_DIR to (re)create")
    parser.add_argument("--force", action="store_true", help="rebuild even if the coupled product exists")
    args = parser.parse_args(argv)

    data_dir = args.data_dir.resolve()
    if (data_dir / "cache" / "Sierrita_coupled.zarr").exists() and not args.force:
        print(f"{data_dir} already holds the demo products (use --force to rebuild)")
        return 0
    if data_dir.exists():
        shutil.rmtree(data_dir)
    settings = Settings(DATA_DIR=str(data_dir))
    with tempfile.TemporaryDirectory() as work:
        chain = build_full_chain(Path(work), facility="Sierrita", bbox=SITE, settings=settings,
                                 vegetated=VEGETATED, decorrelated=DECORRELATED)
    for product in sorted(settings.cache_dir.glob("Sierrita_*.zarr")):
        stamp_synthetic(product)
    print(f"SYNTHETIC demo products written to {settings.cache_dir}")
    print(f"regime counts: {chain['summary'].get('regime_counts')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
