"""LithoCreep command-line interface.

Usage (from the repository root or from ``backend/``)::

    python backend/app/main.py ingest --facility Sierrita --bbox -111.12 31.83 -111.04 31.90
    python backend/app/main.py unmix --facility Sierrita
    python backend/app/main.py kinematics --facility Sierrita [--download]
    python backend/app/main.py hydrology --facility Sierrita [--download] [--crest crest.geojson]
    python backend/app/main.py couple --facility Sierrita

``ingest`` runs ``download_and_crop`` for each requested product, writes the
clipped rasters to ``DATA_DIR/processed/<facility>/``, records the run on the
facility's feature in ``DATA_DIR/global_tailings.geojson`` and logs to
``DATA_DIR/logs/ingest_<run_id>.log``.

``unmix`` runs the spectroscopy engine on the facility's EMIT L2A output from
``ingest`` and writes ``DATA_DIR/cache/<Facility>_spectral.zarr`` plus an
RMSE/validation JSON log in ``DATA_DIR/logs/``.

``kinematics`` analyses the DISP-S1 stack in ``DATA_DIR/raw/OPERA_L3_DISP-S1_V1``
(or, outside CONUS, ``DATA_DIR/<facility>_historic.csv``) and writes
``DATA_DIR/cache/<Facility>_kinematics.zarr``.

``hydrology`` builds GLO-30 terrain/D-infinity routing products and, with
DSWx-HLS granules and a crest line, the pond-to-crest setback series and alerts,
writing ``DATA_DIR/cache/<Facility>_hydrology.zarr``.

``uvicorn app.main:app`` serves the HTTP API (``app/api/endpoints.py``).

``couple`` aligns the spectral, kinematics and hydrology outputs on one 30 m UTM
grid, tests pond -> moving slope -> seepage connectivity and assigns the
4-regime hazard classes, writing ``DATA_DIR/cache/<Facility>_coupled.zarr``.
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
import numpy as np
import pandas as pd
import pyogrio
from shapely.geometry import box

from app.config import Settings, get_settings
from app.pipelines.ingestion import PRODUCTS, download_and_crop, validate_bbox
from app.pipelines.coupling import run_coupling
from app.pipelines.hydrology import STREAM_THRESHOLD_CELLS, glo30_tile_names, run_hydrology
from app.pipelines.kinematics import run_kinematics
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
    "bingham": {
        "name": "Bingham",
        "full_name": "Kennecott Bingham Canyon",
        "country": "USA",
        # Approximate box around the Bingham Canyon pit (~40.52 N, 112.15 W); not survey-verified.
        "bbox": (-112.19, 40.49, -112.11, 40.56),
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

    kin = sub.add_parser("kinematics", help="GP velocity/acceleration and Fukuzono trend timelines from InSAR")
    kin.add_argument("--facility", required=True, type=_facility_name, help="Facility name, e.g. Sierrita")
    kin.add_argument("--bbox", nargs=4, type=float, metavar=("XMIN", "YMIN", "XMAX", "YMAX"), help="optional for known facilities")
    kin.add_argument("--stack-dir", type=Path, help="Folder of DISP-S1 .nc granules; default DATA_DIR/raw/OPERA_L3_DISP-S1_V1")
    kin.add_argument("--historic-csv", type=Path, help="Point history CSV for non-CONUS sites; default DATA_DIR/<facility>_historic.csv")
    kin.add_argument("--download", action="store_true", help="Download the DISP-S1 stack from Earthdata first (CONUS only)")
    kin.add_argument("--hyperparameters", choices=("auto", "per_pixel", "hybrid"), default="auto", help="GP fitting: per series, or hybrid (shared for quiet ground); auto picks by size")
    kin.add_argument("--jobs", type=int, default=-1, help="Parallel GP fits (joblib; -1 = all cores)")
    kin.add_argument("--output", type=Path, help="Output Zarr; default DATA_DIR/cache/<Facility>_kinematics.zarr")
    kin.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))

    hyd = sub.add_parser("hydrology", help="DSWx pond setback alerts and GLO-30 D-infinity flow routing")
    hyd.add_argument("--facility", required=True, type=_facility_name, help="Facility name, e.g. Sierrita")
    hyd.add_argument("--bbox", nargs=4, type=float, metavar=("XMIN", "YMIN", "XMAX", "YMAX"), help="optional for known facilities")
    hyd.add_argument("--download", action="store_true", help="Download GLO-30 tiles (HTTPS) and the DSWx-HLS series (Earthdata) first")
    hyd.add_argument("--max-cloud", type=float, default=None, help="With --download: skip DSWx granules above this cloud cover (%%)")
    hyd.add_argument("--dem", type=Path, nargs="+", help="DEM GeoTIFF(s); default GLO-30 tiles in DATA_DIR/raw/GLO30")
    hyd.add_argument("--dswx-dir", type=Path, help="Folder of DSWx-HLS *_B01_WTR.tif; default DATA_DIR/raw/OPERA_L3_DSWX-HLS_V1")
    hyd.add_argument("--crest", type=Path, help="Crest LineString vector file; default DATA_DIR/reference/<facility>_crest.geojson")
    hyd.add_argument("--boundary", type=Path, help="Facility Polygon vector file; default DATA_DIR/reference/<facility>_boundary.geojson, else the bbox")
    hyd.add_argument("--ndvi", type=Path, help="NDVI source (.zarr with 'ndvi' or raster); default DATA_DIR/cache/<Facility>_spectral.zarr")
    hyd.add_argument("--stream-threshold", type=int, default=STREAM_THRESHOLD_CELLS, help="Cells of accumulation that define a stream")
    hyd.add_argument("--output", type=Path, help="Output Zarr; default DATA_DIR/cache/<Facility>_hydrology.zarr")
    hyd.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))

    cpl = sub.add_parser("couple", help="Co-anomaly coupling and 4-regime hazard classification")
    cpl.add_argument("--facility", required=True, type=_facility_name, help="Facility name, e.g. Sierrita")
    cpl.add_argument("--spectral", type=Path, help="default DATA_DIR/cache/<Facility>_spectral.zarr")
    cpl.add_argument("--kinematics", type=Path, help="default DATA_DIR/cache/<Facility>_kinematics.zarr")
    cpl.add_argument("--hydrology", type=Path, help="default DATA_DIR/cache/<Facility>_hydrology.zarr")
    cpl.add_argument("--earthworks", type=Path, help="Polygon vector file of active earthworks (REGIME_2)")
    cpl.add_argument("--output", type=Path, help="default DATA_DIR/cache/<Facility>_coupled.zarr")
    cpl.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
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
        mine = existing["id"] == slug
        if mine.any():
            # Keep curated metadata (name, mine/embankment type, crest line, status, ...): only the
            # bbox-derived fields and the ingest-run fields are refreshed.
            previous = existing.loc[mine].iloc[0]
            refreshed = {"latitude", "longitude", "bbox_xmin", "bbox_ymin", "bbox_xmax", "bbox_ymax",
                         "ingest_status", "last_run_id", "last_run_utc", "last_run_results", "geometry", "id"}
            for column, value in previous.items():
                if column in refreshed:
                    continue
                if column in feature.columns and column not in ("name", "country", "status"):
                    continue
                if value is not None and not (isinstance(value, float) and np.isnan(value)):
                    feature[column] = [value]
        existing = existing[~mine]
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


def cmd_kinematics(args: argparse.Namespace, parser: argparse.ArgumentParser, settings: Settings) -> int:
    from app.pipelines import ingestion

    slug = facility_slug(args.facility)
    known = KNOWN_FACILITIES.get(slug)
    display = known["name"] if known else args.facility
    if args.bbox is None:
        if known is None:
            parser.error(f"--bbox is required for unknown facility {args.facility!r}")
        bbox = known["bbox"]
    else:
        try:
            bbox = validate_bbox(args.bbox)
        except ValueError as exc:
            parser.error(f"invalid --bbox: {exc}")

    run_id = f"{slug}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    handlers = configure_logging(settings.log_dir / f"kinematics_{run_id}.log", args.log_level)
    try:
        in_conus = ingestion.bbox_within_conus(bbox)
        if args.download and in_conus:
            try:
                ingestion.download_disp_stack(bbox, settings)
            except ingestion.OFFLINE_ERRORS as exc:
                logger.warning("Earthdata unreachable (%s); using DISP-S1 granules already on disk", type(exc).__name__)
        elif args.download:
            logger.warning("%s is outside CONUS; DISP-S1 download skipped", display)

        stack = None
        if args.stack_dir is not None:
            stack = sorted(Path(args.stack_dir).glob("OPERA_L3_DISP-S1_*.nc"))
        output = args.output or settings.cache_dir / f"{display}_kinematics.zarr"
        try:
            summary = run_kinematics(
                display, bbox, output, settings=settings, stack_paths=stack,
                historic_csv=args.historic_csv, hyperparameters=args.hyperparameters, n_jobs=args.jobs,
            )
        except Exception as exc:
            logger.error("Kinematics failed: %s: %s", type(exc).__name__, exc)
            return 1
        summary_path = settings.log_dir / f"{display}_kinematics_{run_id}.json"
        summary_path.write_text(json.dumps(summary, indent=2, default=str))
        logger.info("Summary: %s", summary_path)
        logger.warning(summary["disclaimer"])
        return 0
    finally:
        for handler in handlers:
            logger.removeHandler(handler)
            handler.close()


def _resolve_bbox(args: argparse.Namespace, parser: argparse.ArgumentParser) -> tuple[str, tuple[float, ...]]:
    slug = facility_slug(args.facility)
    known = KNOWN_FACILITIES.get(slug)
    if args.bbox is None:
        if known is None:
            parser.error(f"--bbox is required for unknown facility {args.facility!r}")
        return known["name"], tuple(known["bbox"])
    try:
        return (known["name"] if known else args.facility), validate_bbox(args.bbox)
    except ValueError as exc:
        parser.error(f"invalid --bbox: {exc}")
        raise


def cmd_hydrology(args: argparse.Namespace, parser: argparse.ArgumentParser, settings: Settings) -> int:
    from app.pipelines import hydrology, ingestion

    display, bbox = _resolve_bbox(args, parser)
    slug = facility_slug(args.facility)
    run_id = f"{slug}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    handlers = configure_logging(settings.log_dir / f"hydrology_{run_id}.log", args.log_level)
    try:
        dem_dir = settings.raw_dir / "GLO30"
        dswx_dir = args.dswx_dir or settings.raw_dir / ingestion.PRODUCTS[hydrology.DSWX_DATASET_ID].short_name
        if args.download:
            try:
                hydrology.download_glo30(bbox, dem_dir)
            except Exception as exc:
                logger.warning("GLO-30 download failed (%s: %s); using tiles already on disk", type(exc).__name__, exc)
            try:
                ingestion.download_granule_series(hydrology.DSWX_DATASET_ID, bbox, settings, max_cloud_cover=args.max_cloud)
            except ingestion.OFFLINE_ERRORS as exc:
                logger.warning("Earthdata unreachable (%s); using DSWx-HLS granules already on disk", type(exc).__name__)

        dem_paths = args.dem or [p for p in (dem_dir / f"{n}.tif" for n in glo30_tile_names(bbox)) if p.exists()]
        if not dem_paths:
            logger.error("No DEM for %s: pass --dem or run with --download (GLO-30 tiles go to %s)", display, dem_dir)
            return 1
        dswx_paths = sorted(Path(dswx_dir).glob("*_B01_WTR.tif"))
        reference = settings.data_path / "reference"
        crest = args.crest or next((p for p in [reference / f"{slug}_crest.geojson"] if p.exists()), None)
        boundary = args.boundary or next((p for p in [reference / f"{slug}_boundary.geojson"] if p.exists()), None)
        ndvi = args.ndvi or settings.cache_dir / f"{display}_spectral.zarr"
        output = args.output or settings.cache_dir / f"{display}_hydrology.zarr"
        try:
            summary = run_hydrology(
                display, bbox, output, settings=settings, dem_paths=dem_paths, dswx_paths=dswx_paths,
                crest_path=crest, boundary_path=boundary, ndvi_source=ndvi, stream_threshold=args.stream_threshold,
            )
        except Exception as exc:
            logger.error("Hydrology failed: %s: %s", type(exc).__name__, exc)
            return 1
        summary_path = settings.log_dir / f"{display}_hydrology_{run_id}.json"
        summary_path.write_text(json.dumps(summary, indent=2, default=str))
        for alert in summary["alerts"]:
            logger.warning("ALERT %s at %s (D_crest = %.1f m)", alert["alert"], alert["time"], alert["d_crest_m"])
        logger.info("Summary: %s", summary_path)
        return 0
    finally:
        for handler in handlers:
            logger.removeHandler(handler)
            handler.close()


def cmd_couple(args: argparse.Namespace, settings: Settings) -> int:
    slug = facility_slug(args.facility)
    display = KNOWN_FACILITIES.get(slug, {}).get("name", args.facility)
    run_id = f"{slug}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    handlers = configure_logging(settings.log_dir / f"couple_{run_id}.log", args.log_level)
    try:
        inputs = {
            "spectral": (args.spectral or settings.cache_dir / f"{display}_spectral.zarr", "unmix"),
            "kinematics": (args.kinematics or settings.cache_dir / f"{display}_kinematics.zarr", "kinematics"),
            "hydrology": (args.hydrology or settings.cache_dir / f"{display}_hydrology.zarr", "hydrology"),
        }
        missing = [f"{k} ({p}; run `{cmd}` first)" for k, (p, cmd) in inputs.items() if not Path(p).exists()]
        if missing:
            logger.error("Missing coupling inputs: %s", "; ".join(missing))
            return 1
        output = args.output or settings.cache_dir / f"{display}_coupled.zarr"
        try:
            summary = run_coupling(
                display, inputs["spectral"][0], inputs["kinematics"][0], inputs["hydrology"][0], output,
                earthworks_path=args.earthworks,
            )
        except Exception as exc:
            logger.error("Coupling failed: %s: %s", type(exc).__name__, exc)
            return 1
        summary_path = settings.log_dir / f"{display}_coupled_{run_id}.json"
        summary_path.write_text(json.dumps(summary, indent=2, default=str))
        level = logging.WARNING if summary["facility_hazard_level"] != "NONE" else logging.INFO
        logger.log(level, "Facility hazard level: %s; regimes %s", summary["facility_hazard_level"], summary["regime_counts"])
        if summary["contains_synthetic_or_mock_inputs"]:
            logger.warning("Inputs include synthetic/mock data: %s", summary["input_provenance"])
        logger.info("Summary: %s", summary_path)
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
    if args.command == "kinematics":
        return cmd_kinematics(args, parser, settings)
    if args.command == "hydrology":
        return cmd_hydrology(args, parser, settings)
    if args.command == "couple":
        return cmd_couple(args, settings)
    parser.error(f"unknown command {args.command!r}")
    return 2


# ASGI application for `uvicorn app.main:app` (the database is built on start-up, not on import).
from app.api.endpoints import create_app  # noqa: E402

app = create_app()


if __name__ == "__main__":
    sys.exit(main())
