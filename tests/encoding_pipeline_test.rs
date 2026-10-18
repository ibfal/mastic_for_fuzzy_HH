//! Run the whole heavy-hitters crawl in-process (both aggregators, no network) on the 10-landmark
//! dataset with every encoding, and check the known answer: the top four landmarks with counts
//! 50, 40, 30 and 20. The FLP is not run here; tests/collect_test.rs covers the crawl the same way.

use mastic::{
    collect::{KeyCollection, ReportShare},
    config::{self, DatasetConfig, Encoding, Weight},
    dataset, geo,
    prg::PrgSeed,
    vidpf::VidpfKey,
    Mastic,
};

/// `(lat, lon, rides)` of the landmarks above threshold 0.1 in scripts/make_synthetic_rides.py.
const HEAVY_LANDMARKS: [(f64, f64, u64); 4] = [
    (30.197, -97.666, 50),
    (30.275, -97.740, 40),
    (30.286, -97.739, 30),
    (30.402, -97.725, 20),
];

fn recommended_morton() -> Encoding {
    Encoding::Morton {
        min_lat: 29.9005,
        max_lat: 30.9245,
        min_lon: -98.0505,
        max_lon: -97.0265,
        axis_bits: 10,
    }
}

fn ten_locations(encoding: Encoding) -> DatasetConfig {
    DatasetConfig {
        path: "data/synthetic/ten_locations.csv".into(),
        encoding,
        weight: Weight::Constant,
    }
}

/// The heavy hitters `(alpha, count)` the two aggregators find, with the driver's threshold rule
/// `max(1, floor(fraction * N))`.
fn crawl(inputs: &[(Vec<bool>, usize)], data_bits: usize, fraction: f64) -> Vec<(Vec<bool>, u64)> {
    let mastic = Mastic::new_histogram(1).unwrap();
    let mut aggregators = [0, 1].map(|server_id| {
        KeyCollection::new(
            mastic.clone(),
            server_id,
            &PrgSeed::zero(),
            Some(data_bits),
            [0; 16],
        )
    });
    for (alpha, bucket) in inputs {
        let beta = mastic.encode_measurement(bucket).unwrap();
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
    let threshold = 1.max((fraction * inputs.len() as f64) as u64);
    aggregators.iter_mut().for_each(KeyCollection::tree_init);
    for _ in 0..data_bits - 1 {
        let [counts_0, counts_1] = aggregators
            .each_mut()
            .map(|a| a.tree_crawl(1, &Vec::new(), false).0);
        let keep = KeyCollection::keep_values(input_len, threshold, &counts_0, &counts_1);
        aggregators.iter_mut().for_each(|a| a.tree_prune(&keep));
    }
    let [counts_0, counts_1] = aggregators.each_mut().map(|a| a.tree_crawl_last());
    let keep = KeyCollection::keep_values(input_len, threshold, &counts_0, &counts_1);
    aggregators.iter_mut().for_each(|a| a.tree_prune(&keep));

    let [shares_0, shares_1] = aggregators.each_ref().map(|a| a.final_shares());
    KeyCollection::final_values(input_len, &shares_0, &shares_1)
        .into_iter()
        .map(|result| (result.path, u128::from(result.value[input_len]) as u64))
        .collect()
}

fn assert_finds_heavy_landmarks(encoding: Encoding, data_bits: usize) {
    let loaded = dataset::load_inputs(&ten_locations(encoding.clone()), data_bits, 1).unwrap();
    assert_eq!((loaded.total, loaded.dropped), (165, 0));

    let mut found = crawl(&loaded.inputs, data_bits, 0.1);
    found.sort_by_key(|&(_, count)| std::cmp::Reverse(count));

    let encoder = geo::encoder(&encoding);
    let expected = HEAVY_LANDMARKS
        .iter()
        .map(|&(lat, lon, count)| (encoder.encode(lat, lon).unwrap(), count))
        .collect::<Vec<_>>();
    assert_eq!(found, expected);
}

#[test]
fn mosaic_grid_finds_the_heavy_landmarks() {
    assert_finds_heavy_landmarks(Encoding::MosaicGrid, 22);
}

#[test]
fn geohash_finds_the_heavy_landmarks() {
    assert_finds_heavy_landmarks(
        Encoding::Geohash {
            prefix: "9v6".into(),
            bits: 20,
        },
        20,
    );
}

#[test]
fn h3_finds_the_heavy_landmarks() {
    assert_finds_heavy_landmarks(
        Encoding::H3 {
            prefix: "82489ffffffffff".into(),
            resolution: 9,
        },
        21,
    );
}

#[test]
fn morton_finds_the_heavy_landmarks() {
    assert_finds_heavy_landmarks(recommended_morton(), 20);
}

#[test]
fn load_inputs_rejects_wrong_data_bits() {
    assert!(dataset::load_inputs(&ten_locations(recommended_morton()), 22, 1).is_err());
}

#[test]
fn load_inputs_counts_dropped_rides() {
    // A box starting at 30.25 N leaves out the Airport (50 rides), South Congress (5) and the
    // Circuit of the Americas (3).
    let encoding = Encoding::Morton {
        min_lat: 30.25,
        max_lat: 30.25 + 1.024,
        min_lon: -98.0505,
        max_lon: -97.0265,
        axis_bits: 10,
    };
    let loaded = dataset::load_inputs(&ten_locations(encoding), 20, 1).unwrap();
    assert_eq!(
        (loaded.total, loaded.dropped, loaded.inputs.len()),
        (165, 58, 107)
    );
}

#[test]
fn every_dataset_config_matches_its_encoder() {
    let mut checked = 0;
    for entry in std::fs::read_dir("src/configs").unwrap() {
        let path = entry.unwrap().path();
        if !path.extension().is_some_and(|e| e == "toml") {
            continue;
        }
        let cfg = config::get_config(path.to_str().unwrap());
        let Some(dataset) = &cfg.dataset else {
            continue;
        };
        let name = path.display();
        assert_eq!(
            geo::encoder(&dataset.encoding).depth(),
            cfg.data_bits.unwrap(),
            "{name}: data_bits does not match the encoding"
        );
        assert_eq!(
            dataset.weight.num_buckets(),
            cfg.hist_buckets,
            "{name}: hist_buckets does not match the weight"
        );
        assert!(
            std::path::Path::new(&dataset.path).exists(),
            "{name}: {} not found",
            dataset.path
        );
        checked += 1;
    }
    assert!(checked > 0);
}
