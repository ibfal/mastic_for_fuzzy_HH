use std::{
    collections::HashSet,
    io::{self, Error},
    time::{Duration, Instant, SystemTime},
};

use futures::try_join;
use mastic::{
    bench::{append_jsonl, round_ms, wire_size, BenchMeta, Header},
    bits_to_bitstring,
    collect::{self, ReportShare},
    config::{self, Mode},
    dataset,
    geo::{self, LocationEncoder},
    histogram_chunk_length,
    rpc::{
        AddReportSharesRequest, ApplyFLPResultsRequest, AttributeBasedMetricsResultRequest,
        AttributeBasedMetricsValidateRequest, FinalSharesRequest, GetProofsRequest,
        PlainMetricsResultRequest, PlainMetricsValidateRequest, ResetRequest, RunFlpQueriesRequest,
        TreeCrawlLastRequest, TreeCrawlRequest, TreeInitRequest, TreePruneRequest,
    },
    vec_add,
    vidpf::VidpfKey,
    CollectorClient, Mastic, MasticHistogram,
};
use prio::{
    codec::Encode,
    field::{random_vector, Field128},
    vdaf::{
        prio3::{Prio3, Prio3InputShare, Prio3PublicShare},
        xof::{IntoFieldVec, Xof, XofShake128},
        Client, Collector,
    },
};
use rand::{distributions::Distribution, thread_rng, Rng};
use rand_core::RngCore;
use rayon::prelude::*;
use serde::Serialize;
use tarpc::{client, context, serde_transport::tcp, tokio_serde::formats::Bincode};

fn long_context() -> context::Context {
    let mut ctx = context::current();

    // Increase timeout to one hour
    ctx.deadline = SystemTime::now() + Duration::from_secs(1000000);
    ctx
}

/// One candidate prefix of a crawl level, as the collector sees it after combining the shares.
#[derive(Serialize)]
struct ExportCandidate {
    bits: String,
    /// Number of clients under this prefix (the counter, the last element of `value`).
    count: u64,
    value: Vec<u64>,
    /// Whether the count reached the threshold, so the crawl expands this prefix.
    heavy: bool,
    /// Outline of the prefix as polygons of `[lat, lon]`; empty without a dataset.
    region: Vec<Vec<[f64; 2]>>,
}

#[derive(Serialize)]
struct ExportLevel {
    /// Prefix length of the candidates, 1 to `data_bits`.
    level: usize,
    seconds: f64,
    candidates: Vec<ExportCandidate>,
}

/// Records every crawl level for `--export` and `--bench-out`. The servers expand each surviving
/// prefix into its two children in order and prune in place, so the driver can follow the
/// candidates' paths.
struct CrawlExport {
    encoder: Option<Box<dyn LocationEncoder>>,
    /// Whether to compute each candidate's outline. Only `--export` writes them, and they are
    /// slow to compute, so `--bench-out` alone leaves them out.
    regions: bool,
    frontier: Vec<Vec<bool>>,
    levels: Vec<ExportLevel>,
}

/// A full-length prefix that stayed heavy, with its cell's label and centre.
struct HeavyHitter<'a> {
    candidate: &'a ExportCandidate,
    label: Option<String>,
    center: Option<(f64, f64)>,
}

impl CrawlExport {
    fn new(cfg: &config::Config, regions: bool) -> Self {
        Self {
            encoder: cfg.dataset.as_ref().map(|d| geo::encoder(&d.encoding)),
            regions,
            frontier: vec![vec![]],
            levels: Vec::new(),
        }
    }

    /// The candidates of the last level that reached the threshold.
    fn heavy_hitters(&self) -> Vec<HeavyHitter<'_>> {
        let Some(last) = self.levels.last() else {
            return Vec::new();
        };
        last.candidates
            .iter()
            .filter(|c| c.heavy)
            .map(|c| {
                let bits: Vec<bool> = c.bits.chars().map(|b| b == '1').collect();
                let (label, center) = match &self.encoder {
                    Some(e) => (Some(e.label(&bits)), Some(e.decode(&bits))),
                    None => (None, None),
                };
                HeavyHitter {
                    candidate: c,
                    label,
                    center,
                }
            })
            .collect()
    }

    fn record(
        &mut self,
        input_len: usize,
        cnt_values_0: &[Vec<Field128>],
        cnt_values_1: &[Vec<Field128>],
        keep: &[bool],
    ) {
        let paths: Vec<Vec<bool>> = self
            .frontier
            .iter()
            .flat_map(|p| [false, true].map(|b| [&p[..], &[b]].concat()))
            .collect();
        assert_eq!(paths.len(), cnt_values_0.len());

        let candidates = paths
            .iter()
            .zip(cnt_values_0.iter().zip(cnt_values_1))
            .zip(keep)
            .map(|((path, (v0, v1)), &heavy)| {
                let value: Vec<u64> = v0
                    .iter()
                    .zip(v1)
                    .map(|(&a, &b)| u128::from(a + b) as u64)
                    .collect();
                let region = match &self.encoder {
                    Some(e) if self.regions => e
                        .region(path)
                        .into_iter()
                        .map(|ring| ring.into_iter().map(|(lat, lon)| [lat, lon]).collect())
                        .collect(),
                    _ => Vec::new(),
                };
                ExportCandidate {
                    bits: bits_to_bitstring(path),
                    count: value[input_len],
                    value,
                    heavy,
                    region,
                }
            })
            .collect();

        self.levels.push(ExportLevel {
            level: paths[0].len(),
            seconds: 0.0,
            candidates,
        });
        self.frontier = paths
            .into_iter()
            .zip(keep)
            .filter_map(|(p, &k)| k.then_some(p))
            .collect();
    }
}

/// Which part of a run an RPC belongs to (bench/BENCHMARK_MASTIC.md, A3).
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Phase {
    /// `reset`, before the first report is sent: neither upload nor online.
    Setup,
    Upload,
    Online,
}

/// Totals for one RPC method, over both servers.
struct RpcStat {
    method: &'static str,
    phase: Phase,
    /// Calls sent to both servers at once (`try_join!`); each one is a round (B3).
    rounds: u64,
    /// Merkle binary-search re-sends of `tree_crawl` (B3: `rounds.extra`).
    extra_rounds: u64,
    request_bytes: [u64; 2],
    response_bytes: [u64; 2],
}

/// Message sizes of one call to both servers, measured where `Bench` is not at hand.
struct CallSizes {
    request: [u64; 2],
    response: [u64; 2],
    /// Time spent measuring them.
    overhead: Duration,
}

#[derive(Clone, Copy)]
struct Stopwatch {
    start: Instant,
    overhead: Duration,
}

/// Measurements for `--bench-out` (A9). Time spent measuring (message sizes, following the
/// crawl) is added to `overhead` and subtracted from every timing.
struct Bench {
    meta: BenchMeta,
    overhead: Duration,
    rpcs: Vec<RpcStat>,
    upload_ms: f64,
    online: Option<Stopwatch>,
    online_ms: f64,
    /// Online steps (B2): `flp`, `level_0` ... `level_{n-2}`, `last_level`, `final`.
    steps: Vec<(String, f64)>,
    /// `tree_crawl` calls per level (more than one only after a Merkle re-send), then 1 for
    /// `tree_crawl_last`.
    crawls_per_level: Vec<u64>,
    /// Per-client accept/reject results the driver learns: FLP decisions and final proof checks.
    client_validity_bits: u64,
    /// Merkle roots compared in `tree_crawl` (each covers a block of clients, all of them when
    /// nobody cheats).
    merkle_root_checks: u64,
    /// `(bits, count)` of the reconstructed final shares, to cross-check the output.
    final_cells: Vec<(String, u64)>,
}

impl Bench {
    fn new(meta: BenchMeta) -> Self {
        Self {
            meta,
            overhead: Duration::ZERO,
            rpcs: Vec::new(),
            upload_ms: 0.0,
            online: None,
            online_ms: 0.0,
            steps: Vec::new(),
            crawls_per_level: Vec::new(),
            client_validity_bits: 0,
            merkle_root_checks: 0,
            final_cells: Vec::new(),
        }
    }

    fn stopwatch(&self) -> Stopwatch {
        Stopwatch {
            start: Instant::now(),
            overhead: self.overhead,
        }
    }

    /// Wall-clock milliseconds since `sw`, minus the time spent measuring in between.
    fn elapsed_ms(&self, sw: Stopwatch) -> f64 {
        let measuring = self.overhead.saturating_sub(sw.overhead);
        sw.start.elapsed().saturating_sub(measuring).as_secs_f64() * 1e3
    }

    fn step(&mut self, name: impl Into<String>, sw: Stopwatch) {
        let ms = self.elapsed_ms(sw);
        self.steps.push((name.into(), ms));
    }

    /// Run `f` and count its time as measuring overhead.
    fn measure<R>(&mut self, f: impl FnOnce() -> R) -> R {
        let t = Instant::now();
        let r = f();
        self.overhead += t.elapsed();
        r
    }

    fn add(
        &mut self,
        method: &'static str,
        phase: Phase,
        extra: bool,
        request: [u64; 2],
        response: [u64; 2],
    ) {
        let i = match self.rpcs.iter().position(|s| s.method == method) {
            Some(i) => i,
            None => {
                self.rpcs.push(RpcStat {
                    method,
                    phase,
                    rounds: 0,
                    extra_rounds: 0,
                    request_bytes: [0; 2],
                    response_bytes: [0; 2],
                });
                self.rpcs.len() - 1
            }
        };
        let stat = &mut self.rpcs[i];
        debug_assert_eq!(stat.phase, phase);
        if extra {
            stat.extra_rounds += 1;
        } else {
            stat.rounds += 1;
        }
        for s in 0..2 {
            stat.request_bytes[s] += request[s];
            stat.response_bytes[s] += response[s];
        }
    }

    /// Sum over the RPCs of one phase, per server.
    fn sum(&self, phase: Phase, field: impl Fn(&RpcStat) -> [u64; 2]) -> [u64; 2] {
        self.rpcs
            .iter()
            .filter(|s| s.phase == phase)
            .fold([0, 0], |acc, s| {
                let v = field(s);
                [acc[0] + v[0], acc[1] + v[1]]
            })
    }

    fn rounds(&self, phase: Phase) -> u64 {
        self.rpcs
            .iter()
            .filter(|s| s.phase == phase)
            .map(|s| s.rounds)
            .sum()
    }

    fn result_line(
        &self,
        cfg: &config::Config,
        num_clients: usize,
        export: &CrawlExport,
    ) -> io::Result<ResultLine> {
        let header = Header::new(
            cfg,
            &self.meta,
            num_clients,
            threshold_abs(cfg, num_clients),
        )?;

        // Every crawl level reveals the count of each candidate (both children of every
        // surviving prefix), once per `tree_crawl` call.
        if export.levels.len() != self.crawls_per_level.len() {
            return Err(Error::other(format!(
                "recorded {} crawl levels but counted {} crawls",
                export.levels.len(),
                self.crawls_per_level.len()
            )));
        }
        let revealed: Vec<u64> = export
            .levels
            .iter()
            .zip(&self.crawls_per_level)
            .map(|(l, &crawls)| l.candidates.len() as u64 * crawls)
            .collect();
        let frontier: Vec<u64> = export
            .levels
            .iter()
            .map(|l| l.candidates.iter().filter(|c| c.heavy).count() as u64)
            .collect();

        // The output is what the last level kept; it must equal the reconstructed final shares.
        let heavy_hitters = export.heavy_hitters();
        let mut from_crawl: Vec<(String, u64)> = heavy_hitters
            .iter()
            .map(|h| (h.candidate.bits.clone(), h.candidate.count))
            .collect();
        let mut from_final = self.final_cells.clone();
        from_crawl.sort();
        from_final.sort();
        if from_crawl != from_final {
            return Err(Error::other(format!(
                "heavy hitters of the last level {from_crawl:?} differ from the final shares \
                 {from_final:?}"
            )));
        }
        let mut output: Vec<OutputCell> = heavy_hitters
            .iter()
            .map(|h| {
                let (lat, lon) = h.center.expect("--bench-out runs on a dataset");
                OutputCell {
                    label: h.label.clone(),
                    lat,
                    lon,
                    count: h.candidate.count,
                }
            })
            .collect();
        output.sort_by(|a, b| b.count.cmp(&a.count).then_with(|| a.label.cmp(&b.label)));

        let extra: u64 = self.rpcs.iter().map(|s| s.extra_rounds).sum();
        Ok(ResultLine {
            header,
            phases: PhasesOut {
                // Mastic has no input-independent server preprocessing (B2).
                server_offline_ms: 0.0,
                upload_ms: round_ms(self.upload_ms),
                online_ms: round_ms(self.online_ms),
                online_steps: self
                    .steps
                    .iter()
                    .map(|(name, ms)| StepOut {
                        name: name.clone(),
                        ms: round_ms(*ms),
                    })
                    .collect(),
            },
            rounds: RoundsOut {
                upload: self.rounds(Phase::Upload),
                online: self.rounds(Phase::Online),
                extra,
            },
            bytes: BytesOut {
                upload: UploadBytes {
                    client_to_server: self.sum(Phase::Upload, |s| s.request_bytes),
                },
                online: OnlineBytes {
                    collector_to_server: self.sum(Phase::Online, |s| s.request_bytes),
                    server_to_collector: self.sum(Phase::Online, |s| s.response_bytes),
                    // The servers never talk to each other; the driver relays everything (B3).
                    server_to_server: 0,
                },
            },
            leakage: LeakageOut {
                revealed_aggregates_total: revealed.iter().sum(),
                revealed_aggregates_per_level: revealed,
                revealed_kind: "count",
                client_validity_bits: self.client_validity_bits,
                frontier_per_level: frontier,
            },
            output,
        })
    }

    fn print_summary(&self) {
        println!("Benchmark measurements (bytes: bincode DefaultOptions, as tarpc sends them):");
        println!(
            "  {:<7} {:<18} {:>6} {:>5} {:>24} {:>24}",
            "phase", "rpc", "rounds", "extra", "to server 0 / 1", "from server 0 / 1"
        );
        for s in &self.rpcs {
            println!(
                "  {:<7} {:<18} {:>6} {:>5} {:>24} {:>24}",
                format!("{:?}", s.phase).to_lowercase(),
                s.method,
                s.rounds,
                s.extra_rounds,
                format!("{} / {}", s.request_bytes[0], s.request_bytes[1]),
                format!("{} / {}", s.response_bytes[0], s.response_bytes[1]),
            );
        }
        let extra: u64 = self.rpcs.iter().map(|s| s.extra_rounds).sum();
        println!(
            "  rounds: setup {}, upload {}, online {}, extra {}",
            self.rounds(Phase::Setup),
            self.rounds(Phase::Upload),
            self.rounds(Phase::Online),
            extra
        );
        let steps_ms: f64 = self.steps.iter().map(|(_, ms)| ms).sum();
        println!(
            "  upload {:.1} ms, online {:.1} ms (steps sum to {:.1} ms), measuring overhead {:.1} ms (excluded)",
            self.upload_ms,
            self.online_ms,
            steps_ms,
            self.overhead.as_secs_f64() * 1e3
        );
        println!(
            "  client validity bits {}, Merkle root comparisons {}",
            self.client_validity_bits, self.merkle_root_checks
        );
    }
}

/// Request size per server, when measuring; the same request goes to both servers.
fn request_sizes<T: Serialize>(bench: &mut Option<Bench>, req: &T) -> [u64; 2] {
    bench.as_mut().map_or([0, 0], |b| {
        let size = b.measure(|| wire_size(req));
        [size, size]
    })
}

/// Count one call to both servers and the sizes of its messages, when measuring.
fn record<A: Serialize, B: Serialize>(
    bench: &mut Option<Bench>,
    method: &'static str,
    phase: Phase,
    extra: bool,
    request: [u64; 2],
    resp_0: &A,
    resp_1: &B,
) {
    if let Some(b) = bench {
        let response = b.measure(|| [wire_size(resp_0), wire_size(resp_1)]);
        b.add(method, phase, extra, request, response);
    }
}

/// The run's absolute threshold, as the crawl computes it: max(1, floor(fraction * clients)).
fn threshold_abs(cfg: &config::Config, num_clients: usize) -> u64 {
    match cfg.mode {
        Mode::WeightedHeavyHitters { threshold } => {
            core::cmp::max(1, (threshold * (num_clients as f64)) as u64)
        }
        _ => 0,
    }
}

/// One line of `--bench-out`, in the order of A9.
#[derive(Serialize)]
struct ResultLine {
    #[serde(flatten)]
    header: Header,
    phases: PhasesOut,
    rounds: RoundsOut,
    bytes: BytesOut,
    leakage: LeakageOut,
    output: Vec<OutputCell>,
}

#[derive(Serialize)]
struct PhasesOut {
    server_offline_ms: f64,
    upload_ms: f64,
    online_ms: f64,
    online_steps: Vec<StepOut>,
}

#[derive(Serialize)]
struct StepOut {
    name: String,
    ms: f64,
}

#[derive(Serialize)]
struct RoundsOut {
    upload: u64,
    online: u64,
    extra: u64,
}

#[derive(Serialize)]
struct BytesOut {
    upload: UploadBytes,
    online: OnlineBytes,
}

#[derive(Serialize)]
struct UploadBytes {
    client_to_server: [u64; 2],
}

#[derive(Serialize)]
struct OnlineBytes {
    collector_to_server: [u64; 2],
    server_to_collector: [u64; 2],
    server_to_server: u64,
}

#[derive(Serialize)]
struct LeakageOut {
    revealed_aggregates_total: u64,
    revealed_aggregates_per_level: Vec<u64>,
    revealed_kind: &'static str,
    client_validity_bits: u64,
    frontier_per_level: Vec<u64>,
}

#[derive(Serialize)]
struct OutputCell {
    label: Option<String>,
    lat: f64,
    lon: f64,
    count: u64,
}

fn sample_bits(len: usize) -> Vec<bool> {
    let mut rng = rand::thread_rng();
    (0..len).map(|_| rng.gen::<bool>()).collect()
}

enum PlaintextReport {
    /// A plaintext report for Mastic. This report type is used for weighted heavy hitters and
    /// attribute-based metrics.
    Mastic {
        nonce: [u8; 16],
        vidpf_keys: [VidpfKey; 2],
        flp_proof_shares: [Vec<Field128>; 2],
        flp_joint_rand_parts: [[u8; 16]; 2],
        alpha: Vec<bool>,
    },

    /// A plaintext report for Prio3. This report type is used for plain (non-attribute-based)
    /// metrics.
    Prio3 {
        nonce: [u8; 16],
        public_share: Prio3PublicShare<16>,
        input_shares: [Prio3InputShare<Field128, 16>; 2],
    },
}

impl PlaintextReport {
    /// Panics unless the type is `Mastic`.
    fn unwrap_alpha(&self) -> Vec<bool> {
        match self {
            Self::Mastic {
                nonce: _,
                vidpf_keys: _,
                flp_proof_shares: _,
                flp_joint_rand_parts: _,
                alpha,
            } => alpha.clone(),
            Self::Prio3 { .. } => panic!("Prio3 reports don't have an alpha"),
        }
    }

    fn to_shares(&self) -> [ReportShare; 2] {
        match self {
            Self::Mastic {
                nonce,
                vidpf_keys,
                flp_proof_shares,
                flp_joint_rand_parts,
                ..
            } => [
                ReportShare::Mastic {
                    nonce: *nonce,
                    vidpf_key: vidpf_keys[0].clone(),
                    flp_proof_share: flp_proof_shares[0].clone(),
                    flp_joint_rand_parts: *flp_joint_rand_parts,
                },
                ReportShare::Mastic {
                    nonce: *nonce,
                    vidpf_key: vidpf_keys[1].clone(),
                    flp_proof_share: flp_proof_shares[1].clone(),
                    flp_joint_rand_parts: *flp_joint_rand_parts,
                },
            ],
            Self::Prio3 {
                nonce,
                public_share,
                input_shares,
            } => [
                ReportShare::Prio3 {
                    nonce: *nonce,
                    public_share_bytes: public_share.get_encoded(),
                    input_share_bytes: input_shares[0].get_encoded(),
                },
                ReportShare::Prio3 {
                    nonce: *nonce,
                    public_share_bytes: public_share.get_encoded(),
                    input_share_bytes: input_shares[1].get_encoded(),
                },
            ],
        }
    }
}

/// Synthesize `zipf_unique_buckets` random `(alpha, bucket)` inputs. Clients are later drawn from
/// them by a Zipf distribution.
fn synthetic_inputs(cfg: &config::Config) -> Vec<(Vec<bool>, usize)> {
    assert!(cfg.zipf_unique_buckets > 0);

    let mut rng = thread_rng();
    (0..cfg.zipf_unique_buckets)
        .map(|_| {
            (
                sample_bits(cfg.data_bits.unwrap_or(0)),
                rng.gen_range(0..cfg.hist_buckets),
            )
        })
        .collect()
}

fn generate_reports(
    cfg: &config::Config,
    mastic: &MasticHistogram,
    inputs: Vec<(Vec<bool>, usize)>,
) -> Vec<PlaintextReport> {
    assert!(!inputs.is_empty());

    let reports = inputs
        .into_par_iter()
        .map(|(alpha, bucket)| {
            let mut rng = thread_rng();
            let nonce = rng.gen::<[u8; 16]>();

            match cfg.mode {
                Mode::WeightedHeavyHitters { .. } | Mode::AttributeBasedMetrics { .. } => {
                    let beta = mastic.encode_measurement(&bucket).unwrap();

                    let (key_0, key_1) = VidpfKey::gen(&alpha, &beta);

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

                        let proof_0 = proof
                            .iter()
                            .map(|_| Field128::from(rand::thread_rng().gen::<u128>()))
                            .collect::<Vec<_>>();
                        let proof_1 = proof
                            .par_iter()
                            .zip(proof_0.par_iter())
                            .map(|(p_0, p_1)| p_0 - p_1)
                            .collect::<Vec<_>>();

                        (proof_0, proof_1)
                    };

                    PlaintextReport::Mastic {
                        nonce,
                        vidpf_keys: [key_0, key_1],
                        flp_proof_shares: [proof_0, proof_1],
                        flp_joint_rand_parts: jr_parts,
                        alpha,
                    }
                }
                Mode::PlainMetrics => {
                    let chunk_length =
                        histogram_chunk_length(mastic.input_len(), Mode::PlainMetrics);
                    let prio3 = Prio3::new_histogram(2, mastic.input_len(), chunk_length).unwrap();
                    let (public_share, input_shares) = prio3.shard(&bucket, &nonce).unwrap();

                    PlaintextReport::Prio3 {
                        nonce,
                        public_share,
                        input_shares: input_shares.try_into().unwrap(),
                    }
                }
            }
        })
        .collect::<Vec<_>>();

    match &reports[0] {
        PlaintextReport::Mastic {
            nonce,
            vidpf_keys,
            flp_proof_shares,
            flp_joint_rand_parts,
            ..
        } => {
            let encoded: Vec<u8> = bincode::serialize(nonce).unwrap();
            println!("\t- Nonce size: {:?} bytes", encoded.len());

            let encoded: Vec<u8> = bincode::serialize(&flp_joint_rand_parts[0]).unwrap();
            println!("\t- JR size: {:?} bytes", encoded.len());

            let encoded: Vec<u8> = bincode::serialize(&vidpf_keys[0]).unwrap();
            println!("\t- VIDPFKey size: {:?} bytes", encoded.len());

            let encoded: Vec<u8> = bincode::serialize(&flp_proof_shares[0]).unwrap();
            println!("\t- FLP proof size: {:?} bytes", encoded.len());
        }
        r @ PlaintextReport::Prio3 { .. } => {
            let [report_share_0, report_share_1] = r.to_shares();

            let encoded: Vec<u8> = bincode::serialize(&report_share_0).unwrap();
            println!("\t- leader report share size: {:?} bytes", encoded.len());

            let encoded: Vec<u8> = bincode::serialize(&report_share_1).unwrap();
            println!("\t- helper report share size: {:?} bytes", encoded.len());
        }
    }

    reports
}

async fn reset_servers(
    cfg: &config::Config,
    client_0: &CollectorClient,
    client_1: &CollectorClient,
    verify_key: &[u8; 16],
    bench: &mut Option<Bench>,
) -> io::Result<()> {
    let req = ResetRequest {
        verify_key: *verify_key,
        hist_buckets: cfg.hist_buckets,
    };
    let request = request_sizes(bench, &req);
    let resp_0 = client_0.reset(long_context(), req.clone());
    let resp_1 = client_1.reset(long_context(), req);
    let (out_0, out_1) = try_join!(resp_0, resp_1).unwrap();
    record(bench, "reset", Phase::Setup, false, request, &out_0, &out_1);

    Ok(())
}

async fn tree_init(
    client_0: &CollectorClient,
    client_1: &CollectorClient,
    bench: &mut Option<Bench>,
) -> io::Result<()> {
    let req = TreeInitRequest {};
    let request = request_sizes(bench, &req);
    let resp_0 = client_0.tree_init(long_context(), req.clone());
    let resp_1 = client_1.tree_init(long_context(), req);
    let (out_0, out_1) = try_join!(resp_0, resp_1).unwrap();
    record(
        bench,
        "tree_init",
        Phase::Online,
        false,
        request,
        &out_0,
        &out_1,
    );

    Ok(())
}

async fn add_reports(
    cfg: &config::Config,
    clients: (&CollectorClient, &CollectorClient),
    all_reports: &[PlaintextReport],
    first: Option<usize>,
    num_clients: usize,
    malicious_percentage: f32,
    measure: bool,
) -> io::Result<Option<CallSizes>> {
    let mut rng = rand::thread_rng();
    let zipf = zipf::ZipfDistribution::new(cfg.zipf_unique_buckets, cfg.zipf_exponent).unwrap();

    let mut report_shares_0 = Vec::with_capacity(num_clients);
    let mut report_shares_1 = Vec::with_capacity(num_clients);
    for r in 0..num_clients {
        // Replay the dataset in order, or draw synthetic clients from the Zipf distribution.
        let idx_1 = match first {
            Some(first) => first + r,
            None => zipf.sample(&mut rng) - 1,
        };
        let [report_share_0, mut report_share_1] = all_reports[idx_1].to_shares();

        if rng.gen_range(0.0..1.0) < malicious_percentage {
            match report_share_1 {
                ReportShare::Mastic {
                    nonce: _,
                    ref mut vidpf_key,
                    ref mut flp_proof_share,
                    flp_joint_rand_parts: _,
                } => {
                    if rng.gen() {
                        // Malicious key. Tweaking the root seed is sufficient to cause VIDPF
                        // verification to fail.
                        vidpf_key.get_root_seed().key[0] ^= 1;
                    } else {
                        // Malicious FLP. Tweaking the proof is sufficient to cause FLP verification to
                        // fail.
                        flp_proof_share[0] += Field128::from(1337);
                    }
                }
                ReportShare::Prio3 {
                    nonce: _,
                    public_share_bytes: _,
                    ref mut input_share_bytes,
                } => {
                    // Tweaking the input share is sufficient to cause FLP verification to fail.
                    input_share_bytes[0] ^= 1;
                }
            }
            println!("Malicious {}", r);
        }

        report_shares_0.push(report_share_0);
        report_shares_1.push(report_share_1);
    }

    let req_0 = AddReportSharesRequest {
        report_shares: report_shares_0,
    };
    let req_1 = AddReportSharesRequest {
        report_shares: report_shares_1,
    };
    let t = Instant::now();
    let request = measure.then(|| [wire_size(&req_0), wire_size(&req_1)]);
    let mut overhead = t.elapsed();

    let resp_0 = clients.0.add_report_shares(long_context(), req_0);
    let resp_1 = clients.1.add_report_shares(long_context(), req_1);

    let (out_0, out_1) = try_join!(resp_0, resp_1).unwrap();

    Ok(request.map(|request| {
        let t = Instant::now();
        let response = [wire_size(&out_0), wire_size(&out_1)];
        overhead += t.elapsed();
        CallSizes {
            request,
            response,
            overhead,
        }
    }))
}

async fn run_flp_queries(
    cfg: &config::Config,
    mastic: &MasticHistogram,
    client_0: &CollectorClient,
    client_1: &CollectorClient,
    num_clients: usize,
    bench: &mut Option<Bench>,
) -> io::Result<()> {
    // Receive FLP query responses in chunks of cfg.query_flp_batch_size to avoid having huge RPC messages.
    let mut keep = vec![];
    let mut start = 0;
    while start < num_clients {
        let end = std::cmp::min(num_clients, start + cfg.query_flp_batch_size);

        let req = RunFlpQueriesRequest { start, end };
        let request = request_sizes(bench, &req);
        let resp_0 = client_0.run_flp_queries(long_context(), req.clone());
        let resp_1 = client_1.run_flp_queries(long_context(), req);
        let (flp_verifier_shares_0, flp_verifier_shares_1) = try_join!(resp_0, resp_1).unwrap();
        record(
            bench,
            "run_flp_queries",
            Phase::Online,
            false,
            request,
            &flp_verifier_shares_0,
            &flp_verifier_shares_1,
        );
        debug_assert_eq!(flp_verifier_shares_0.len(), flp_verifier_shares_1.len());

        keep.extend(
            flp_verifier_shares_0
                .par_iter()
                .zip(flp_verifier_shares_1.par_iter())
                .map(|(flp_verifier_share_0, flp_verifier_share_1)| {
                    let flp_verifier = flp_verifier_share_0
                        .par_iter()
                        .zip(flp_verifier_share_1.par_iter())
                        .map(|(&v1, &v2)| v1 + v2)
                        .collect::<Vec<_>>();

                    mastic.decide(&flp_verifier).unwrap()
                })
                .collect::<Vec<_>>(),
        );

        start += cfg.query_flp_batch_size;
    }
    if let Some(b) = bench {
        // One accept/reject decision per client.
        b.client_validity_bits += keep.len() as u64;
    }

    // Tree prune
    let req = ApplyFLPResultsRequest { keep };
    let request = request_sizes(bench, &req);
    let resp_0 = client_0.apply_flp_results(long_context(), req.clone());
    let resp_1 = client_1.apply_flp_results(long_context(), req);
    let (out_0, out_1) = try_join!(resp_0, resp_1).unwrap();
    record(
        bench,
        "apply_flp_results",
        Phase::Online,
        false,
        request,
        &out_0,
        &out_1,
    );

    Ok(())
}

async fn run_level(
    cfg: &config::Config,
    mastic: &MasticHistogram,
    client_0: &CollectorClient,
    client_1: &CollectorClient,
    num_clients: usize,
    export: &mut Option<CrawlExport>,
    bench: &mut Option<Bench>,
) -> io::Result<()> {
    let threshold = if let Mode::WeightedHeavyHitters { threshold } = cfg.mode {
        core::cmp::max(1, (threshold * (num_clients as f64)) as u64)
    } else {
        return Err(Error::other(
            "invoked server in an unexpected mode of operation",
        ));
    };
    if let Some(b) = bench {
        b.crawls_per_level.push(0);
    }
    let mut keep;
    let mut cnt_values;
    let mut split = 1usize;
    let mut malicious = Vec::<usize>::new();
    let mut is_last = false;
    // Whether this crawl re-sends the level for the Merkle binary search (B3: an extra round).
    let mut resend = false;
    loop {
        let ml = malicious.clone();
        let req = TreeCrawlRequest {
            split_by: split,
            malicious: ml,
            is_last,
        };

        let request = request_sizes(bench, &req);
        let resp_0 = client_0.tree_crawl(long_context(), req.clone());
        let resp_1 = client_1.tree_crawl(long_context(), req);
        let (out_0, out_1) = try_join!(resp_0, resp_1).unwrap();
        record(
            bench,
            "tree_crawl",
            Phase::Online,
            resend,
            request,
            &out_0,
            &out_1,
        );
        let ((cnt_values_0, mt_root_0, indices_0), (cnt_values_1, mt_root_1, indices_1)) =
            (out_0, out_1);
        if let Some(b) = bench {
            *b.crawls_per_level.last_mut().unwrap() += 1;
            b.merkle_root_checks += mt_root_0.len() as u64;
        }

        assert_eq!(cnt_values_0.len(), cnt_values_1.len());
        keep = collect::KeyCollection::keep_values(
            mastic.input_len(),
            threshold,
            &cnt_values_0,
            &cnt_values_1,
        );
        cnt_values = (cnt_values_0, cnt_values_1);
        if mt_root_0.is_empty() {
            break;
        }

        malicious = Vec::new();
        for i in 0..mt_root_0.len() {
            if mt_root_0[i] != mt_root_1[i] {
                assert_eq!(indices_0[i], indices_1[i]);
                malicious.push(indices_0[i]);
                // println!("{}) different {} vs {}", i, hl0, hl1);
            }
        }
        if malicious.is_empty() {
            break;
        } else {
            println!(
                "Detected malicious {:?} out of {} clients",
                malicious, num_clients
            );
            if split >= num_clients {
                if is_last {
                    break;
                } else {
                    is_last = true;
                }
            } else {
                split *= 2;
            }
            resend = true;
        }
    }

    if let Some(export) = export {
        let t = Instant::now();
        export.record(mastic.input_len(), &cnt_values.0, &cnt_values.1, &keep);
        if let Some(b) = bench {
            b.overhead += t.elapsed();
        }
    }

    // Tree prune
    let req = TreePruneRequest { keep };
    let request = request_sizes(bench, &req);
    let resp_0 = client_0.tree_prune(long_context(), req.clone());
    let resp_1 = client_1.tree_prune(long_context(), req);
    let (out_0, out_1) = try_join!(resp_0, resp_1).unwrap();
    record(
        bench,
        "tree_prune",
        Phase::Online,
        false,
        request,
        &out_0,
        &out_1,
    );

    Ok(())
}

async fn run_level_last(
    cfg: &config::Config,
    mastic: &MasticHistogram,
    client_0: &CollectorClient,
    client_1: &CollectorClient,
    num_clients: usize,
    export: &mut Option<CrawlExport>,
    bench: &mut Option<Bench>,
) -> io::Result<()> {
    let start_last = Instant::now();
    let step = bench.as_ref().map(Bench::stopwatch);
    let threshold = if let Mode::WeightedHeavyHitters { threshold } = cfg.mode {
        core::cmp::max(1, (threshold * (num_clients as f64)) as u64)
    } else {
        return Err(Error::other(
            "invoked server in an unexpected mode of operation",
        ));
    };

    let req = TreeCrawlLastRequest {};
    let request = request_sizes(bench, &req);
    let resp_0 = client_0.tree_crawl_last(long_context(), req.clone());
    let resp_1 = client_1.tree_crawl_last(long_context(), req);
    let (cnt_values_0, cnt_values_1) = try_join!(resp_0, resp_1).unwrap();
    record(
        bench,
        "tree_crawl_last",
        Phase::Online,
        false,
        request,
        &cnt_values_0,
        &cnt_values_1,
    );
    if let Some(b) = bench {
        b.crawls_per_level.push(1);
    }
    assert_eq!(cnt_values_0.len(), cnt_values_1.len());
    let keep = collect::KeyCollection::keep_values(
        mastic.input_len(),
        threshold,
        &cnt_values_0,
        &cnt_values_1,
    );
    if let Some(export) = export {
        let t = Instant::now();
        export.record(mastic.input_len(), &cnt_values_0, &cnt_values_1, &keep);
        if let Some(b) = bench {
            b.overhead += t.elapsed();
        }
    }

    // Receive counters in chunks to avoid having huge RPC messages.
    let mut start = 0;
    while start < num_clients {
        let end = std::cmp::min(num_clients, start + cfg.query_flp_batch_size);

        let req = GetProofsRequest { start, end };
        let request = request_sizes(bench, &req);
        let resp_0 = client_0.get_proofs(long_context(), req.clone());
        let resp_1 = client_1.get_proofs(long_context(), req);
        let (hashes_0, hashes_1) = try_join!(resp_0, resp_1).unwrap();
        record(
            bench,
            "get_proofs",
            Phase::Online,
            false,
            request,
            &hashes_0,
            &hashes_1,
        );
        if let Some(b) = bench {
            // One proof comparison per client.
            b.client_validity_bits += hashes_0.len() as u64;
        }

        assert_eq!(hashes_0.len(), hashes_1.len());

        let verified = hashes_0
            .par_iter()
            .zip(hashes_1.par_iter())
            .all(|(&h0, &h1)| h0 == h1);
        assert!(verified);

        start += cfg.query_flp_batch_size;
    }

    // Tree prune
    let req = TreePruneRequest { keep };
    let request = request_sizes(bench, &req);
    let resp_0 = client_0.tree_prune(long_context(), req.clone());
    let resp_1 = client_1.tree_prune(long_context(), req);
    let (out_0, out_1) = try_join!(resp_0, resp_1).unwrap();
    record(
        bench,
        "tree_prune",
        Phase::Online,
        false,
        request,
        &out_0,
        &out_1,
    );
    if let (Some(b), Some(sw)) = (bench.as_mut(), step) {
        b.step("last_level", sw);
    }

    let step = bench.as_ref().map(Bench::stopwatch);
    let req = FinalSharesRequest {};
    let request = request_sizes(bench, &req);
    let resp_0 = client_0.final_shares(long_context(), req.clone());
    let resp_1 = client_1.final_shares(long_context(), req);
    let (shares_0, shares_1) = try_join!(resp_0, resp_1).unwrap();
    record(
        bench,
        "final_shares",
        Phase::Online,
        false,
        request,
        &shares_0,
        &shares_1,
    );
    println!(
        "- Time for level {}: {:?}\n",
        cfg.data_bits.unwrap(),
        start_last.elapsed().as_secs_f64()
    );
    let final_values =
        collect::KeyCollection::final_values(mastic.input_len(), &shares_0, &shares_1);
    if let (Some(b), Some(sw)) = (bench.as_mut(), step) {
        // The collector now holds the output: the online phase ends here (A3).
        b.step("final", sw);
        b.online_ms = b.elapsed_ms(b.online.expect("online phase was started"));
        b.final_cells = final_values
            .iter()
            .map(|r| {
                let count = u128::from(r.value[mastic.input_len()]) as u64;
                (bits_to_bitstring(&r.path), count)
            })
            .collect();
    }
    let encoder = cfg.dataset.as_ref().map(|d| geo::encoder(&d.encoding));
    for res in &final_values {
        let bits = mastic::bits_to_bitstring(&res.path);
        if res.value[mastic.input_len() - 1] > Field128::from(0) {
            match &encoder {
                Some(encoder) => {
                    let (lat, lon) = encoder.decode(&res.path);
                    println!(
                        "Value ({}) \t Cell: {} \t Location: ({:.4}, {:.4}) \t Count: {:?}",
                        bits,
                        encoder.label(&res.path),
                        lat,
                        lon,
                        res.value
                    );
                }
                None => println!("Value ({}) \t Count: {:?}", bits, res.value),
            }
        }
    }

    Ok(())
}

async fn run_attribute_based_metrics(
    cfg: &config::Config,
    mastic: &MasticHistogram,
    client_0: &CollectorClient,
    client_1: &CollectorClient,
    attributes: &[Vec<bool>],
    num_clients: usize,
) -> io::Result<()> {
    for start in (0..num_clients).step_by(cfg.query_flp_batch_size) {
        let end = std::cmp::min(num_clients, start + cfg.query_flp_batch_size);
        let req = AttributeBasedMetricsValidateRequest {
            attributes: attributes.to_vec(),
            start,
            end,
        };

        // For each report, each aggregator evaluates the VIDPF on each of the attributes and returns
        // the VIDPF proof and its FLP verifier share.
        let t = Instant::now();
        let resp_0 = client_0.attribute_based_metrics_validate(long_context(), req.clone());
        let resp_1 = client_1.attribute_based_metrics_validate(long_context(), req.clone());
        let (results_0, results_1) = try_join!(resp_0, resp_1).unwrap();
        assert_eq!(results_0.len(), req.end - req.start);
        assert_eq!(results_1.len(), req.end - req.start);

        let mut rejected = Vec::with_capacity(req.end - req.start);
        for (client_index, ((mut verifier, eval_proof_0), (verifier_share_1, eval_proof_1))) in
            (req.start..req.end).zip(results_0.into_iter().zip(results_1.into_iter()))
        {
            vec_add(&mut verifier, &verifier_share_1);
            if !mastic.decide(&verifier).unwrap() {
                println!("Report {client_index} rejected (weight check failed)");
                rejected.push(client_index);
            }

            if eval_proof_0 != eval_proof_1 {
                println!("Report {client_index} rejected (onehot or path check failed)");
                rejected.push(client_index);
            }
        }

        println!(
            "{start}..{end}: report validation completed in {:?}: rejected {} reports",
            t.elapsed(),
            rejected.len()
        );

        let t = Instant::now();
        let req = AttributeBasedMetricsResultRequest {
            rejected,
            num_attributes: attributes.len(),
            start,
            end,
        };

        let resp_0 = client_0.attribute_based_metrics_result(long_context(), req.clone());
        let resp_1 = client_1.attribute_based_metrics_result(long_context(), req.clone());
        let (mut results, results_share_1) = try_join!(resp_0, resp_1).unwrap();
        for (r, s1) in results.iter_mut().zip(results_share_1.iter()) {
            vec_add(r, s1);
        }

        println!(
            "{start}..{end}: report aggregation completed in {:?}",
            t.elapsed(),
        );

        for (attribute, result) in attributes.iter().zip(results.iter()) {
            println!("{}: {result:?}", bits_to_bitstring(attribute));
        }
    }

    Ok(())
}

async fn run_plain_metrics(
    cfg: &config::Config,
    mastic: &MasticHistogram,
    client_0: &CollectorClient,
    client_1: &CollectorClient,
    num_clients: usize,
) -> io::Result<()> {
    let chunk_length = histogram_chunk_length(mastic.input_len(), Mode::PlainMetrics);
    let prio3 = Prio3::new_histogram(2, mastic.input_len(), chunk_length).unwrap();

    for start in (0..num_clients).step_by(cfg.query_flp_batch_size) {
        let end = std::cmp::min(num_clients, start + cfg.query_flp_batch_size);
        let req = PlainMetricsValidateRequest { start, end };

        // For each report, each aggregator evaluates the VIDPF on each of the attributes and returns
        // the VIDPF proof and its FLP verifier share.
        let t = Instant::now();
        let resp_0 = client_0.plain_metrics_validate(long_context(), req.clone());
        let resp_1 = client_1.plain_metrics_validate(long_context(), req.clone());
        let (results_0, results_1) = try_join!(resp_0, resp_1).unwrap();
        assert_eq!(results_0.len(), req.end - req.start);
        assert_eq!(results_1.len(), req.end - req.start);
        println!(
            "{start}..{end}: report validation initialized in {:?}",
            t.elapsed(),
        );

        // Relay each aggregator's prep shares to its peer.
        //
        // NOTE To validate the report, each aggregator combines the prep shares, then uses the
        // combined prep message and its state to compute its output share. Thus it is necessary to
        // relay each aggregator's prep share to its peer. We could save communication here by
        // having the driver compute the prep message at this point, however the libprio-rs API
        // currently doesn't allow this. See https://github.com/divviup/libprio-rs/issues/912.
        let t = Instant::now();
        let resp_0 = client_0.plain_metrics_result(
            long_context(),
            PlainMetricsResultRequest {
                peer_prep_shares: results_1,
                start,
                end,
            },
        );
        let resp_1 = client_1.plain_metrics_result(
            long_context(),
            PlainMetricsResultRequest {
                peer_prep_shares: results_0,
                start,
                end,
            },
        );
        let ((agg_share_0, rejected_count), (agg_share_1, rejected_count_1)) =
            try_join!(resp_0, resp_1).unwrap();
        assert_eq!(rejected_count, rejected_count_1);
        let result = prio3
            .unshard(
                &(),
                [agg_share_0, agg_share_1],
                end - start - rejected_count,
            )
            .unwrap();

        println!(
            "{start}..{end}: report validation and aggregation completed in {:?}: rejected {} reports",
            t.elapsed(),
            rejected_count,
        );

        println!("{result:?}");
    }

    Ok(())
}

/// Write the recorded crawl and the heavy hitters (the full-length prefixes that stayed heavy) as
/// JSON. The format is read by viz/index.html.
#[allow(clippy::too_many_arguments)]
fn write_export(
    path: &str,
    cfg: &config::Config,
    mastic: &MasticHistogram,
    export: CrawlExport,
    num_clients: usize,
    rides: Option<(usize, usize)>,
    keygen_seconds: f64,
    total_seconds: f64,
) -> io::Result<()> {
    let threshold_fraction = match cfg.mode {
        Mode::WeightedHeavyHitters { threshold } => threshold,
        _ => {
            return Err(Error::other(
                "--export only supports weighted heavy hitters",
            ))
        }
    };
    let heavy_hitters: Vec<_> = export
        .heavy_hitters()
        .into_iter()
        .map(|h| {
            let c = h.candidate;
            serde_json::json!({
                "bits": c.bits,
                "count": c.count,
                "value": c.value,
                "label": h.label,
                "center": h.center.map(|(lat, lon)| [lat, lon]),
            })
        })
        .collect();

    let json = serde_json::json!({
        "format": "mastic-crawl/1",
        "dataset": cfg.dataset.as_ref().map(|d| &d.path),
        "encoding": cfg.dataset.as_ref().map(|d| &d.encoding),
        "data_bits": cfg.data_bits,
        "input_len": mastic.input_len(),
        "num_clients": num_clients,
        "rides": rides.map(|(total, dropped)| serde_json::json!({"total": total, "dropped": dropped})),
        "threshold_fraction": threshold_fraction,
        "threshold": core::cmp::max(1, (threshold_fraction * (num_clients as f64)) as u64),
        "keygen_seconds": keygen_seconds,
        "total_seconds": total_seconds,
        "levels": export.levels,
        "heavy_hitters": heavy_hitters,
    });
    let file = std::fs::File::create(path)?;
    serde_json::to_writer(io::BufWriter::new(file), &json).map_err(Error::other)
}

#[tokio::main]
async fn main() -> io::Result<()> {
    let (cfg, _, num_clients, malicious) = config::get_args("Driver", false, true, true);
    assert!((0.0..0.8).contains(&malicious));
    // `--bench-out` and `--bench-meta` come together (clap enforces it).
    let mut bench = match &cfg.bench_meta {
        Some(meta) => {
            if cfg.dataset.is_none() || !matches!(cfg.mode, Mode::WeightedHeavyHitters { .. }) {
                return Err(Error::other(
                    "--bench-out needs weighted heavy hitters on a [dataset]",
                ));
            }
            Some(Bench::new(BenchMeta::parse(meta)?))
        }
        None => None,
    };
    println!("Running with {}% malicious clients", malicious * 100.0);
    let client_0 = CollectorClient::new(
        client::Config::default(),
        tcp::connect(cfg.server_0.clone(), Bincode::default).await?,
    )
    .spawn();
    let client_1 = CollectorClient::new(
        client::Config::default(),
        tcp::connect(cfg.server_1.clone(), Bincode::default).await?,
    )
    .spawn();

    println!("- Mode: {:?}", cfg.mode);
    println!("- Using {:?} histogram buckets", cfg.hist_buckets);
    println!("- Using {:?} bits", cfg.data_bits);

    let mastic = Mastic::new_histogram(cfg.hist_buckets).unwrap();
    // `--bench-out` reads the per-level candidates and the heavy hitters from the crawl record.
    let mut export = (cfg.export.is_some() || bench.is_some())
        .then(|| CrawlExport::new(&cfg, cfg.export.is_some()));
    // Totals for the export: (rides in the file, rides outside the encoded area).
    let mut rides = None;
    // Printed after loading and again with the results, so dropped rides are never missed.
    let mut rides_summary = None;
    let inputs = match &cfg.dataset {
        Some(dataset) => {
            let loaded = dataset::load_inputs(dataset, cfg.data_bits.unwrap(), cfg.hist_buckets)
                .map_err(|e| Error::other(e.to_string()))?;
            let mut inputs = loaded.inputs;
            inputs.truncate(num_clients);
            println!("- Loaded {} rides from {}", inputs.len(), dataset.path);
            let summary = format!(
                "*** DROPPED RIDES: {} of {} outside the area of {:?}; {} rides used ***",
                loaded.dropped,
                loaded.total,
                dataset.encoding,
                inputs.len()
            );
            println!("{summary}");
            rides_summary = Some(summary);
            rides = Some((loaded.total, loaded.dropped));
            inputs
        }
        None => synthetic_inputs(&cfg),
    };
    // With a dataset, every ride is one client.
    let num_clients = if cfg.dataset.is_some() {
        inputs.len()
    } else {
        num_clients
    };

    let start = Instant::now();
    println!("Generating reports...");
    let reports = generate_reports(&cfg, &mastic, inputs);
    let delta = start.elapsed().as_secs_f64();
    let keygen_seconds = delta;
    println!(
        "Generated {:?} keys in {:?} seconds ({:?} sec/key)",
        reports.len(),
        delta,
        delta / (reports.len() as f64)
    );

    let mut verify_key = [0; 16];
    thread_rng().fill(&mut verify_key);

    reset_servers(&cfg, &client_0, &client_1, &verify_key, &mut bench).await?;

    // Upload (A3): from the first report sent until the last one is stored on both servers.
    let upload = bench.as_ref().map(Bench::stopwatch);
    let mut left_to_go = num_clients;
    let reqs_in_flight = 1000;
    while left_to_go > 0 {
        let mut responses = vec![];

        for _ in 0..reqs_in_flight {
            let this_batch = std::cmp::min(left_to_go, cfg.add_report_share_batch_size);
            // Index of this batch's first ride when replaying a dataset.
            let first = cfg.dataset.as_ref().map(|_| num_clients - left_to_go);
            left_to_go -= this_batch;

            if this_batch > 0 {
                responses.push(add_reports(
                    &cfg,
                    (&client_0, &client_1),
                    &reports,
                    first,
                    this_batch,
                    malicious,
                    bench.is_some(),
                ));
            }
        }

        // The batches run one after another: each future only starts when awaited.
        for r in responses {
            let sizes = r.await?;
            if let (Some(b), Some(s)) = (bench.as_mut(), sizes) {
                b.overhead += s.overhead;
                b.add(
                    "add_report_shares",
                    Phase::Upload,
                    false,
                    s.request,
                    s.response,
                );
            }
        }
    }
    if let (Some(b), Some(sw)) = (bench.as_mut(), upload) {
        b.upload_ms = b.elapsed_ms(sw);
    }

    let start = Instant::now();
    if let Some(b) = bench.as_mut() {
        b.online = Some(b.stopwatch());
    }
    match cfg.mode {
        Mode::WeightedHeavyHitters { .. } => {
            // `tree_init` counts towards the `flp` step.
            let flp_step = bench.as_ref().map(Bench::stopwatch);
            tree_init(&client_0, &client_1, &mut bench).await?;

            for level in 0..cfg.data_bits.unwrap() - 1 {
                let start_level = Instant::now();
                if level == 0 {
                    run_flp_queries(&cfg, &mastic, &client_0, &client_1, num_clients, &mut bench)
                        .await?;
                    if let (Some(b), Some(sw)) = (bench.as_mut(), flp_step) {
                        b.step("flp", sw);
                    }
                }
                let level_step = bench.as_ref().map(Bench::stopwatch);
                run_level(
                    &cfg,
                    &mastic,
                    &client_0,
                    &client_1,
                    num_clients,
                    &mut export,
                    &mut bench,
                )
                .await?;
                if let (Some(b), Some(sw)) = (bench.as_mut(), level_step) {
                    b.step(format!("level_{level}"), sw);
                }
                let seconds = start_level.elapsed().as_secs_f64();
                println!("- Time for level {}: {:?}", level + 1, seconds);
                if let Some(level) = export.as_mut().and_then(|e| e.levels.last_mut()) {
                    level.seconds = seconds;
                }
            }
            let start_level = Instant::now();
            run_level_last(
                &cfg,
                &mastic,
                &client_0,
                &client_1,
                num_clients,
                &mut export,
                &mut bench,
            )
            .await?;
            if let Some(level) = export.as_mut().and_then(|e| e.levels.last_mut()) {
                level.seconds = start_level.elapsed().as_secs_f64();
            }
        }

        Mode::AttributeBasedMetrics { num_attributes } => {
            // Synthesize a set of attributes.
            let attributes = {
                let mut rng = rand::thread_rng();
                let zipf = zipf::ZipfDistribution::new(cfg.zipf_unique_buckets, cfg.zipf_exponent)
                    .unwrap();
                let mut unique_inputs = HashSet::with_capacity(num_attributes);
                for _ in 0..num_attributes {
                    let client_index = zipf.sample(&mut rng);
                    unique_inputs.insert(reports[client_index].unwrap_alpha());
                }
                unique_inputs.into_iter().collect::<Vec<_>>()
            };
            println!("Using {} attributes", attributes.len());

            run_attribute_based_metrics(
                &cfg,
                &mastic,
                &client_0,
                &client_1,
                &attributes,
                num_clients,
            )
            .await?;
        }
        Mode::PlainMetrics => {
            run_plain_metrics(&cfg, &mastic, &client_0, &client_1, num_clients).await?
        }
    };
    let total_seconds = start.elapsed().as_secs_f64();
    println!("Total time {:?}", total_seconds);
    if let Some(summary) = &rides_summary {
        println!("{summary}");
    }

    if let (Some(b), Some(path), Some(export)) = (&bench, &cfg.bench_out, &export) {
        b.print_summary();
        let line = b.result_line(&cfg, num_clients, export)?;
        append_jsonl(path, &line)?;
        println!("Appended the benchmark result to {path}");
    }

    if let (Some(path), Some(export)) = (&cfg.export, export) {
        write_export(
            path,
            &cfg,
            &mastic,
            export,
            num_clients,
            rides,
            keygen_seconds,
            total_seconds,
        )?;
        println!("Wrote crawl to {path}");
    }

    Ok(())
}
