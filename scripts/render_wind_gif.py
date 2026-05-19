"""Re-render the multi-year wind animation GIF used as the README marquee.

Frames are one per year, with the final year repeated 9 times so the GIF holds
on the most recent snapshot for ~3 seconds before looping. Bins are computed
once on the final year so colors stay comparable across frames.
"""

from __future__ import annotations

import argparse
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

PRESETS = {
    "wind": {
        "energy_type": "Wind",
        "frame_prefix": "wind",
        "frames_dir": "fig/frames",
        "gif_name": "wind-2005-may2026.gif",
        "cmap": "GnBu",
        "noun": "turbines",
        "title": "Installed wind capacity in Germany",
        "extra_subtitle": "",
    },
    "pv": {
        "energy_type": "Solare Strahlungsenergie",
        "frame_prefix": "pv",
        "frames_dir": "fig/frames-pv",
        "gif_name": "pv-2005-may2026.gif",
        "cmap": "YlOrRd",
        "noun": "plants",
        "title": "Installed PV capacity in Germany",
        "extra_subtitle": " ≥ 49 kW (top 200k by capacity)",
    },
}

START_YEAR = 2005
END_YEAR = 2025          # last full year-start frame
FINAL_FRAME = date(2026, 5, 1)   # extra frame past END_YEAR for YTD signal
HOLD_FRAMES = 9


def snapshot_dates() -> list[date]:
    """Yearly Jan-1 frames from START_YEAR through END_YEAR, plus FINAL_FRAME."""
    dates = [date(y, 1, 1) for y in range(START_YEAR, END_YEAR + 1)]
    if FINAL_FRAME and FINAL_FRAME > dates[-1]:
        dates.append(FINAL_FRAME)
    return dates


def render_frames(records, units, cfg) -> tuple[int, Path]:
    frames = ROOT / cfg["frames_dir"]
    frames.mkdir(parents=True, exist_ok=True)
    for old in frames.glob(f"{cfg['frame_prefix']}-*.png"):
        old.unlink()

    dates = snapshot_dates()
    agg_max, _ = mastr_plot.aggregate_by_unit(
        records, units, dates[-1], cfg["energy_type"]
    )
    positive = agg_max["power_gw"][agg_max["power_gw"] > 0].to_numpy()
    bins = mastr_plot.jenks_bins(positive, k=8)

    idx = 0
    for snap in dates:
        agg, active = mastr_plot.aggregate_by_unit(records, units, snap, cfg["energy_type"])
        total_gw = active["power"].sum() / 1e6
        title = (
            f"{cfg['title']} — {snap.isoformat()}\n"
            f"{len(active):,} {cfg['noun']}{cfg['extra_subtitle']} · "
            f"{round(total_gw, 1)} GW"
        )
        fig = mastr_plot.plot_choropleth(
            agg, snap, title, bins=bins, cmap=cfg["cmap"],
        )
        fig.savefig(frames / f"{cfg['frame_prefix']}-{idx:03d}.png",
                    dpi=150, bbox_inches="tight")
        plt.close(fig)
        idx += 1

    last = frames / f"{cfg['frame_prefix']}-{idx - 1:03d}.png"
    for _ in range(HOLD_FRAMES):
        shutil.copy(last, frames / f"{cfg['frame_prefix']}-{idx:03d}.png")
        idx += 1
    return idx, frames


def assemble_gif(frames: Path, cfg):
    out_gif = ROOT / "fig" / cfg["gif_name"]
    docs_gif = ROOT / "docs" / "assets" / cfg["gif_name"]
    cmd = [
        "ffmpeg", "-y", "-framerate", "3",
        "-i", str(frames / f"{cfg['frame_prefix']}-%03d.png"),
        "-vf", "scale=-1:1200:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=192[p];[s1][p]paletteuse=dither=bayer",
        "-loop", "0", str(out_gif),
    ]
    subprocess.run(cmd, check=True)
    shutil.copy(out_gif, docs_gif)
    return out_gif


def run_tech(tech: str):
    cfg = PRESETS[tech]
    records, demo = mastr_plot.load_records()
    if demo:
        print(f"WARNING: rendering {tech} GIF from demo data.")
    units, _ = mastr_plot.load_admin_units()
    n, frames = render_frames(records, units, cfg)
    print(f"Rendered {n} PNG frames for {tech}.")
    out = assemble_gif(frames, cfg)
    print(f"GIF written to {out.relative_to(ROOT)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "tech", nargs="?", default="wind", choices=["wind", "pv", "both"],
        help="Which animation to build (default: wind)",
    )
    args = parser.parse_args()
    techs = ["wind", "pv"] if args.tech == "both" else [args.tech]
    for t in techs:
        run_tech(t)


if __name__ == "__main__":
    main()
