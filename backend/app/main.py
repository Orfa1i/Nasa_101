"""LithoCreep command-line interface.

Usage (from the repository root or from ``backend/``)::

    python backend/app/main.py ingest --facility Sierrita --bbox -111.12 31.83 -111.04 31.90
    python backend/app/main.py unmix --facility Sierrita

``ingest`` runs ``download_and_crop`` for each requested product, writes the
clipped rasters to ``DATA_DIR/processed/<facility>/``, records the run on the
facility's feature in ``DATA_DIR/global_tailings.geojson`` and logs to
``DATA_DIR/logs/ingest_<run_id>.log``.

``unmix`` runs the spectroscopy engine on the facility's EMIT L2A output from
``ingest`` and writes ``DATA_DIR/cache/<Facility>_spectral.zarr`` plus an
RMSE/validation JSON log in ``DATA_DIR/logs/``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

if __package__ in (None, ""):  # executed as a script: make the ``app`` package importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import geopandas as gpd
import pandas as pd
import pyogrio
from shapely.geometry import box

from app.config import Settings, get_settings
from app.pipelines.ingestion import PRODUCTS, download_and_crop, validate_bbox
from app.pipelines.unmixing import find_l2bmin_for, run_spectral_pipeline

logger = logging.getLogger("lithocreep")

KNOWN_FACILITIES = {
    "sierrita": {
        "name": "Sierrita",
        "full_name": "Freeport-McMoRan Sierrita TSF",
        "country": "USA",
        "bbox": (-111.12, 31.83, -111.04, 31.90),
    },
    "mission": {
        "name": "Mission",
        "full_name": "ASARCO Mission Complex TSF",
        "country": "USA",
        "bbox": (-111.08, 31.94, -111.00, 32.01),
    },
    "brumadinho": {
        "name": "Brumadinho",
        "full_name": "Brumadinho Dam I (historical backtest)",
        "country": "Brazil",
        "bbox": (-44.15, -20.14, -44.10, -20.10),
    },
}

FACILITY_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.\-]{0,63}$")
SUCCESS_STATUSES = {"downloaded", "mock", "cache_fallback"}
PRODUCT_SLUGS = {
    "EMITL2ARFL.001": "emit_l2a_rfl",
    "OPERA_L3_DISP-S1_V1": "opera_disp_s1",
    "OPERA_L3_DSWX-HLS_V1.0": "opera_dswx_hls",
}


def facility_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _facility_name(value: str) -> str:
    if not FACILITY_NAME_RE.match(value):
        raise argparse.ArgumentTypeError(
            "facility must be 1-64 characters of letters, digits, spaces, '_', '.' or '-'"
        )
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lithocreep", description="LithoCreep TSF screening pipeline")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="Download, reproject and clip NASA granules for a facility")
    ingest.add_argument("--facility", required=True, type=_facility_name, help="Facility name, e.g. Sierrita")
    ingest.add_argument(
        "--bbox",
        nargs=4,
        type=float,
        metavar=("XMIN", "YMIN", "XMAX", "YMAX"),
        help="EPSG:4326 bounding box; optional for known facilities (%s)" % ", ".join(sorted(KNOWN_FACILITIES)),
    )
    ingest.add_argument(
        "--datasets",
        nargs="+",
        choices=sorted(PRODUCTS),
        default=list(PRODUCTS),
        help="Products to ingest (default: all)",
    )
    ingest.add_argument("--format", choices=("tif", "zarr"), default="tif", help="Output format (default: tif)")
    ingest.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))

    unmix = sub.add_parser("unmix", help="NNLS-unmix a facility's EMIT reflectance and compute the SAI")
    unmix.add_argument("--facility", required=True, type=_facility_name, help="Facility name, e.g. Sierrita")
    unmix.add_argument("--input", type=Path, help="EMIT reflectance (.zarr/.tif); default: the ingest output")
    unmix.add_argument("--output", type=Path, help="Output Zarr; default: DATA_DIR/cache/<Facility>_spectral.zarr")
    unmix.add_argument("--l2bmin", type=Path, help="EMITL2BMIN NetCDF for validation; default: matching file in DATA_DIR/raw/EMITL2BMIN")
    unmix.add_argument("--library", type=Path, help="USGS library extract CSV; default: splib07 extract if present, else bundled splib06a")
    unmix.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return parser


def configure_logging(log_file: Path, level: str) -> list[logging.Handler]:
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers: list[logging.Handler] = [logging.FileHandler(log_file), logging.StreamHandler(sys.stderr)]
    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(level)
    return handlers


def update_tailings_registry(
    registry_path: Path,
    facility: str,
    bbox: Sequence[float],
    run_id: str,
    run_utc: str,
    results: list[dict],
) -> None:
    """Upsert the facility's feature (bbox polygon + last ingest run) in the GeoJSON registry."""
    slug = facility_slug(facility)
    known = KNOWN_FACILITIES.get(slug, {})
    ok = [r for r in results if r.get("status") in SUCCESS_STATUSES]
    if results and len(ok) == len(results):
        ingest_status = "COMPLETE"
    elif ok:
        ingest_status = "PARTIAL"
    elif any(r.get("status") == "error" for r in results):
        ingest_status = "FAILED"
    else:
        ingest_status = "NO_DATA"
    feature = gpd.GeoDataFrame(
        {
            "id": [slug],
            "name": [known.get("full_name", facility)],
            "country": [known.get("country")],
            "latitude": [(bbox[1] + bbox[3]) / 2],
            "longitude": [(bbox[0] + bbox[2]) / 2],
            "bbox_xmin": [bbox[0]],
            "bbox_ymin": [bbox[1]],
            "bbox_xmax": [bbox[2]],
            "bbox_ymax": [bbox[3]],
            "status": ["INACTIVE"],
            "ingest_status": [ingest_status],
            "last_run_id": [run_id],
            "last_run_utc": [run_utc],
            "last_run_results": [json.dumps(results, default=str)],
        },
        geometry=[box(*bbox)],
        crs="EPSG:4326",
    )
    if registry_path.exists():
        existing = pyogrio.read_dataframe(registry_path)
        if "status" in existing and (existing["id"] == slug).any():
            feature.loc[0, "status"] = existing.loc[existing["id"] == slug, "status"].iloc[0]
        existing = existing[existing["id"] != slug]
        feature = gpd.GeoDataFrame(pd.concat([existing, feature], ignore_index=True), crs="EPSG:4326")

    tmp_path = registry_path.with_name(f".{registry_path.name}.tmp")
    pyogrio.write_dataframe(feature, tmp_path, driver="GeoJSON", layer=registry_path.stem)
    tmp_path.replace(registry_path)


async def _ingest_all(
    datasets: Sequence[str], bbox: Sequence[float], out_dir: Path, slug: str, fmt: str, settings: Settings
) -> list[dict]:
    async def one(dataset_id: str) -> dict:
        output_path = out_dir / f"{slug}_{PRODUCT_SLUGS[dataset_id]}.{fmt}"
        try:
            result = await download_and_crop(dataset_id, bbox, output_path, settings=settings)
            return result.to_dict()
        except Exception as exc:  # one failing product must not abort the others
            logger.error("%s failed: %s: %s", dataset_id, type(exc).__name__, exc)
            return {"dataset_id": dataset_id, "status": "error", "error": f"{type(exc).__name__}: {exc}"}

    return list(await asyncio.gather(*(one(d) for d in datasets)))


def cmd_ingest(args: argparse.Namespace, parser: argparse.ArgumentParser, settings: Settings) -> int:
    slug = facility_slug(args.facility)
    known = KNOWN_FACILITIES.get(slug)
    if args.bbox is None:
        if known is None:
            parser.error(f"--bbox is required for unknown facility {args.facility!r}")
        bbox = known["bbox"]
    else:
        try:
            bbox = validate_bbox(args.bbox)
        except ValueError as exc:
            parser.error(f"invalid --bbox: {exc}")

    run_utc = datetime.now(timezone.utc)
    run_id = f"{slug}_{run_utc.strftime('%Y%m%dT%H%M%SZ')}"
    log_file = settings.log_dir / f"ingest_{run_id}.log"
    handlers = configure_logging(log_file, args.log_level)
    try:
        logger.info("Run %s: facility=%s bbox=%s datasets=%s", run_id, args.facility, bbox, args.datasets)
        if known is not None and args.bbox is not None and tuple(bbox) != tuple(known["bbox"]):
            logger.warning("bbox %s differs from the registered %s bbox %s", bbox, known["name"], known["bbox"])

        out_dir = settings.processed_dir / slug
        results = asyncio.run(_ingest_all(args.datasets, bbox, out_dir, slug, args.format, settings))

        registry_path = settings.data_path / "global_tailings.geojson"
        update_tailings_registry(registry_path, args.facility, bbox, run_id, run_utc.isoformat(), results)

        for res in results:
            logger.info(
                "%-24s status=%-15s origin=%-18s output=%s",
                res["dataset_id"],
                res.get("status"),
                res.get("origin", "-"),
                res.get("output_path") or res.get("error") or "-",
            )
        logger.info("Registry updated: %s; log: %s", registry_path, log_file)
        return 1 if any(r.get("status") == "error" for r in results) else 0
    finally:
        for handler in handlers:
            logger.removeHandler(handler)
            handler.close()


def cmd_unmix(args: argparse.Namespace, settings: Settings) -> int:
    slug = facility_slug(args.facility)
    display = KNOWN_FACILITIES.get(slug, {}).get("name", args.facility)
    run_utc = datetime.now(timezone.utc)
    run_id = f"{slug}_{run_utc.strftime('%Y%m%dT%H%M%SZ')}"
    handlers = configure_logging(settings.log_dir / f"unmix_{run_id}.log", args.log_level)
    try:
        input_path = args.input
        if input_path is None:
            candidates = [settings.processed_dir / slug / f"{slug}_{PRODUCT_SLUGS['EMITL2ARFL.001']}.{ext}" for ext in ("zarr", "tif")]
            input_path = next((c for c in candidates if c.exists()), None)
            if input_path is None:
                logger.error("No EMIT reflectance for %s (looked for %s); run `ingest` first", display, [str(c) for c in candidates])
                return 1
        if not Path(input_path).exists():
            logger.error("Input %s does not exist", input_path)
            return 1

        l2bmin = args.l2bmin
        if l2bmin is None:
            sidecar = Path(f"{input_path}.meta.json")
            granule = json.loads(sidecar.read_text()).get("granule_id") if sidecar.exists() else None
            l2bmin = find_l2bmin_for(settings.raw_dir, granule)
        output = args.output or settings.cache_dir / f"{display}_spectral.zarr"
        rmse_log = settings.log_dir / f"{display}_spectral_rmse_{run_id}.json"

        logger.info("Run %s: unmixing %s -> %s (L2BMIN: %s)", run_id, input_path, output, l2bmin or "none")
        try:
            run_spectral_pipeline(
                Path(input_path), Path(output), log_path=rmse_log, l2bmin_path=l2bmin, library_path=args.library, settings=settings
            )
        except Exception as exc:
            logger.error("Unmixing failed: %s: %s", type(exc).__name__, exc)
            return 1
        logger.info("RMSE/validation log: %s", rmse_log)
        return 0
    finally:
        for handler in handlers:
            logger.removeHandler(handler)
            handler.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = get_settings()
    except (PermissionError, NotADirectoryError) as exc:
        print(f"lithocreep: configuration error: {exc}", file=sys.stderr)
        return 2
    if args.command == "ingest":
        return cmd_ingest(args, parser, settings)
    if args.command == "unmix":
        return cmd_unmix(args, settings)
    parser.error(f"unknown command {args.command!r}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
