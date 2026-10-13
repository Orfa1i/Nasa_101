"""Rebuild the bundled spectral reference files in ``data/reference/``.

Sources (all public):

* EMIT wavelength calibration: ``emit-sds/emit-sds-l1b``
  ``data/emit/EMIT_Wavelengths_20220817.txt`` (Apache-2.0). Detector rows
  20..304 (``first/last_distributed_row`` in ``config/EMIT_20220818.json``)
  are the 285 distributed L1B/L2A bands, stored in reverse order.
* USGS spectral library splib06a ASCII files (public domain; Clark et al. 2007,
  USGS Data Series 231), as shipped in ``emit-sds/spectroscopy-tetracorder``
  under ``sl1/usgs/library06/ASCII``.
* EMIT L2B mineral grouping matrix: ``emit-sds/emit-sds-l2b``
  ``data/mineral_grouping_matrix_20230503.csv`` (Apache-2.0); maps the
  ``group_*_mineral_id`` values of EMITL2BMIN files to library entries.

Usage::

    python backend/scripts/build_reference_data.py \
        --emit-l1b /path/to/emit-sds-l1b \
        --emit-l2b /path/to/emit-sds-l2b \
        --splib06-ascii /path/to/library06/ASCII \
        --out data/reference
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pandas as pd

EMIT_WAVELENGTH_FILE = "data/emit/EMIT_Wavelengths_20220817.txt"
EMIT_FIRST_DISTRIBUTED_ROW, EMIT_LAST_DISTRIBUTED_ROW = 20, 304
GROUPING_MATRIX_FILE = "data/mineral_grouping_matrix_20230503.csv"
DELETED = -1.0e30  # splib "deleted point" marker is -1.23e34

# Endmember -> list of (splib06a ASCII file, weight). Beckman (W1R1B*) spectra
# cover 0.2-3.0 um, i.e. all EMIT channels; purity grade "a" chosen where one exists.
ENDMEMBER_SOURCES = {
    "Jarosite": [("M/jarosite_gds99.11469.asc", 1.0)],  # K-jarosite GDS99, purity a
    "Goethite": [("M/goethite_ws222.8447.asc", 1.0)],  # medium grain, purity a
    "Ferrihydrite": [("M/ferrihydrite_gds75.7822.asc", 1.0)],  # only splib06a ferrihydrite
    "Montmorillonite": [("M/montmorillonite_swy1.14688.asc", 1.0)],  # SWy-1 reference smectite
    "Gypsum": [("M/gypsum_su2202.8850.asc", 1.0)],  # purity a
    # No quartz+feldspar spectrum exists in splib06a: 50/50 linear (areal) mix.
    "Quartz-Feldspar": [("M/quartz_gds31.19064.asc", 0.5), ("M/orthoclase_hs13.17283.asc", 0.5)],
    "Water-Shade": [("L/seawater_open_ocean_sw2.27262.asc", 1.0)],  # dark water / shade
}


def git_commit(repo: Path) -> str:
    try:
        return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def read_splib_ascii(path: Path) -> tuple[str, np.ndarray, np.ndarray]:
    """Return (title, wavelength_nm, reflectance) of a splib06a ASCII spectrum."""
    lines = path.read_text(errors="replace").splitlines()
    title = lines[14].strip()
    data = np.array([[float(v) for v in line.split()[:2]] for line in lines[16:] if line.strip()])
    keep = (data[:, 1] > DELETED) & (data[:, 0] > 0)
    return title, data[keep, 0] * 1000.0, data[keep, 1]


def build_emit_wavelengths(l1b: Path) -> pd.DataFrame:
    table = np.loadtxt(l1b / EMIT_WAVELENGTH_FILE)
    rows = np.arange(EMIT_LAST_DISTRIBUTED_ROW, EMIT_FIRST_DISTRIBUTED_ROW - 1, -1)
    return pd.DataFrame(
        {
            "band": np.arange(1, rows.size + 1),
            "wavelength_nm": np.round(table[rows, 1] * 1000.0, 4),
            "fwhm_nm": np.round(table[rows, 2] * 1000.0, 4),
        }
    )


def build_usgs_extract(ascii_dir: Path) -> pd.DataFrame:
    frames = []
    for endmember, parts in ENDMEMBER_SOURCES.items():
        spectra = [(read_splib_ascii(ascii_dir / rel), rel, weight) for rel, weight in parts]
        # Components share the Beckman W1 wavelength set but may have different deleted
        # points; mix only where every component has a measured value.
        base_wl = spectra[0][0][1]
        for (_, wl, _), _, _ in spectra[1:]:
            base_wl = np.intersect1d(base_wl, wl)
        refl = sum(weight * spec[2][np.isin(spec[1], base_wl)] for spec, _, weight in spectra)
        if len(spectra) == 1:
            title, source = spectra[0][0][0], parts[0][0]
        else:
            title = "derived: " + " + ".join(f"{w:g}*[{s[0]}]" for s, _, w in spectra)
            source = ";".join(rel for rel, _ in parts)
        frames.append(
            pd.DataFrame(
                {
                    "endmember": endmember,
                    "usgs_title": title,
                    "source_file": source,
                    "library": "splib06a",
                    "wavelength_nm": np.round(base_wl, 3),
                    "reflectance": np.round(refl, 6),
                }
            )
        )
    return pd.concat(frames, ignore_index=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--emit-l1b", type=Path, required=True)
    parser.add_argument("--emit-l2b", type=Path, required=True)
    parser.add_argument("--splib06-ascii", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[2] / "data" / "reference")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    build_emit_wavelengths(args.emit_l1b).to_csv(args.out / "emit_wavelengths_285.csv", index=False)
    build_usgs_extract(args.splib06_ascii).to_csv(args.out / "usgs_splib06a_extract.csv", index=False)
    shutil.copyfile(args.emit_l2b / GROUPING_MATRIX_FILE, args.out / "emit_l2b_mineral_grouping_matrix_20230503.csv")
    print(f"emit-sds-l1b commit: {git_commit(args.emit_l1b)}")
    print(f"emit-sds-l2b commit: {git_commit(args.emit_l2b)}")
    print(f"wrote reference files to {args.out}")


if __name__ == "__main__":
    main()
