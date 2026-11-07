//! Client micro-benchmark (bench/BENCHMARK_MASTIC.md, A6): the time one client needs to turn its
//! location into its two report shares, on one thread.
//!
//!     keygen_bench -c <config.toml> [--csv bench/data/day_3of3.csv] [-k 2000]
//!                  [--out bench/results/raw/mastic_client.jsonl] [--bench-meta <json|@file>]
//!
//! The config gives the encoding, `data_bits` and `hist_buckets`; the rides come from `--csv`.
//! Per client it times what the driver's `generate_reports` does for weighted heavy hitters:
//! encode the location (alpha) and weight (beta), `VidpfKey::gen`, the FLP joint-randomness
//! parts, `prove`, and splitting the proof into two shares, ending with the two `ReportShare`s
//! the servers receive. One untimed warm-up pass over the same rides comes first.

use std::{
    io,
    time::{Duration, Instant},
};

use clap::{App, Arg};
use mastic::{
    bench::{append_jsonl, fixint_size, wire_size, BenchMeta, Header},
    collect::{KeyCollection, ReportShare},
    config::{self, DatasetConfig},
    dataset::{self, Ride},
    geo::{self, LocationEncoder},
    prg::PrgSeed,
    vidpf::VidpfKey,
    Mastic, MasticHistogram,
};
use prio::{
    field::{random_vector, Field128},
    vdaf::xof::{IntoFieldVec, Xof, XofShake128},
};
use rand::{thread_rng, Rng};
use rand_core::RngCore;
use serde::Serialize;

/// Time spent in each part of one client's report generation.
#[derive(Clone, Copy)]
struct Parts {
    encode: Duration,
    vidpf: Duration,
    flp: Duration,
    total: Duration,
}

/// One client's report shares, as `generate_reports` (driver.rs) and `to_shares` build them.
/// Returns `None` if the location lies outside the encoded area.
fn generate(
    mastic: &MasticHistogram,
    encoder: &dyn LocationEncoder,
    dataset: &DatasetConfig,
    ride: &Ride,
) -> Option<([ReportShare; 2], Parts)> {
    let t0 = Instant::now();
    let alpha = encoder.encode(ride.start_location_lat, ride.start_location_long)?;
    let beta = mastic
        .encode_measurement(&dataset.weight.bucket(ride))
        .unwrap();
    let t1 = Instant::now();

    let mut rng = thread_rng();
    let nonce = rng.gen::<[u8; 16]>();
    let (key_0, key_1) = VidpfKey::gen(&alpha, &beta);
    let t2 = Instant::now();

    let jr_parts = {
        let vidpf_seeds = (key_0.get_root_seed().key, key_1.get_root_seed().key);
        let mut jr_parts = [[0u8; 16]; 2];
        let mut jr_part_0_xof = XofShake128::init(&vidpf_seeds.0, &[0u8; 16]);
        jr_part_0_xof.update(&[0]); // Aggregator ID
        jr_part_0_xof.update(&nonce);
        jr_part_0_xof
            .into_seed_stream()
            .fill_bytes(&mut jr_parts[0]);

        let mut jr_part_1_xof = XofShake128::init(&vidpf_seeds.1, &[0u8; 16]);
        jr_part_1_xof.update(&[1]); // Aggregator ID
        jr_part_1_xof.update(&nonce);
        jr_part_1_xof
            .into_seed_stream()
            .fill_bytes(&mut jr_parts[1]);
        jr_parts
    };

    let (proof_0, proof_1) = {
        let joint_rand_xof = XofShake128::init(&jr_parts[0], &jr_parts[1]);
        let joint_rand: Vec<Field128> = joint_rand_xof
            .into_seed_stream()
            .into_field_vec(mastic.joint_rand_len());

        let prove_rand = random_vector(mastic.prove_rand_len()).unwrap();
        let proof = mastic.prove(&beta, &prove_rand, &joint_rand).unwrap();

        // The driver splits with `par_iter`; on one thread a plain iterator does the same work.
        let proof_0 = proof
            .iter()
            .map(|_| Field128::from(rand::thread_rng().gen::<u128>()))
            .collect::<Vec<_>>();
        let proof_1 = proof
            .iter()
            .zip(proof_0.iter())
            .map(|(p_0, p_1)| p_0 - p_1)
            .collect::<Vec<_>>();

        (proof_0, proof_1)
    };

    let shares = [
        ReportShare::Mastic {
            nonce,
            vidpf_key: key_0,
            flp_proof_share: proof_0,
            flp_joint_rand_parts: jr_parts,
        },
        ReportShare::Mastic {
            nonce,
            vidpf_key: key_1,
            flp_proof_share: proof_1,
            flp_joint_rand_parts: jr_parts,
        },
    ];
    let t3 = Instant::now();

    Some((
        shares,
        Parts {
            encode: t1 - t0,
            vidpf: t2 - t1,
            flp: t3 - t2,
            total: t3 - t0,
        },
    ))
}

/// Whether the servers' own code (`KeyCollection`) accepts the report's FLP proof.
fn flp_accepts(mastic: &MasticHistogram, data_bits: usize, shares: &[ReportShare; 2]) -> bool {
    let verify_key = thread_rng().gen::<[u8; 16]>();
    let seed = PrgSeed { key: [1u8; 16] }; // as in server.rs
    let verifier_shares: Vec<Vec<Field128>> = shares
        .iter()
        .enumerate()
        .map(|(server_id, share)| {
            let mut coll = KeyCollection::new(
                mastic.clone(),
                server_id as i8,
                &seed,
                Some(data_bits),
                verify_key,
            );
            coll.add_report_share(share.clone());
            coll.tree_init();
            coll.run_flp_queries(0, 1).remove(0)
        })
        .collect();
    let verifier: Vec<Field128> = verifier_shares[0]
        .iter()
        .zip(&verifier_shares[1])
        .map(|(&a, &b)| a + b)
        .collect();
    mastic.decide(&verifier).unwrap()
}

fn mean_ms(durations: impl Iterator<Item = Duration>) -> f64 {
    let (sum, n) = durations.fold((0.0, 0usize), |(s, n), d| (s + d.as_secs_f64(), n + 1));
    sum / n as f64 * 1e3
}

/// Mean, median and p95 (nearest rank) in milliseconds.
fn summary_ms(durations: &[Duration]) -> (f64, f64, f64) {
    let mut ms: Vec<f64> = durations.iter().map(|d| d.as_secs_f64() * 1e3).collect();
    ms.sort_by(f64::total_cmp);
    let n = ms.len();
    let median = if n % 2 == 1 {
        ms[n / 2]
    } else {
        (ms[n / 2 - 1] + ms[n / 2]) / 2.0
    };
    let p95 = ms[((0.95 * n as f64).ceil() as usize).max(1) - 1];
    (ms.iter().sum::<f64>() / n as f64, median, p95)
}

/// Client times are fractions of a millisecond: keep 0.1 µs.
fn round_client_ms(ms: f64) -> f64 {
    (ms * 1e4).round() / 1e4
}

#[derive(Serialize)]
struct ClientLine {
    #[serde(flatten)]
    header: Header,
    client: ClientOut,
}

#[derive(Serialize)]
struct ClientOut {
    k: usize,
    keygen_ms_mean: f64,
    keygen_ms_median: f64,
    keygen_ms_p95: f64,
    report_bytes_per_server: [u64; 2],
    upload_bytes_per_client: u64,
}

fn main() -> io::Result<()> {
    let flags = App::new("keygen_bench")
        .about("Client micro-benchmark (bench/BENCHMARK_MASTIC.md, A6)")
        .arg(
            Arg::with_name("config")
                .short("c")
                .long("config")
                .value_name("FILENAME")
                .help("Driver config: encoding, data_bits, hist_buckets, threshold")
                .required(true)
                .takes_value(true),
        )
        .arg(
            Arg::with_name("csv")
                .long("csv")
                .value_name("FILENAME")
                .help("Rides to take the first K rows from")
                .default_value("bench/data/day_3of3.csv")
                .takes_value(true),
        )
        .arg(
            Arg::with_name("clients")
                .short("k")
                .long("clients")
                .value_name("NUMBER")
                .help("Number of clients (rows) to time")
                .default_value("2000")
                .takes_value(true),
        )
        .arg(
            Arg::with_name("out")
                .long("out")
                .value_name("FILENAME")
                .help("JSONL file to append the result line to")
                .default_value("bench/results/raw/mastic_client.jsonl")
                .takes_value(true),
        )
        .arg(
            Arg::with_name("bench_meta")
                .long("bench-meta")
                .value_name("JSON")
                .help("Labels for the result line, as for the driver's --bench-meta (default: day, 3of3, rep 0)")
                .takes_value(true),
        )
        .get_matches();

    // One thread, also for the library's internal use of rayon (A6).
    rayon::ThreadPoolBuilder::new()
        .num_threads(1)
        .build_global()
        .map_err(io::Error::other)?;

    let cfg = config::get_config(flags.value_of("config").unwrap());
    let dataset = cfg
        .dataset
        .as_ref()
        .ok_or_else(|| io::Error::other("the config needs a [dataset] with an encoding"))?;
    let data_bits = cfg
        .data_bits
        .ok_or_else(|| io::Error::other("the config needs data_bits"))?;
    let encoder = geo::encoder(&dataset.encoding);
    if encoder.depth() != data_bits {
        return Err(io::Error::other(format!(
            "data_bits = {data_bits} but {:?} produces {} bits",
            dataset.encoding,
            encoder.depth()
        )));
    }
    if dataset.weight.num_buckets() != cfg.hist_buckets {
        return Err(io::Error::other("hist_buckets does not match the weight"));
    }
    let mastic = Mastic::new_histogram(cfg.hist_buckets).unwrap();

    let csv = flags.value_of("csv").unwrap();
    let k: usize = flags
        .value_of("clients")
        .unwrap()
        .parse()
        .map_err(io::Error::other)?;
    let mut rides = dataset::load_rides(csv).map_err(|e| io::Error::other(e.to_string()))?;
    if rides.len() < k || k == 0 {
        return Err(io::Error::other(format!(
            "{csv} has {} rows, need {k}",
            rides.len()
        )));
    }
    rides.truncate(k);
    let meta = match flags.value_of("bench_meta") {
        Some(m) => BenchMeta::parse(m)?,
        None => BenchMeta {
            dataset: "day".into(),
            subset: "3of3".into(),
            rep: 0,
            warmup: false,
            timestamp: None,
            env: None,
        },
    };

    // Warm-up pass, untimed.
    let outside = rides
        .iter()
        .filter(|r| generate(&mastic, encoder.as_ref(), dataset, r).is_none())
        .count();
    if outside > 0 {
        return Err(io::Error::other(format!(
            "{outside} of the first {k} rides lie outside the encoded area"
        )));
    }

    // Timed pass. Sizes are taken outside the timed part.
    let mut parts = Vec::with_capacity(k);
    let mut wire = Vec::with_capacity(k);
    let mut first = None;
    for ride in &rides {
        let (shares, p) = generate(&mastic, encoder.as_ref(), dataset, ride).unwrap();
        parts.push(p);
        wire.push([wire_size(&shares[0]), wire_size(&shares[1])]);
        first.get_or_insert(shares);
    }
    let first = first.unwrap();

    let totals: Vec<Duration> = parts.iter().map(|p| p.total).collect();
    let (mean, median, p95) = summary_ms(&totals);
    if wire.iter().any(|w| *w != wire[0]) {
        println!("WARNING: report sizes differ between clients; reporting the first client's");
    }
    let report_bytes = wire[0];

    let (encoding, _) = mastic::bench::encoding_name_and_params(&dataset.encoding);
    let [ReportShare::Mastic {
        vidpf_key,
        flp_proof_share,
        ..
    }, _] = &first
    else {
        unreachable!("generate builds Mastic report shares")
    };
    let m = mastic.input_len();
    let b3_key = 33 + data_bits * (42 + 24 * (m + 1));
    let accepted = flp_accepts(&mastic, data_bits, &first);

    println!("keygen_bench: {encoding} (n = {data_bits}), first {k} rides of {csv}, 1 thread");
    println!("  per client (ms): mean {mean:.4}, median {median:.4}, p95 {p95:.4}");
    println!(
        "    means: encode {:.4} | VidpfKey::gen {:.4} | FLP joint rand + prove + share {:.4}",
        mean_ms(parts.iter().map(|p| p.encode)),
        mean_ms(parts.iter().map(|p| p.vidpf)),
        mean_ms(parts.iter().map(|p| p.flp)),
    );
    println!(
        "  report share per server: {} / {} B on the wire (bincode DefaultOptions), {} / {} B with bincode::serialize",
        report_bytes[0],
        report_bytes[1],
        fixint_size(&first[0]),
        fixint_size(&first[1]),
    );
    println!(
        "    VIDPF key: {} B wire, {} B bincode::serialize (B3 formula 33 + n(42 + 24(m+1)) with m = {m}: {b3_key} B)",
        wire_size(vidpf_key),
        fixint_size(vidpf_key),
    );
    println!(
        "    FLP proof share: {} field elements, {} B wire, {} B bincode::serialize",
        flp_proof_share.len(),
        wire_size(flp_proof_share),
        fixint_size(flp_proof_share),
    );
    println!(
        "  FLP check of the first report with the servers' code: {}",
        if accepted { "accepted" } else { "REJECTED" }
    );
    if !accepted {
        return Err(io::Error::other(
            "the generated report fails FLP verification",
        ));
    }

    let line = ClientLine {
        // The threshold does not apply to report generation.
        header: Header::new(&cfg, &meta, k, 0)?,
        client: ClientOut {
            k,
            keygen_ms_mean: round_client_ms(mean),
            keygen_ms_median: round_client_ms(median),
            keygen_ms_p95: round_client_ms(p95),
            report_bytes_per_server: report_bytes,
            upload_bytes_per_client: report_bytes[0] + report_bytes[1],
        },
    };
    let out = flags.value_of("out").unwrap();
    append_jsonl(out, &line)?;
    println!("Appended the result to {out}");

    Ok(())
}
