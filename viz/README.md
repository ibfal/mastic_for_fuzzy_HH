# Crawl viewer

Four synced maps, one per encoding (Mosaic grid, geohash, H3, Morton), with a slider that steps
through the levels of the heavy-hitters crawl. At each level every candidate prefix is drawn as
its region: filled if its count reached the threshold and the crawl expands it, dashed if it was
pruned. This is what the collector learns, including the counts of pruned prefixes.

## Save runs

```sh
scripts/run_encodings.sh synthetic-ten-locations     # mosaic-grid, geohash, h3, morton
scripts/run_encodings.sh ride-austin-day
scripts/run_encodings.sh ride-austin-week morton-shifted morton-h3res10-area   # other configs
```

For each `src/configs/<dataset>-<encoding>.toml` this starts the two servers, runs the driver
with `--export`, and stops the servers. It writes

- `results/<dataset>-<encoding>.json`: every crawl level and the heavy hitters
- `logs/<dataset>-<encoding>.log`: the driver's output (and `.server-0.log`, `.server-1.log`)

and then runs `scripts/build_viz.py`, which rebuilds `results/manifest.json` and
`dist/mastic-crawl.html`. Rerunning a config overwrites its files. Run `build_viz.py` yourself
after deleting or copying in result files.

## View

- While developing: `python3 -m http.server -d viz 8080`, then open http://localhost:8080. The
  page reads `results/` on every load, so a new run shows up on refresh.
- To share: send `dist/mastic-crawl.html`. It has every run inlined and opens straight from disk.

Each pane's menu can show any saved run of the selected dataset, for example a Morton variant
next to the default Morton box. Arrow keys step through the levels, space plays. The map tiles
need an internet connection.
