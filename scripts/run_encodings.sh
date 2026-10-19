#!/usr/bin/env bash
# Run the heavy-hitters crawl once per encoding and save what viz/index.html shows.
#
#   scripts/run_encodings.sh ride-austin-day                      # the four encodings
#   scripts/run_encodings.sh synthetic-ten-locations
#   scripts/run_encodings.sh ride-austin-week morton-shifted h3   # any configs for that dataset
#
# Each config src/configs/<dataset>-<encoding>.toml gets its own pair of servers (they fix
# data_bits at startup), one at a time. For every run this writes
#   viz/results/<dataset>-<encoding>.json   the crawl, from the driver's --export
#   viz/logs/<dataset>-<encoding>.log       the driver's output
#   viz/logs/<dataset>-<encoding>.server-{0,1}.log
# and then rebuilds viz/results/manifest.json and the single-file viz/dist/mastic-crawl.html.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ $# -lt 1 ]]; then
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
fi
dataset=$1
shift
encodings=("$@")
if [[ ${#encodings[@]} -eq 0 ]]; then
    encodings=(mosaic-grid geohash h3 morton)
fi

# With a dataset every ride is one client; -n only caps the number of rides.
num_clients=${NUM_CLIENTS:-100000000}

mkdir -p viz/results viz/logs
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
    local port=$1
    for _ in $(seq 1 100); do
        if nc -z 127.0.0.1 "$port" 2>/dev/null; then
            return 0
        fi
        sleep 0.1
    done
    echo "server on port $port did not start" >&2
    return 1
}

for encoding in "${encodings[@]}"; do
    name="$dataset-$encoding"
    config="src/configs/$name.toml"
    if [[ ! -f $config ]]; then
        echo "skipping $name: no $config" >&2
        continue
    fi
    echo "=== $name"

    for id in 0 1; do
        target/release/server --config "$config" --server_id "$id" \
            >"viz/logs/$name.server-$id.log" 2>&1 &
        server_pids+=($!)
    done
    for addr in $(grep -E '^server_[01] *=' "$config" | sed -E 's/.*"(.*)".*/\1/'); do
        wait_for_port "${addr##*:}"
    done

    if ! target/release/driver --config "$config" -n "$num_clients" \
        --export "viz/results/$name.json" >"viz/logs/$name.log" 2>&1; then
        tail -n 20 "viz/logs/$name.log" >&2
        echo "driver failed for $name; full output in viz/logs/$name.log" >&2
        exit 1
    fi
    grep -E 'DROPPED|Total time|Wrote crawl' "viz/logs/$name.log" || true
    stop_servers
done

python3 scripts/build_viz.py
