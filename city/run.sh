#!/usr/bin/env bash
# Build the city map: run Mastic on the company-labelled busiest day once per tab (pickup, drop-off)
# and encoding, check every run against the plaintext answer, and write the single-file map
# city/dist/city-map.html.
#
#   city/run.sh                            # both tabs, all four encodings
#   city/run.sh dropoff-morton pickup-h3   # only these runs (<tab>-<encoding>)
#
# Makes city/data/rides_day_companies.csv first if it is missing. Run <tab>-<encoding> uses
# city/configs/city-day-<tab>-<encoding>.toml and writes
#   city/results/<tab>-<encoding>.json   the crawl, from the driver's --export
#   city/logs/<tab>-<encoding>.log       the driver's output (plus .server-{0,1}.log)
set -euo pipefail

cd "$(dirname "$0")/.."

runs=("$@")
if [[ ${#runs[@]} -eq 0 ]]; then
    for tab in pickup dropoff; do
        for encoding in mosaic-grid geohash h3 morton; do
            runs+=("$tab-$encoding")
        done
    done
fi
python=python3
[[ -x .venv/bin/python ]] && python=.venv/bin/python

[[ -f city/data/rides_day_companies.csv ]] || $python city/make_company_rides.py

mkdir -p city/results city/logs
cargo build --release --bin server --bin driver

server_pids=()
stop_servers() {
    for pid in "${server_pids[@]+"${server_pids[@]}"}"; do
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    done
    server_pids=()
}
trap stop_servers EXIT

wait_for_port() {
    for _ in $(seq 1 100); do
        nc -z 127.0.0.1 "$1" 2>/dev/null && return 0
        sleep 0.1
    done
    echo "server on port $1 did not start" >&2
    return 1
}

for name in "${runs[@]}"; do
    config="city/configs/city-day-$name.toml"
    if [[ ! -f $config ]]; then
        echo "skipping $name: no $config" >&2
        continue
    fi
    echo "=== $name"
    for id in 0 1; do
        target/release/server --config "$config" --server_id "$id" \
            >"city/logs/$name.server-$id.log" 2>&1 &
        server_pids+=($!)
    done
    for addr in $(grep -E '^server_[01] *=' "$config" | sed -E 's/.*"(.*)".*/\1/'); do
        wait_for_port "${addr##*:}"
    done
    # Every ride is one report; -n only caps the number of rides.
    if ! target/release/driver --config "$config" -n 100000000 \
        --export "city/results/$name.json" >"city/logs/$name.log" 2>&1; then
        tail -n 20 "city/logs/$name.log" >&2
        echo "driver failed for $name; full output in city/logs/$name.log" >&2
        exit 1
    fi
    grep -E 'DROPPED|Total time' "city/logs/$name.log" || true
    stop_servers
done

$python city/check_companies.py
$python city/build_map.py
