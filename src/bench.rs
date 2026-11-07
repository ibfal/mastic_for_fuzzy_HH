//! Measurement helpers shared by the driver's `--bench-out` and the `keygen_bench` binary
//! (bench/BENCHMARK_MASTIC.md, A9): the run labels passed with `--bench-meta`, the header every
//! result line starts with, message sizes, and appending a line to a JSONL file.
//!
//! Nothing here is used unless one of those options is given.

use std::{
    fs,
    io::{self, Write},
    path::Path,
    time::{SystemTime, UNIX_EPOCH},
};

use bincode::Options;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::config::{Config, Encoding, Mode};

pub const SCHEMA_VERSION: u32 = 1;
pub const SYSTEM: &str = "mastic";

/// Size of a message as the RPC layer sends it. tarpc's transport serializes every request and
/// response with `bincode::DefaultOptions` (variable-length integers); this is that payload,
/// without tarpc's envelope (request id, deadline, trace context, method tag) and without the
/// 4-byte frame length (A4: framing is not counted).
pub fn wire_size<T: Serialize + ?Sized>(msg: &T) -> u64 {
    bincode::DefaultOptions::new()
        .serialized_size(msg)
        .expect("message is serializable")
}

/// Size with `bincode::serialize` (fixed 8-byte integers and lengths). The driver's key-size
/// printout and B3's key-size formula use this encoding; the wire does not.
pub fn fixint_size<T: Serialize + ?Sized>(msg: &T) -> u64 {
    bincode::serialized_size(msg).expect("message is serializable")
}

/// The labels the benchmark runner passes with `--bench-meta`, as a JSON object or `@file`.
#[derive(Deserialize, Debug, Clone)]
#[serde(deny_unknown_fields)]
pub struct BenchMeta {
    pub dataset: String,
    pub subset: String,
    pub rep: i64,
    pub warmup: bool,
    /// Start of the run (ISO 8601). Defaults to now, in UTC.
    #[serde(default)]
    pub timestamp: Option<String>,
    /// The `env` object of A9 (the runner collects it). Defaults to what Rust can tell.
    #[serde(default)]
    pub env: Option<Value>,
}

impl BenchMeta {
    /// Parse a `--bench-meta` value: a JSON object, or `@path` to read one from a file.
    pub fn parse(arg: &str) -> io::Result<Self> {
        let text = match arg.strip_prefix('@') {
            Some(path) => fs::read_to_string(path)?,
            None => arg.to_string(),
        };
        serde_json::from_str(&text)
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidInput, format!("--bench-meta: {e}")))
    }
}

#[derive(Serialize)]
pub struct Params {
    pub data_bits: Option<usize>,
    pub threshold_frac: f64,
    pub threshold_abs: u64,
    /// Mosaic's radius; does not apply to Mastic.
    pub delta: Option<u64>,
    pub encoding_params: Value,
}

/// The fields every result line starts with (A9), in the schema's order.
#[derive(Serialize)]
pub struct Header {
    pub schema_version: u32,
    pub system: &'static str,
    pub encoding: String,
    pub dataset: String,
    pub subset: String,
    pub n_clients: usize,
    pub rep: i64,
    pub warmup: bool,
    pub timestamp: String,
    pub params: Params,
    pub env: Value,
}

impl Header {
    /// Header for a run of `cfg` with `n_clients` clients. `threshold_abs` is the absolute
    /// threshold the run used (0 where it does not apply).
    pub fn new(
        cfg: &Config,
        meta: &BenchMeta,
        n_clients: usize,
        threshold_abs: u64,
    ) -> io::Result<Self> {
        let dataset = cfg.dataset.as_ref().ok_or_else(|| {
            io::Error::other("benchmark output needs a [dataset] with an encoding")
        })?;
        let Mode::WeightedHeavyHitters { threshold } = cfg.mode else {
            return Err(io::Error::other(
                "benchmark output only supports weighted heavy hitters",
            ));
        };
        let (encoding, encoding_params) = encoding_name_and_params(&dataset.encoding);
        Ok(Self {
            schema_version: SCHEMA_VERSION,
            system: SYSTEM,
            encoding,
            dataset: meta.dataset.clone(),
            subset: meta.subset.clone(),
            n_clients,
            rep: meta.rep,
            warmup: meta.warmup,
            timestamp: meta.timestamp.clone().unwrap_or_else(timestamp_utc),
            params: Params {
                data_bits: cfg.data_bits,
                threshold_frac: threshold,
                threshold_abs,
                delta: None,
                encoding_params,
            },
            env: meta.env.clone().unwrap_or_else(default_env),
        })
    }
}

/// `("geohash", {"prefix": "9v6", "bits": 21})` and the like; `("mosaic_grid", {})`.
pub fn encoding_name_and_params(encoding: &Encoding) -> (String, Value) {
    match serde_json::to_value(encoding).expect("encoding is serializable") {
        Value::String(name) => (name, json!({})),
        Value::Object(map) if map.len() == 1 => map.into_iter().next().unwrap(),
        other => panic!("unexpected encoding representation {other}"),
    }
}

/// The `env` object when the runner did not pass one: what Rust knows, 0 or null elsewhere.
pub fn default_env() -> Value {
    let logical = std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(0);
    let threads = std::env::var("RAYON_NUM_THREADS")
        .ok()
        .and_then(|t| t.parse::<usize>().ok())
        .unwrap_or(logical);
    json!({
        "cpu": "unknown",
        "cores_physical": 0,
        "cores_logical": logical,
        "ram_gb": 0,
        "os": std::env::consts::OS,
        "threads": threads,
        "compiler": null,
        "commit": null,
        "dirty": null,
    })
}

/// Current time in UTC, e.g. `2026-11-10T13:03:11+00:00`.
pub fn timestamp_utc() -> String {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    let (days, sod) = (secs.div_euclid(86_400), secs.rem_euclid(86_400));
    // Civil date from days since 1970-01-01 (H. Hinnant's algorithm).
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}+00:00",
        sod / 3_600,
        sod % 3_600 / 60,
        sod % 60
    )
}

/// Round milliseconds to microseconds, so result lines stay readable.
pub fn round_ms(ms: f64) -> f64 {
    (ms * 1_000.0).round() / 1_000.0
}

/// Append `line` as one JSON line to `path`, creating the file and its folder if needed.
pub fn append_jsonl<T: Serialize>(path: &str, line: &T) -> io::Result<()> {
    if let Some(dir) = Path::new(path).parent() {
        if !dir.as_os_str().is_empty() {
            fs::create_dir_all(dir)?;
        }
    }
    let mut text = serde_json::to_string(line).map_err(io::Error::other)?;
    text.push('\n');
    fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?
        .write_all(text.as_bytes())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn meta_parses_and_rejects_unknown_fields() {
        let meta = BenchMeta::parse(r#"{"dataset":"day","subset":"1of3","rep":2,"warmup":false}"#)
            .unwrap();
        assert_eq!((meta.rep, meta.warmup), (2, false));
        assert!(meta.env.is_none());
        assert!(BenchMeta::parse(
            r#"{"dataset":"day","subset":"1of3","rep":2,"warmup":false,"x":1}"#
        )
        .is_err());
    }

    #[test]
    fn encoding_names() {
        assert_eq!(
            encoding_name_and_params(&Encoding::MosaicGrid),
            ("mosaic_grid".to_string(), json!({}))
        );
        let geohash = Encoding::Geohash {
            prefix: "9v6".into(),
            bits: 21,
        };
        assert_eq!(
            encoding_name_and_params(&geohash),
            ("geohash".to_string(), json!({"prefix": "9v6", "bits": 21}))
        );
    }

    #[test]
    fn timestamp_format() {
        let t = timestamp_utc();
        assert_eq!(t.len(), "2026-11-10T13:03:11+00:00".len());
        assert!(t.ends_with("+00:00") && &t[4..5] == "-" && &t[10..11] == "T");
    }
}
