use std::fs;

use clap::{App, Arg};
use serde::Deserialize;

/// Parameters used by the driver program to generate test data.
#[derive(Deserialize, Debug)]
#[serde(rename_all = "snake_case")]
pub enum Mode {
    /// Simulate weighted heavy hitters. In this mode, the aggregators compute the subset of inputs
    /// whose total weight exceed a given threshold.
    WeightedHeavyHitters {
        /// The weighted heavy hitters threshold.
        threshold: f64,
    },

    /// Simulate attribute-based metrics. In this mode, the aggregators agree on a set of
    /// attributes to use to partition the metrics.
    AttributeBasedMetrics {
        /// For testing purposes, the driver will sample `num_attributes` inputs from the same
        /// distribution as the clients.
        num_attributes: usize,
    },

    /// Simulate plain metrics, aggregated using Prio3 instead of Mastic.
    PlainMetrics,
}

/// How a pickup location is encoded into the Mastic input string `alpha`.
#[derive(Deserialize, Debug, Clone)]
#[serde(rename_all = "snake_case")]
pub enum Encoding {
    /// Mosaic's RideAustin grid: 0.001 degree cells, 11 bits per axis (`data_bits = 22`).
    MosaicGrid,

    /// Standard geohash. Every point must lie inside the cell named by `prefix`; those bits are
    /// shared by all rides, so only the next `bits` bits are sent (`data_bits = bits`).
    Geohash {
        /// Base32 geohash covering the whole area, e.g. "9v6" for Austin.
        prefix: String,
        /// Bits after the prefix. Each bit halves the cell, alternating longitude and latitude.
        bits: usize,
    },

    /// Uber's H3 hexagonal grid. Every point must lie inside the H3 cell `prefix`; only the
    /// 3-bit digits for the resolutions below it are sent (`data_bits = 3 * (resolution -
    /// prefix resolution)`).
    H3 {
        /// H3 cell covering the whole area, e.g. "82489ffffffffff" (resolution 2) for Austin.
        prefix: String,
        /// Resolution of the cells to count, 0 (coarsest) to 15.
        resolution: u8,
    },

    /// Morton (Z-order) code over a bounding box. Each axis is split into `2^axis_bits` equal
    /// cells, `(max - min) / 2^axis_bits` degrees wide, and the two cell indices are interleaved,
    /// longitude first, most significant bit first (`data_bits = 2 * axis_bits`). Points outside
    /// the box are dropped.
    Morton {
        /// South edge of the box (inclusive), in degrees.
        min_lat: f64,
        /// North edge of the box (exclusive), in degrees.
        max_lat: f64,
        /// West edge of the box (inclusive), in degrees.
        min_lon: f64,
        /// East edge of the box (exclusive), in degrees.
        max_lon: f64,
        /// Bits per axis, 1 to 31.
        axis_bits: usize,
    },
}

/// What each ride contributes to the histogram `beta`.
#[derive(Deserialize, Debug, Clone, Copy, Default)]
#[serde(rename_all = "snake_case")]
pub enum Weight {
    /// Every ride counts once, as in Poplar (`hist_buckets = 1`).
    #[default]
    Constant,
}

/// Use real ride data instead of synthetic Zipf inputs.
#[derive(Deserialize, Debug, Clone)]
pub struct DatasetConfig {
    /// Path to a CSV with `start_location_lat` and `start_location_long` columns.
    pub path: String,

    pub encoding: Encoding,

    #[serde(default)]
    pub weight: Weight,
}

#[derive(Deserialize)]
pub struct Config {
    /// Number of bits of each string.
    pub data_bits: Option<usize>,

    /// Number of histogram buckets for the FLP range check.
    pub hist_buckets: usize,

    /// The servers will output the collection of strings that more than a `threshold` of clients
    /// hold.
    pub add_report_share_batch_size: usize,

    /// Similar to `add_report_share_batch_size` but with a greater threshold.
    pub query_flp_batch_size: usize,

    /// Zipf parameter: Number of distinct strings.
    pub zipf_unique_buckets: usize,

    /// Mode of operation in which to test Mastic.
    pub mode: Mode,

    /// Zipf parameter: The distribution from which each client chooses its input.
    pub zipf_exponent: f64,

    /// The `IP:port` tuple for server 0.
    pub server_0: String,

    /// The `IP:port` tuple for server 1.
    pub server_1: String,

    /// Optional ride dataset. When set, the driver sends one report per ride.
    pub dataset: Option<DatasetConfig>,
}

pub fn get_config(filename: &str) -> Config {
    let toml_data = &fs::read_to_string(filename).expect("Cannot open TOML file");
    toml::from_str(toml_data).expect("Cannot parse TOML config")
}

pub fn get_args(
    name: &str,
    get_server_id: bool,
    get_n_reqs: bool,
    get_malicious: bool,
) -> (Config, i8, usize, f32) {
    let mut flags = App::new(name)
        .version("0.1")
        .about("Mastic: Private Aggregated Statistics through Fully Linear Proofs")
        .arg(
            Arg::with_name("config")
                .short("c")
                .long("config")
                .value_name("FILENAME")
                .help("Location of JSON config file")
                .required(true)
                .takes_value(true),
        )
        .arg(
            Arg::with_name("server0")
                .short("s0")
                .long("server-0")
                .value_name("STRING")
                .help("Aggregator 0 host path to connect to, e.g., 0.0.0.0:8000")
                .required(false)
                .takes_value(true),
        )
        .arg(
            Arg::with_name("server1")
                .short("s0")
                .long("server-1")
                .value_name("STRING")
                .help("Aggregator 1 host path to connect to, e.g., 0.0.0.0:8001")
                .required(false)
                .takes_value(true),
        );
    if get_server_id {
        flags = flags.arg(
            Arg::with_name("server_id")
                .short("i")
                .long("server_id")
                .value_name("NUMBER")
                .help("Zero-indexed ID of server")
                .required(true)
                .takes_value(true),
        );
    }
    if get_n_reqs {
        flags = flags.arg(
            Arg::with_name("num_clients")
                .short("n")
                .long("num_clients")
                .value_name("NUMBER")
                .help("Number of client requests to generate")
                .required(true)
                .takes_value(true),
        );
    }
    if get_malicious {
        flags = flags.arg(
            Arg::with_name("malicious")
                .short("m")
                .long("malicious")
                .value_name("NUMBER")
                .help("Percentage of malicious clients")
                .required(false)
                .takes_value(true),
        );
    }

    let flags = flags.get_matches();
    let mut config = get_config(flags.value_of("config").unwrap());

    let mut server_id = -1;
    if get_server_id {
        server_id = flags.value_of("server_id").unwrap().parse().unwrap();
    } else {
        // If it's the leader.
        if flags.is_present("server0") {
            config.server_0 = flags.value_of("server0").unwrap().parse().unwrap();
        }
        if flags.is_present("server1") {
            config.server_1 = flags.value_of("server1").unwrap().parse().unwrap();
        }
    }

    let mut n_reqs = 0;
    if get_n_reqs {
        n_reqs = flags.value_of("num_clients").unwrap().parse().unwrap();
    }

    let mut malicious = 0.0;
    if flags.is_present("malicious") {
        malicious = flags.value_of("malicious").unwrap().parse::<f32>().unwrap();
    }

    (config, server_id, n_reqs, malicious)
}
