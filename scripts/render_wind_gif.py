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
        "basename": "pv-2005-may2026",
        "cmap": "YlOrRd",
        "noun": "plants",
        "title": "Installed PV capacity in Germany",
        "extra_subtitle": " ≥ 49 kW (top 200k by capacity)",
    },
    "bess": {
        "kind": "bess",
        "frame_prefix": "bess",
        "frames_dir": "fig/frames-bess",
        "basename": "bess-2005-may2026",
        "cmap": "BuPu",
        "noun": "units",
        "title": "Installed battery storage power in Germany",
        "extra_subtitle": " (top 200k by power)",
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


FRAME_EXT = "webp"
TARGET_HEIGHT = 1440
FFMPEG_FRAMERATE = "3"


def _aggregate(records, units, snap, cfg):
    """Dispatch on cfg['kind'] — generation tech or BESS."""
    if cfg["kind"] == "bess":
        return mastr_plot.aggregate_bess_by_unit(records, units, snap)
    return mastr_plot.aggregate_by_unit(records, units, snap, cfg["energy_type"])


def _active_power_gw(active, cfg):
    col = "power_kw" if cfg["kind"] == "bess" else "power"
    return active[col].sum() / 1e6 if len(active) else 0.0


def _noun_count(active, cfg):
    return f"{len(active):,} {cfg['noun']}{cfg['extra_subtitle']}"


def render_frames(records, units, cfg) -> tuple[int, Path]:
    frames = ROOT / cfg["frames_dir"]
    frames.mkdir(parents=True, exist_ok=True)
    for old in frames.iterdir():
        if old.is_file():
            old.unlink()

    dates = snapshot_dates()
    agg_max, _ = _aggregate(records, units, dates[-1], cfg)
    positive = agg_max["power_gw"][agg_max["power_gw"] > 0].to_numpy()
    bins = mastr_plot.jenks_bins(positive, k=8)

    idx = 0
    for snap in dates:
        agg, active = _aggregate(records, units, snap, cfg)
        total_gw = _active_power_gw(active, cfg)
        title = (
            f"{cfg['title']} — {snap.isoformat()}\n"
            f"{_noun_count(active, cfg)} · {round(total_gw, 1)} GW"
        )
        fig = mastr_plot.plot_choropleth(
            agg, snap, title, bins=bins, cmap=cfg["cmap"],
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


def _assemble(frames: Path, cfg, ext: str, vcodec_args: list[str]):
    """Run ffmpeg over the frame sequence to produce `<basename>.<ext>`."""
    out_path = ROOT / "fig" / f"{cfg['basename']}.{ext}"
    docs_path = ROOT / "docs" / "assets" / f"{cfg['basename']}.{ext}"
    cmd = [
        "ffmpeg", "-y", "-framerate", FFMPEG_FRAMERATE,
        "-i", str(frames / f"{cfg['frame_prefix']}-%03d.{FRAME_EXT}"),
        *vcodec_args,
        str(out_path),
    ]
    subprocess.run(cmd, check=True)
    shutil.copy(out_path, docs_path)
    return out_path


def assemble_gif(frames: Path, cfg):
    return _assemble(
        frames, cfg, "gif",
        [
            "-vf",
            f"scale=-1:{TARGET_HEIGHT}:flags=lanczos,"
            "split[s0][s1];[s0]palettegen=max_colors=224[p];"
            "[s1][p]paletteuse=dither=bayer",
            "-loop", "0",
        ],
    )


def assemble_mp4(frames: Path, cfg):
    """H.264 MP4 — LinkedIn-native, ~30-50% smaller than the GIF.

    yuv420p + even-dimensions for maximum player compatibility.
    """
    return _assemble(
        frames, cfg, "mp4",
        [
            "-vf",
            f"scale=-2:{TARGET_HEIGHT}:flags=lanczos,fps=10,format=yuv420p",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-preset", "slow",
            "-crf", "18",
            "-movflags", "+faststart",
        ],
    )


def run_tech(tech: str):
    cfg = PRESETS[tech]
    if cfg["kind"] == "bess":
        records, demo = mastr_plot.load_bess()
    else:
        records, demo = mastr_plot.load_records()
    if demo:
        print(f"WARNING: rendering {tech} animation from demo data.")
    units, _ = mastr_plot.load_admin_units()
    n, frames = render_frames(records, units, cfg)
    print(f"Rendered {n} WebP frames for {tech}.")
    gif = assemble_gif(frames, cfg)
    print(f"GIF written to {gif.relative_to(ROOT)} ({gif.stat().st_size / 1024:.0f} KiB)")
    mp4 = assemble_mp4(frames, cfg)
    print(f"MP4 written to {mp4.relative_to(ROOT)} ({mp4.stat().st_size / 1024:.0f} KiB)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "tech", nargs="?", default="wind",
        choices=["wind", "pv", "bess", "both", "all"],
        help="Which animation to build (default: wind). 'both' = wind+pv, 'all' = wind+pv+bess.",
    )
    args = parser.parse_args()
    if args.tech == "both":
        techs = ["wind", "pv"]
    elif args.tech == "all":
        techs = ["wind", "pv", "bess"]
    else:
        techs = [args.tech]
    for t in techs:
        run_tech(t)


if __name__ == "__main__":
    main()
