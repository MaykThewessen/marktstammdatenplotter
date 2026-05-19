# Contributing

Thanks for considering a contribution. This is research-quality code — the
bar is "useful + reproducible", not enterprise polish.

## Quick start

```bash
git clone https://github.com/MaykThewessen/marktstammdatenplotter.git
cd marktstammdatenplotter
pixi install                              # one-shot env setup
pixi run fetch-kreise                     # 1 MB GeoJSON, one-off
pixi run scrape-non-pv                    # 7 pages of registry JSON
pixi run scrape-pv-top                    # 8 pages of PV ≥ 49 kW
pixi run render-samples                   # regenerate all SVG charts
pixi run render-gifs                      # rebuild both animation GIFs
pixi run docs-build                       # re-export the marimo notebooks
pixi run test                             # 52 parser tests, <1 second
```

## Repository layout

```
parser.py              PowerPlant dataclass + JSON decoder
mastr_plot.py          Shared loaders, spatial joins, choropleth helpers
pv.py / wind.py        Marimo reactive notebooks
docs/                  RTD-styled GitHub Pages site (served at /docs)
fig/                   Rendered SVG / GIF outputs
scripts/               CI helpers — must be idempotent
tests/                 pytest suite
.github/workflows/     Weekly refresh CI
pixi.toml              Reproducible env + named tasks
```

## How to add a new chart

1. Add a `render_<name>()` function in `scripts/render_samples.py`. Mirror
   the existing pattern: write the SVG into both `fig/` and `docs/assets/`.
2. Call it from `main()` so the weekly CI keeps it fresh.
3. Embed it in `docs/index.html` (and `README.md` if it deserves a hero slot).
4. Update `CHANGELOG.md` under `[Unreleased] / Added`.

## How to add a parser enum

1. Add the case branch to the appropriate `match` in `parser.py`.
   **Treat unknown codes as `None` — do not raise.**
2. Add a `pytest.mark.parametrize` case in `tests/test_parser.py`.
3. Update the corresponding box in `fig/enum-decoding.svg`.

## Style

- Vectorise — no row-wise `for` or `.apply(lambda)` over DataFrames.
- Timestamps tz-aware UTC. Strip tz only at display boundaries.
- File names: kebab-case, descriptive. SVGs use `sample-<topic>.svg`.
- Match the surrounding comment density: short, English, only where intent
  is non-obvious.

## Commit messages

- Imperative subject under 70 chars: `Add wind age histogram`.
- Body: explain the *why* and any non-obvious trade-offs.
- One commit per logical change. Squash before opening a PR if a branch
  has fixup commits.
- No `Co-Authored-By` trailers unless requested.

## Pull requests

- Open against `main`.
- The PR template's test-plan checklist must pass before review:
  `pixi run test`, `pixi run render-samples`, `pixi run docs-build`.
- For new charts: paste before/after SVG / PNG screenshots.

## Reporting bugs

Use the `bug_report` issue template. The most useful bug reports include
the *exact* MaStR enum code or Kreis name that misbehaves.

## License

The upstream repo carries no license file. Pending an explicit decision,
contributions are accepted "as-is, research-quality" — the same terms as
the rest of the code. See `CITATION.cff` for citation metadata.
