#!/usr/bin/env bash
# Build the city map: run Mastic once per encoding on the company-labelled month, check every run
# against the plaintext answer, and write the single-file map city/dist/city-map.html.
#
#   city/run.sh                    # all four encodings
#   city/run.sh morton h3          # only these
#
# Makes city/data/rides_month_companies.csv first if it is missing. For every encoding this writes
#   city/results/<encoding>.json   the crawl, from the driver's --export
#   city/logs/<encoding>.log       the driver's output (plus .server-{0,1}.log)
set -euo pipefail

cd "$(dirname "$0")/.."

encodings=("$@")
if [[ ${#encodings[@]} -eq 0 ]]; then
    encodings=(mosaic-grid geohash h3 morton)
fi
python=python3
[[ -x .venv/bin/python ]] && python=.venv/bin/python

[[ -f city/data/rides_month_companies.csv ]] || $python city/make_company_rides.py

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

for encoding in "${encodings[@]}"; do
    config="city/configs/city-month-$encoding.toml"
    if [[ ! -f $config ]]; then
        echo "skipping $encoding: no $config" >&2
        continue
    fi
    echo "=== $encoding"
    for id in 0 1; do
        target/release/server --config "$config" --server_id "$id" \
            >"city/logs/$encoding.server-$id.log" 2>&1 &
        server_pids+=($!)
    done
    for addr in $(grep -E '^server_[01] *=' "$config" | sed -E 's/.*"(.*)".*/\1/'); do
        wait_for_port "${addr##*:}"
    done
    # Every ride is one report; -n only caps the number of rides.
    if ! target/release/driver --config "$config" -n 100000000 \
        --export "city/results/$encoding.json" >"city/logs/$encoding.log" 2>&1; then
        tail -n 20 "city/logs/$encoding.log" >&2
        echo "driver failed for $encoding; full output in city/logs/$encoding.log" >&2
        exit 1
    fi
    grep -E 'DROPPED|Total time' "city/logs/$encoding.log" || true
    stop_servers
done

$python city/check_companies.py
$python city/build_map.py
