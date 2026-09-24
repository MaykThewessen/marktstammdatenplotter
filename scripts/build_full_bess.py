"""Build BNetzA_MaStR/full-storage.parquet from the MaStR parquet store.

Previous pipeline (Zenodo base + MaStR JSON API delta) is superseded by the
daily-fresh open-mastr bulk export. This script projects the store's
EinheitenStromSpeicher table through the pipeline column contract
(mastr_parquet.load_for_pipeline) and writes it as one zstd parquet file so
legacy callers keep working.

Run after refreshing the store:
    pixi run db-mastr-storage  # or db-mastr-core for wind+solar+storage+market
    pixi run build-full-bess

mastr_plot.load_from_bulk does not need this file: it reads the store directly.
It is kept as a portable single-file artefact.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mastr_db  # noqa: E402
import mastr_parquet  # noqa: E402

BULK = Path(__file__).resolve().parent.parent / "BNetzA_MaStR"
OUT = BULK / "full-storage.parquet"


def main() -> None:
    storage_table = mastr_db.EXTENDED_TABLES["storage"]
    if storage_table not in mastr_parquet.available_tables():
        sys.exit(
            f"{storage_table}.parquet not found in {mastr_parquet.PARQUET_DIR}. "
            "Run `pixi run db-mastr-storage` (or db-mastr-core) first."
        )

    print(f"Loading bess from {mastr_parquet.PARQUET_DIR} ...")
    df = mastr_parquet.load_for_pipeline("bess")

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
