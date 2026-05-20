"""Re-render the multi-year wind animation GIF used as the README marquee.

Frames are one per year, with the final year repeated 9 times so the GIF holds
on the most recent snapshot for ~3 seconds before looping. Bins are computed
once on the final year so colors stay comparable across frames.
"""

from __future__ import annotations

import argparse
import os
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
        "kind": "generation",
        "energy_type": "Wind",
        "frame_prefix": "wind",
        "frames_dir": "fig/frames",
        "basename": "wind-2005-may2026",
        "cmap": "GnBu",
        "noun": "turbines",
        "title": "Installed wind capacity in Germany",
        "extra_subtitle": "",
    },
    "pv": {
        "kind": "generation",
        "energy_type": "Solare Strahlungsenergie",
        "frame_prefix": "pv",
        "frames_dir": "fig/frames-pv",
        "basename": "pv-2010-may2026",
        "cmap": "YlOrRd",
        "noun": "plants",
        "title": "Installed PV capacity in Germany",
        "extra_subtitle": "",
        "agg_unit": "GWp",
        "legend_label": "Capacity [GWp]",
        "start_year": 2010,
    },
    "bess": {
        "kind": "bess",
        "frame_prefix": "bess",
        "frames_dir": "fig/frames-bess",
        "basename": "bess-2020-may2026",
        "cmap": "BuPu",
        "noun": "units",
        "title": "Installed battery storage energy in Germany",
        "extra_subtitle": "",
        "agg_col": "energy_gwh",
        "agg_unit": "GWh",
        "legend_label": "Energy [GWh]",
        "start_year": 2020,
    },
}

START_YEAR = 2005
END_YEAR = 2025          # last full year-start frame
# Closing YTD frame tracks the current calendar day so re-renders against a
# freshly refreshed open-mastr SQLite always end on real data, not a stale
# hard-coded date. Override with the env var MASTR_FINAL_FRAME=YYYY-MM-DD
# for reproducible renders.
FINAL_FRAME = (
    date.fromisoformat(os.environ["MASTR_FINAL_FRAME"])
    if os.environ.get("MASTR_FINAL_FRAME") else date.today()
)
HOLD_FRAMES = 9

# Cadence-aware framerate so all variants run ~8-12s end-to-end. Override
# at the CLI with --cadence yearly|halfyear|quarterly|monthly.
CADENCE_FRAMERATE = {
    "yearly":    "3",
    "halfyear":  "5",
    "quarterly": "8",
    "monthly":   "18",
}
DEFAULT_CADENCE = "yearly"


def snapshot_dates(
    start_year: int = START_YEAR, cadence: str = DEFAULT_CADENCE,
) -> list[date]:
    """Snapshot dates from start_year through FINAL_FRAME.

    `cadence` controls inter-frame spacing:
      yearly    — Jan-1 (~22 frames over 2005-2026)
      halfyear  — Jan-1 + Jul-1 (~43 frames)
      quarterly — Jan/Apr/Jul/Oct-1 (~85 frames)
      monthly   — 1st of every month (~256 frames)

    Walks from start_year/Jan up to FINAL_FRAME in `step` months; ensures
    FINAL_FRAME itself is the closing frame regardless of step alignment
    (so cadence='monthly' includes Jan/Feb/Mar/Apr 2026 + May 2026 YTD
    instead of jumping from Dec 2025 straight to May 2026).
    """
    months_step = {
        "yearly":     12,
        "halfyear":   6,
        "quarterly":  3,
        "monthly":    1,
    }
    if cadence not in months_step:
        raise ValueError(f"unknown cadence {cadence!r}; expected one of {list(months_step)}")
    step = months_step[cadence]
    dates: list[date] = []
    y, m = start_year, 1
    while date(y, m, 1) < FINAL_FRAME:
        dates.append(date(y, m, 1))
        m += step
        while m > 12:
            m -= 12
            y += 1
    if not dates or dates[-1] != FINAL_FRAME:
        dates.append(FINAL_FRAME)
    return dates


FRAME_EXT = "webp"
TARGET_HEIGHT = 1440
FFMPEG_FRAMERATE = "3"  # overridden per-cadence at render time


def _aggregate(records, units, snap, cfg):
    """Dispatch on cfg['kind'] — generation tech or BESS."""
    if cfg["kind"] == "bess":
        return mastr_plot.aggregate_bess_by_unit(records, units, snap)
    return mastr_plot.aggregate_by_unit(records, units, snap, cfg["energy_type"])


def _active_total(active, cfg):
    if cfg["kind"] == "bess":
        src_col = "energy_kwh" if cfg.get("agg_col") == "energy_gwh" else "power_kw"
    else:
        src_col = "power"
    return active[src_col].sum() / 1e6 if len(active) else 0.0


def _noun_count(active, cfg):
    return f"{len(active):,} {cfg['noun']}{cfg['extra_subtitle']}"


def render_frames(records, units, cfg, cadence: str = DEFAULT_CADENCE) -> tuple[int, Path]:
    frames = ROOT / cfg["frames_dir"]
    frames.mkdir(parents=True, exist_ok=True)
    for old in frames.iterdir():
        if old.is_file():
            old.unlink()

    agg_col = cfg.get("agg_col", "power_gw")
    agg_unit = cfg.get("agg_unit", "GW")
    legend_label = cfg.get("legend_label", "Capacity [GW]")

    dates = snapshot_dates(cfg.get("start_year", START_YEAR), cadence=cadence)
    agg_max, _ = _aggregate(records, units, dates[-1], cfg)
    positive = agg_max[agg_col][agg_max[agg_col] > 0].to_numpy()
    bins = mastr_plot.jenks_bins(positive, k=8)

    idx = 0
    for snap in dates:
        agg, active = _aggregate(records, units, snap, cfg)
        total = _active_total(active, cfg)
        title = (
            f"{cfg['title']} — {snap.isoformat()}\n"
            f"{_noun_count(active, cfg)} · {round(total, 1)} {agg_unit}"
        )
        fig = mastr_plot.plot_choropleth(
            agg, snap, title, bins=bins, cmap=cfg["cmap"],
            col=agg_col, legend_label=legend_label,
        )
        # Lossless WebP via Pillow under the hood. Smaller than PNG at the
        # same dpi and natively read by ffmpeg's webp decoder.
        fig.savefig(
            frames / f"{cfg['frame_prefix']}-{idx:03d}.{FRAME_EXT}",
            dpi=180, bbox_inches="tight",
            pil_kwargs={"lossless": True, "method": 4},
        )
        plt.close(fig)
        idx += 1

    last = frames / f"{cfg['frame_prefix']}-{idx - 1:03d}.{FRAME_EXT}"
    for _ in range(HOLD_FRAMES):
        shutil.copy(last, frames / f"{cfg['frame_prefix']}-{idx:03d}.{FRAME_EXT}")
        idx += 1
    return idx, frames


def _assemble(frames: Path, cfg, ext: str, vcodec_args: list[str],
              cadence: str = DEFAULT_CADENCE):
    """Run ffmpeg over the frame sequence to produce `<basename>.<ext>`."""
    out_path = ROOT / "fig" / f"{cfg['basename']}.{ext}"
    docs_path = ROOT / "docs" / "assets" / f"{cfg['basename']}.{ext}"
    framerate = CADENCE_FRAMERATE.get(cadence, FFMPEG_FRAMERATE)
    cmd = [
        "ffmpeg", "-y", "-framerate", framerate,
        "-i", str(frames / f"{cfg['frame_prefix']}-%03d.{FRAME_EXT}"),
        *vcodec_args,
        str(out_path),
    ]
    subprocess.run(cmd, check=True)
    shutil.copy(out_path, docs_path)
    return out_path


def assemble_gif(frames: Path, cfg, cadence: str = DEFAULT_CADENCE):
    return _assemble(
        frames, cfg, "gif",
        [
            "-vf",
            f"scale=-1:{TARGET_HEIGHT}:flags=lanczos,"
            "split[s0][s1];[s0]palettegen=max_colors=224[p];"
            "[s1][p]paletteuse=dither=bayer",
            "-loop", "0",
        ],
        cadence=cadence,
    )


def assemble_mp4(frames: Path, cfg, cadence: str = DEFAULT_CADENCE):
    """H.264 MP4 — LinkedIn-native, ~30-50% smaller than the GIF.

    yuv420p + even-dimensions for maximum player compatibility.
    """
    # MP4 fps filter normalises to a fixed display rate independent of
    # cadence; bump it for high-cadence renders so the animation runs in a
    # similar wall-clock window.
    mp4_display_fps = {
        "yearly":    10,
        "halfyear":  12,
        "quarterly": 18,
        "monthly":   24,
    }.get(cadence, 10)
    return _assemble(
        frames, cfg, "mp4",
        [
            "-vf",
            f"scale=-2:{TARGET_HEIGHT}:flags=lanczos,fps={mp4_display_fps},format=yuv420p",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "slow",
            "-crf", "18",
            "-movflags", "+faststart",
        ],
        cadence=cadence,
    )


def run_tech(tech: str, cadence: str = DEFAULT_CADENCE):
    cfg = PRESETS[tech]
    if cfg["kind"] == "bess":
        records, demo = mastr_plot.load_bess()
        records = mastr_plot.split_bess_storage(records)["batteries"]
    else:
        records, demo = mastr_plot.load_records()
    if demo:
        print(f"WARNING: rendering {tech} animation from demo data.")
    units, _ = mastr_plot.load_admin_units()
    n, frames = render_frames(records, units, cfg, cadence=cadence)
    print(f"Rendered {n} WebP frames for {tech} (cadence={cadence}).")
    gif = assemble_gif(frames, cfg, cadence=cadence)
    print(f"GIF written to {gif.relative_to(ROOT)} ({gif.stat().st_size / 1024:.0f} KiB)")
    mp4 = assemble_mp4(frames, cfg, cadence=cadence)
    print(f"MP4 written to {mp4.relative_to(ROOT)} ({mp4.stat().st_size / 1024:.0f} KiB)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "tech", nargs="?", default="wind",
        choices=["wind", "pv", "bess", "both", "all"],
        help="Which animation to build (default: wind). 'both' = wind+pv, 'all' = wind+pv+bess.",
    )
    parser.add_argument(
        "--cadence", default=DEFAULT_CADENCE,
        choices=list(CADENCE_FRAMERATE),
        help=(
            "Inter-frame spacing for the snapshot loop "
            f"(default: {DEFAULT_CADENCE}). 'halfyear' / 'quarterly' / 'monthly' "
            "produce smoother animations at the cost of more frames + longer render time."
        ),
    )
    args = parser.parse_args()
    if args.tech == "both":
        techs = ["wind", "pv"]
    elif args.tech == "all":
        techs = ["wind", "pv", "bess"]
    else:
        techs = [args.tech]
    for t in techs:
        run_tech(t, cadence=args.cadence)


if __name__ == "__main__":
    main()
