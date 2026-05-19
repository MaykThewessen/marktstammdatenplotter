"""Build BNetzA_MaStR/full-storage.parquet from the open-mastr SQLite snapshot.

Previous pipeline (Zenodo base + MaStR JSON API delta) is superseded by the
daily-fresh open-mastr bulk dump at data/mastr/open-mastr.db. This script now
just projects the SQLite storage_extended table through the pipeline column
contract (mastr_db.load_for_pipeline) and writes it as zstd-Parquet so legacy
callers + CI artefact uploads keep working.

Run after refreshing the DB:
    pixi run db-mastr-storage  # or db-mastr-core for wind+solar+storage+market
    pixi run build-full-bess

The on-disk parquet is no longer required by mastr_plot.load_from_bulk when
source="auto"/"sqlite" — load_from_bulk reads the SQLite directly. The parquet
is kept as a portable artefact (smaller, single-file, no SQLAlchemy dep).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mastr_db  # noqa: E402

BULK = Path(__file__).resolve().parent.parent / "BNetzA_MaStR"
OUT = BULK / "full-storage.parquet"


def main() -> None:
    if not mastr_db.DB_PATH.exists():
        sys.exit(
            f"open-mastr DB not found at {mastr_db.DB_PATH}. "
            "Run `pixi run db-mastr-storage` (or db-mastr-core) first."
        )

    print(f"Loading bess from {mastr_db.DB_PATH} ...")
    df = mastr_db.load_for_pipeline("bess")

    BULK.mkdir(parents=True, exist_ok=True)
    df.to_parquet(OUT, compression="zstd", index=False)

    size_mb = OUT.stat().st_size / 1e6
    active = df[df["decommissioning_date"].isna()]
    print(f"\nOutput: {OUT}  ·  {size_mb:.1f} MB  ·  {len(df):,} rows")
    print(
        f"  active (no decommission): {len(active):,} · "
        f"{active['gross_capacity_kw'].sum() / 1e6:.2f} GW · "
        f"{active['usable_capacity_kwh'].sum() / 1e6:.2f} GWh"
    )
    print(f"  max commissioning_date:   {df['commissioning_date'].max()}")


if __name__ == "__main__":
    main()
