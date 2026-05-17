"""Re-render the multi-year wind animation GIF used as the README marquee.

Frames are one per year, with the final year repeated 9 times so the GIF holds
on the most recent snapshot for ~3 seconds before looping. Bins are computed
once on the final year so colors stay comparable across frames.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mastr_plot  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FRAMES = ROOT / "fig" / "frames"
OUT_GIF = ROOT / "fig" / "wind-2005-2025.gif"
DOCS_GIF = ROOT / "docs" / "assets" / "wind-2005-2025.gif"

START_YEAR = 2005
END_YEAR = 2025
HOLD_FRAMES = 9


def render_frames(records, units) -> int:
    FRAMES.mkdir(parents=True, exist_ok=True)
    for old in FRAMES.glob("wind-*.png"):
        old.unlink()

    agg_max, _ = mastr_plot.aggregate_by_unit(records, units, date(END_YEAR, 12, 31), "Wind")
    positive = agg_max["power_gw"][agg_max["power_gw"] > 0].to_numpy()
    bins = mastr_plot.jenks_bins(positive, k=8)

    idx = 0
    for year in range(START_YEAR, END_YEAR + 1):
        snap = date(year, 1, 1)
        agg, active = mastr_plot.aggregate_by_unit(records, units, snap, "Wind")
        fig = mastr_plot.plot_choropleth(
            agg, snap,
            f"Installed wind capacity in Germany — {snap.isoformat()}\n"
            f"{len(active):,} turbines · {round(agg['power_gw'].sum(), 1)} GW",
            bins=bins, cmap="GnBu",
        )
        fig.savefig(FRAMES / f"wind-{idx:03d}.png", dpi=110, bbox_inches="tight")
        plt.close(fig)
        idx += 1

    last = FRAMES / f"wind-{idx - 1:03d}.png"
    for _ in range(HOLD_FRAMES):
        shutil.copy(last, FRAMES / f"wind-{idx:03d}.png")
        idx += 1
    return idx


def assemble_gif():
    cmd = [
        "ffmpeg", "-y", "-framerate", "3",
        "-i", str(FRAMES / "wind-%03d.png"),
        "-vf", "scale=-1:900:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer",
        "-loop", "0", str(OUT_GIF),
    ]
    subprocess.run(cmd, check=True)
    shutil.copy(OUT_GIF, DOCS_GIF)


def main():
    records, demo = mastr_plot.load_records()
    if demo:
        print("WARNING: rendering wind GIF from demo data.")
    units, _ = mastr_plot.load_admin_units()
    n = render_frames(records, units)
    print(f"Rendered {n} PNG frames.")
    assemble_gif()
    print(f"GIF written to {OUT_GIF.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
