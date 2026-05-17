"""Interactive PV-capacity explorer (marimo notebook).

Run:    python -m marimo edit pv.py
Export: python -m marimo export html pv.py -o docs/pv.html
"""

import marimo

__generated_with = "0.23.5"
app = marimo.App(width="medium")


@app.cell
def _intro(mo):
    mo.md(r"""
    # PV explorer · Marktstammdatenregister

    Interactive choropleth of installed **photovoltaic** capacity per German
    county. Drag the date slider to step through history, toggle the
    installation-type filter to isolate rooftop vs. ground-mount vs. balcony
    plants. If no local data are present, a synthetic demo dataset is used.
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
        "> **Demo mode** — no `data-*.json` or `germany_kreise.gpkg` was found, "
        "so synthetic plants on a coarse grid are shown."
        if (demo or units_demo)
        else "> Loaded **real** MaStR records and OSM kreise polygons."
    )
    banner
    return df, units


@app.cell
def _controls(date, df, mo):
    min_d = df["install_date"].min().date() if df["install_date"].notna().any() else date(2005, 1, 1)
    max_d = date.today()
    date_slider = mo.ui.date(
        start=min_d, stop=max_d, value=date(2023, 1, 1), label="Snapshot date"
    )
    install_types = ["(all)"] + sorted(
        t for t in df["installation_type"].dropna().unique().tolist()
    )
    type_filter = mo.ui.dropdown(
        options=install_types, value="(all)", label="Installation type"
    )
    bin_count = mo.ui.slider(start=3, stop=10, step=1, value=7, label="Color bins")
    cmap_picker = mo.ui.dropdown(
        options=["viridis", "plasma", "magma", "cividis", "YlOrRd"],
        value="YlOrRd", label="Colormap",
    )
    scale_picker = mo.ui.radio(
        options=["jenks", "linear", "log"], value="jenks", label="Color scale"
    )
    controls = mo.hstack(
        [date_slider, type_filter, bin_count, cmap_picker, scale_picker], gap=2
    )
    controls
    return bin_count, cmap_picker, date_slider, scale_picker, type_filter


@app.cell
def _filtered(df, type_filter):
    pv = df[df["energy_type"] == "Solare Strahlungsenergie"].copy()
    if type_filter.value != "(all)":
        pv = pv[pv["installation_type"] == type_filter.value]
    pv
    return (pv,)


@app.cell
def _map(
    bin_count,
    cmap_picker,
    date_slider,
    mastr_plot,
    mo,
    pv,
    scale_picker,
    units,
):
    agg, active = mastr_plot.aggregate_by_unit(
        pv, units, plot_date=date_slider.value, energy_type="Solare Strahlungsenergie"
    )
    bins = mastr_plot.jenks_bins(
        agg["power_gw"][agg["power_gw"] > 0].to_numpy(), k=bin_count.value
    )
    fig = mastr_plot.plot_choropleth(
        agg, plot_date=date_slider.value,
        title=f"PV capacity — {len(active):,} plants active",
        bins=bins, cmap=cmap_picker.value, scale=scale_picker.value,
    )
    mo.mpl.interactive(fig)
    return active, agg


@app.cell
def _stats(active, agg, mo):
    total_gw = float(agg["power_gw"].sum())
    private_share = float(active["is_private"].mean()) if len(active) else 0.0
    mo.md(
        f"""
        ## Summary

        - **Active plants:** {len(active):,}
        - **Total installed capacity:** {total_gw:,.2f} GW
        - **Private-owner share:** {private_share*100:,.1f}%
        - **Counties covered:** {(agg['power_gw'] > 0).sum():,} / {len(agg):,}
        """
    )
    return


@app.cell
def _growth(mo, pv):
    growth_monthly = (
        pv.assign(month=pv["install_date"].dt.to_period("M"))
        .groupby("month")["power"].sum().div(1e6).cumsum()
    )
    growth_monthly.index = growth_monthly.index.to_timestamp()
    import matplotlib.pyplot as growth_plt
    growth_fig, growth_ax = growth_plt.subplots(figsize=(9, 3.4), dpi=120)
    growth_ax.fill_between(growth_monthly.index, growth_monthly.values, color="#f59e0b", alpha=0.35)
    growth_ax.plot(growth_monthly.index, growth_monthly.values, color="#b45309", linewidth=2)
    growth_ax.set_ylabel("Cumulative PV [GW]")
    growth_ax.set_title("Cumulative PV capacity over time")
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
