//! Run the heavy-hitters crawl in-process (both aggregators, no network) with hour-of-day
//! weights, and check that every heavy cell's reconstructed histogram is its hour profile:
//! `value[..buckets]` = reports per hour bucket, `value[buckets]` = the count. The FLP is not run
//! here (tests/flp_test.rs covers it); tests/encoding_pipeline_test.rs does the same with one
//! bucket.

use std::collections::BTreeMap;

use mastic::{
    collect::{KeyCollection, ReportShare},
    config::{Alpha, DatasetConfig, Encoding, Weight},
    dataset::{self, hour_bucket},
    prg::PrgSeed,
    vidpf::VidpfKey,
    Mastic,
};
use prio::field::Field128;

/// Heavy cells `(alpha, value)` the two aggregators find, with the driver's threshold rule
/// `max(1, floor(fraction * N))`. `value` holds the bucket counts and then the counter.
fn crawl(
    inputs: &[(Vec<bool>, Vec<usize>)],
    data_bits: usize,
    hist_buckets: usize,
    fraction: f64,
) -> Vec<(Vec<bool>, Vec<u64>)> {
    let mastic = Mastic::new_histogram(hist_buckets).unwrap();
    let mut aggregators = [0, 1].map(|server_id| {
        KeyCollection::new(
            mastic.clone(),
            server_id,
            &PrgSeed::zero(),
            Some(data_bits),
            [0; 16],
        )
    });
    for (alpha, buckets) in inputs {
        let beta = mastic.encode_measurement(buckets).unwrap();
        let (key_0, key_1) = VidpfKey::gen(alpha, &beta);
        for (aggregator, vidpf_key) in aggregators.iter_mut().zip([key_0, key_1]) {
            aggregator.add_report_share(ReportShare::Mastic {
                nonce: Default::default(),
                vidpf_key,
                flp_proof_share: Default::default(),
                flp_joint_rand_parts: Default::default(),
            });
        }
    }

    let input_len = mastic.input_len();
    assert_eq!(input_len, hist_buckets);
    let threshold = 1.max((fraction * inputs.len() as f64) as u64);
    aggregators.iter_mut().for_each(KeyCollection::tree_init);
    for _ in 0..data_bits - 1 {
        let [counts_0, counts_1] = aggregators
            .each_mut()
            .map(|a| a.tree_crawl(1, &Vec::new(), false).0);
        let keep = KeyCollection::keep_values(threshold, &counts_0, &counts_1);
        check_revealed_histograms(&aggregators, &counts_0, &counts_1, input_len);
        aggregators.iter_mut().for_each(|a| a.tree_prune(&keep));
    }
    let [counts_0, counts_1] = aggregators.each_mut().map(|a| a.tree_crawl_last());
    let keep = KeyCollection::keep_values(threshold, &counts_0, &counts_1);
    aggregators.iter_mut().for_each(|a| a.tree_prune(&keep));

    let [shares_0, shares_1] = aggregators.each_ref().map(|a| a.final_shares());
    KeyCollection::final_values(input_len, &shares_0, &shares_1)
        .into_iter()
        .map(|r| {
            let value = r.value.iter().map(|&v| u128::from(v) as u64).collect();
            (r.path, value)
        })
        .collect()
}

/// With `reveal_crawl_histograms` (the parking app), the servers also send their shares of each
/// candidate's full value after a crawl: the histograms must add up to the count, and the count
/// must be the one the crawl's counter shares give.
fn check_revealed_histograms(
    aggregators: &[KeyCollection; 2],
    counts_0: &[Field128],
    counts_1: &[Field128],
    input_len: usize,
) {
    let [values_0, values_1] = aggregators.each_ref().map(|a| a.frontier_value_shares());
    let counts = KeyCollection::counts(counts_0, counts_1);
    assert_eq!(values_0.len(), counts.len());
    for ((v0, v1), count) in values_0.iter().zip(&values_1).zip(counts) {
        let value: Vec<u64> = v0
            .iter()
            .zip(v1)
            .map(|(&a, &b)| u128::from(a + b) as u64)
            .collect();
        assert_eq!(value.len(), input_len + 1);
        assert_eq!(value[input_len], count);
        assert_eq!(value[..input_len].iter().sum::<u64>(), count);
    }
}

/// Three heavy locations with distinct hour profiles, one light one, and one outside the box.
/// Location A has no report at hour 23, so its last bucket is 0 (the case E7 used to hide).
fn write_csv(tag: usize) -> std::path::PathBuf {
    let mut rows = String::from("start_location_lat,start_location_long,hour\n");
    let profiles: [(f64, f64, &[(i64, usize)]); 5] = [
        (30.197, -97.666, &[(0, 3), (8, 10), (12, 7), (22, 2)]),
        (30.275, -97.740, &[(9, 4), (13, 5), (17, 6), (23, 3)]),
        (30.286, -97.739, &[(1, 1), (4, 2), (11, 8), (20, 5)]),
        (30.402, -97.725, &[(10, 2)]),
        (40.000, -97.725, &[(5, 4)]),
    ];
    for (lat, lon, hours) in profiles {
        for &(hour, n) in hours {
            for _ in 0..n {
                rows.push_str(&format!("{lat},{lon},{hour}\n"));
            }
        }
    }
    let path = std::env::temp_dir().join(format!(
        "mastic-hour-weight-test-{}-{tag}.csv",
        std::process::id()
    ));
    std::fs::write(&path, rows).unwrap();
    path
}

fn check_hour_profiles(buckets: usize) {
    let csv = write_csv(buckets);
    let dataset_cfg = DatasetConfig {
        path: csv.to_str().unwrap().into(),
        encoding: Encoding::Morton {
            min_lat: 29.9005,
            max_lat: 30.9245,
            min_lon: -98.0505,
            max_lon: -97.0265,
            axis_bits: 10,
        },
        alpha: Alpha::Pickup,
        weights: vec![Weight::HourOfDay { buckets }],
        zones: vec![],
        companies: vec![],
    };
    let rides = dataset::load_rides(&dataset_cfg.path).unwrap();
    let loaded = dataset::load_inputs(&dataset_cfg, 20, buckets).unwrap();
    std::fs::remove_file(&csv).unwrap();
    assert_eq!((loaded.total, loaded.dropped), (rides.len(), 4));

    // Plaintext answer: bucket counts per cell, then the counter.
    let encoder = mastic::geo::encoder(&dataset_cfg.encoding);
    let mut expected: BTreeMap<Vec<bool>, Vec<u64>> = BTreeMap::new();
    for ride in &rides {
        let Some(alpha) = encoder.encode(ride.start_location_lat, ride.start_location_long) else {
            continue;
        };
        let v = expected
            .entry(alpha)
            .or_insert_with(|| vec![0; buckets + 1]);
        v[hour_bucket(ride.hour.unwrap() as usize, buckets)] += 1;
        v[buckets] += 1;
    }
    // Threshold 0.1 of 58 reports: T = 5, so the light location (2 reports) drops out.
    let n = loaded.inputs.len();
    assert_eq!(n, 58);
    expected.retain(|_, v| v[buckets] >= 5);
    assert_eq!(expected.len(), 3);

    let found: BTreeMap<_, _> = crawl(&loaded.inputs, 20, buckets, 0.1)
        .into_iter()
        .collect();
    assert_eq!(found, expected);
}

#[test]
fn twenty_four_hour_buckets_give_each_cells_profile() {
    check_hour_profiles(24);
}

#[test]
fn six_four_hour_blocks_give_each_cells_profile() {
    check_hour_profiles(6);
}
