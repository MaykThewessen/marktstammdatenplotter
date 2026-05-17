"""Interactive wind-capacity explorer (marimo notebook).

Run:    python -m marimo edit wind.py
Export: python -m marimo export html wind.py -o docs/wind.html
"""

import marimo

__generated_with = "0.23.5"
app = marimo.App(width="medium")


@app.cell
def _intro(mo):
    mo.md(r"""
    # Wind explorer · Marktstammdatenregister

    Interactive choropleth of installed **wind** capacity per German county.
    Slider walks the install date; the on/offshore toggle controls whether
    Nordsee + Ostsee turbines are included. Falls back to a synthetic demo
    dataset when no MaStR scrape is present.
    """)
    return


@app.cell
def _imports():
    from datetime import date
    import pandas as pd
    import mastr_plot

    return date, mastr_plot


@app.cell
def _data(mastr_plot, mo):
    df, demo = mastr_plot.load_records()
    units, units_demo = mastr_plot.load_admin_units()
    banner = mo.md(
        "> **Demo mode** — no MaStR scrape or kreise polygons found, "
        "synthetic data is used."
        if (demo or units_demo)
        else "> Loaded **real** MaStR records and OSM kreise polygons."
    )
    banner
    return df, units


@app.cell
def _controls(date, df, mo):
    min_d = df["install_date"].min().date() if df["install_date"].notna().any() else date(2000, 1, 1)
    max_d = date.today()
    date_slider = mo.ui.date(
        start=min_d, stop=max_d, value=date(2023, 1, 1), label="Snapshot date"
    )
    location = mo.ui.radio(
        options=["onshore + offshore", "onshore only", "offshore only"],
        value="onshore + offshore", label="Location",
    )
    bin_count = mo.ui.slider(start=3, stop=10, step=1, value=7, label="Color bins")
    cmap_picker = mo.ui.dropdown(
        options=["viridis", "plasma", "Blues", "GnBu", "BuPu"],
        value="GnBu", label="Colormap",
    )
    scale_picker = mo.ui.radio(
        options=["jenks", "linear", "log"], value="jenks", label="Color scale"
    )
    controls = mo.hstack(
        [date_slider, location, bin_count, cmap_picker, scale_picker], gap=2
    )
    controls
    return bin_count, cmap_picker, date_slider, location, scale_picker


@app.cell
def _filtered(df, location):
    w = df[df["energy_type"] == "Wind"].copy()
    if location.value == "onshore only":
        w = w[w["off_shore"].isna()]
    elif location.value == "offshore only":
        w = w[w["off_shore"].notna()]
    w
    return (w,)


@app.cell
def _map(
    bin_count,
    cmap_picker,
    date_slider,
    mastr_plot,
    mo,
    scale_picker,
    units,
    w,
):
    agg, active = mastr_plot.aggregate_by_unit(
        w, units, plot_date=date_slider.value, energy_type="Wind"
    )
    bins = mastr_plot.jenks_bins(
        agg["power_gw"][agg["power_gw"] > 0].to_numpy(), k=bin_count.value
    )
    fig = mastr_plot.plot_choropleth(
        agg, plot_date=date_slider.value,
        title=f"Wind capacity — {len(active):,} turbines active",
        bins=bins, cmap=cmap_picker.value, scale=scale_picker.value,
    )
    mo.mpl.interactive(fig)
    return active, agg


@app.cell
def _stats(active, agg, mo):
    total_gw = float(agg["power_gw"].sum())
    off = int(active["off_shore"].notna().sum()) if "off_shore" in active else 0
    on = len(active) - off
    median_kw = float(active["power"].median()) if len(active) else 0.0
    mo.md(
        f"""
        ## Summary

        - **Active turbines:** {len(active):,}  ·  onshore {on:,}  ·  offshore {off:,}
        - **Total installed capacity:** {total_gw:,.2f} GW
        - **Median turbine rating:** {median_kw/1e3:,.2f} MW
        - **Counties covered:** {(agg['power_gw'] > 0).sum():,} / {len(agg):,}
        """
    )
    return


@app.cell
def _growth(mo, w):
    growth_monthly = (
        w.assign(month=w["install_date"].dt.to_period("M"))
        .groupby(["month", w["off_shore"].notna().rename("offshore")])["power"]
        .sum()
        .div(1e6)
        .unstack(fill_value=0.0)
        .cumsum()
    )
    growth_monthly.index = growth_monthly.index.to_timestamp()
    import matplotlib.pyplot as growth_plt
    growth_fig, growth_ax = growth_plt.subplots(figsize=(9, 3.4), dpi=120)
    if False in growth_monthly.columns:
        growth_ax.fill_between(growth_monthly.index, 0, growth_monthly[False].values,
                               color="#86efac", alpha=0.5, label="onshore")
    if True in growth_monthly.columns:
        bottom = growth_monthly[False].values if False in growth_monthly.columns else 0
        growth_ax.fill_between(growth_monthly.index, bottom, bottom + growth_monthly[True].values,
                               color="#0ea5e9", alpha=0.6, label="offshore")
    growth_ax.set_ylabel("Cumulative wind [GW]")
    growth_ax.set_title("Cumulative wind capacity over time")
    growth_ax.legend(loc="upper left")
    growth_ax.grid(alpha=0.3)
    growth_fig.tight_layout()
    mo.mpl.interactive(growth_fig)
    return


@app.cell
def _import_mo():
    import marimo as mo

    return (mo,)


if __name__ == "__main__":
    app.run()
