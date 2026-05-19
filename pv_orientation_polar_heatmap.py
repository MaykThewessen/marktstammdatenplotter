import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
from matplotlib.cm import ScalarMappable
from scipy.interpolate import interp1d

# --- Load & filter ---
df = pd.read_parquet(
    "BNetzA_MaStR/solar.parquet",
    columns=["orientation", "installed_capacity_kw", "commissioning_date", "status"],
)
active = df[df["status"] == "In Betrieb"].dropna(subset=["commissioning_date", "orientation"])
active = active.assign(year=active["commissioning_date"].dt.year.astype("Int64"))
active = active[active["year"].between(2000, 2025)]

MASTR_NAMES = {
    "N": "Nord", "NO": "Nord-Ost", "O": "Ost", "SO": "Süd-Ost",
    "S": "Süd", "SW": "Süd-West", "W": "West", "NW": "Nord-West",
}
FULL_NAMES = {
    "N": "Nord", "NO": "NO", "O": "Ost", "SO": "SO",
    "S": "Süd", "SW": "SW", "W": "West", "NW": "NW",
}
LABELS = list(MASTR_NAMES.keys())
DEGREES = [0, 45, 90, 135, 180, 225, 270, 315]

# Split Ost-West 50/50
ow = active[active["orientation"] == "Ost-West"].copy()
ow_half = ow.assign(installed_capacity_kw=ow["installed_capacity_kw"] / 2)

rows = {}
for code, name in MASTR_NAMES.items():
    rows[code] = active[active["orientation"] == name][["year", "installed_capacity_kw"]]
rows["O"] = pd.concat([rows["O"], ow_half[["year", "installed_capacity_kw"]]])
rows["W"] = pd.concat([rows["W"], ow_half[["year", "installed_capacity_kw"]]])

years = list(range(2000, 2026))

# Matrix: (n_years=26) × (n_dirs=8), GW
matrix = np.zeros((len(years), len(LABELS)))
for j, code in enumerate(LABELS):
    grouped = rows[code].groupby("year")["installed_capacity_kw"].sum() / 1e6
    for i, yr in enumerate(years):
        matrix[i, j] = grouped.get(yr, 0.0)

# --- Smooth circular interpolation: 8 directions → 720 points ---
N_THETA = 720
theta_fine_deg = np.linspace(0, 360, N_THETA, endpoint=False)
deg_arr = np.array(DEGREES, dtype=float)

matrix_smooth = np.zeros((len(years), N_THETA))
for i in range(len(years)):
    vals = matrix[i, :]
    # Wrap for periodic cubic interpolation
    deg_wrap = np.append(deg_arr, deg_arr[0] + 360.0)
    vals_wrap = np.append(vals, vals[0])
    f = interp1d(deg_wrap, vals_wrap, kind="cubic")
    interp_vals = f(theta_fine_deg)
    matrix_smooth[i, :] = np.clip(interp_vals, 0, None)

# --- Build meshgrid ---
# pcolormesh flat: THETA/R → (M+1, N+1), C → (M, N)
# M=26 years, N=720 theta cells
theta_edges = np.deg2rad(np.linspace(0, 360, N_THETA + 1))  # 721
r_edges = np.array(years + [2026], dtype=float)              # 27
THETA, R = np.meshgrid(theta_edges, r_edges)                 # (27, 721)
# C is (26, 720): don't wrap column — theta 0→360 is already closed
C = matrix_smooth

# --- Figure ---
fig = plt.figure(figsize=(12, 12), facecolor="#0d0d0d")
ax = fig.add_subplot(111, projection="polar")
ax.set_facecolor("#0d0d0d")
ax.set_theta_zero_location("N")
ax.set_theta_direction(-1)  # clockwise

cmap = mcolors.LinearSegmentedColormap.from_list(
    "green_yellow_red", ["#006400", "#32cd32", "#ffff00", "#ff4500", "#8b0000"]
)
norm = mcolors.Normalize(vmin=0, vmax=matrix_smooth.max())

ax.pcolormesh(THETA, R, C, cmap=cmap, norm=norm, shading="flat", rasterized=True)

# --- Radial axis ---
ax.set_ylim(r_edges[0], r_edges[-1])
ax.set_yticks(years[::5])
ax.set_yticklabels(
    [str(y) for y in years[::5]],
    fontsize=7.5, color="white", fontfamily="monospace",
)
ax.set_rlabel_position(8)  # place year labels slightly off North

# --- Angular axis: every 30° degree labels ---
tick_degs = np.arange(0, 360, 30)
ax.set_xticks(np.deg2rad(tick_degs))
ax.set_xticklabels(
    [f"{d}°" for d in tick_degs],
    fontsize=8.5, color="white",
)
ax.tick_params(axis="x", pad=8, colors="white")

# Subtle radial gridlines
for yr in years[::5]:
    theta_ring = np.linspace(0, 2 * np.pi, 500)
    ax.plot(theta_ring, [yr] * 500, color="white", lw=0.4, alpha=0.35)

# Compass direction labels outside the ring
COMPASS_LABELS = {0: "Nord", 90: "Ost", 180: "Süd", 270: "West",
                   45: "NO", 135: "SO", 225: "SW", 315: "NW"}
for deg, lbl in COMPASS_LABELS.items():
    bold = deg % 90 == 0
    ax.text(
        np.deg2rad(deg), 2027,
        lbl,
        ha="center", va="center",
        fontsize=10 if bold else 8,
        fontweight="bold" if bold else "normal",
        color="white",
    )

# Total GW per direction (small, below compass label)
total_per_dir = matrix.sum(axis=0)
for j, (code, deg) in enumerate(zip(LABELS, DEGREES)):
    gw = total_per_dir[j]
    ax.text(
        np.deg2rad(deg), 2024.5,
        f"{gw:.0f} GW" if gw >= 10 else f"{gw:.1f}",
        ha="center", va="center",
        fontsize=6.5, color="white", alpha=0.85,
    )

# Outer ring border circle
theta_border = np.linspace(0, 2 * np.pi, 600)
ax.plot(theta_border, [r_edges[-1]] * 600, color="white", lw=1.2, alpha=0.7)

# Colorbar
cbar = fig.colorbar(
    ScalarMappable(norm=norm, cmap=cmap),
    ax=ax, pad=0.08, fraction=0.025, aspect=35, shrink=0.7,
)
cbar.set_label("GW commissioned per year", fontsize=9, color="white", labelpad=8)
cbar.ax.tick_params(labelsize=8, colors="white")
cbar.ax.yaxis.set_tick_params(color="white")
plt.setp(cbar.ax.yaxis.get_ticklines(), color="white")

# Title
total_gw_all = matrix.sum()
peak_yr = years[int(np.argmax(matrix.sum(axis=1)))]
ax.set_title(
    f"German PV — capacity by orientation & commissioning year\n"
    f"MaStR · {total_gw_all:.0f} GW total · peak year: {peak_yr}",
    fontsize=12, pad=24, color="white",
)

plt.tight_layout()
out = "fig/pv_orientation_polar_heatmap.png"
plt.savefig(out, dpi=160, bbox_inches="tight", facecolor="#0d0d0d")
print(f"Saved → {out}")
