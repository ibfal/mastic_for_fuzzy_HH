//! Load ride-sharing pickups (or other located reports) and turn them into Mastic inputs
//! `(alpha, bucket)`.

use std::error::Error;

use serde::Deserialize;

use crate::{
    config::{DatasetConfig, Weight},
    geo,
};

/// Hours in a day: the range of the `hour` column and the finest `Weight::HourOfDay`.
pub const HOURS: usize = 24;

/// One CSV row. Columns are matched by header name; all other columns are ignored.
#[derive(Deserialize, Debug)]
pub struct Ride {
    pub start_location_lat: f64,
    pub start_location_long: f64,

    /// Hour of day, 0 to 23, for `Weight::HourOfDay`. Optional: files without the column (the
    /// ride data) load as `None`, and so does an empty cell.
    #[serde(default)]
    pub hour: Option<i64>,
}

pub fn load_rides(path: &str) -> Result<Vec<Ride>, Box<dyn Error>> {
    let mut reader = csv::Reader::from_path(path)?;
    let rides = reader.deserialize().collect::<Result<Vec<Ride>, _>>()?;
    Ok(rides)
}

/// The bucket of `hour` (0 to 23) when the day is split into `buckets` equal blocks:
/// `hour * buckets / 24`. With 6 buckets, hours 0-3 go to bucket 0, 4-7 to bucket 1, and so on.
pub fn hour_bucket(hour: usize, buckets: usize) -> usize {
    hour * buckets / HOURS
}

impl Weight {
    /// Checks the weight's own parameters.
    pub fn validate(&self) -> Result<(), String> {
        match *self {
            Weight::Constant => Ok(()),
            Weight::HourOfDay { buckets } => {
                if (1..=HOURS).contains(&buckets) && HOURS % buckets == 0 {
                    Ok(())
                } else {
                    Err(format!(
                        "hour_of_day buckets = {buckets}: must divide {HOURS} (1, 2, 3, 4, 6, 8, \
                         12 or 24)"
                    ))
                }
            }
        }
    }

    /// Number of histogram buckets this weight needs. Must equal `hist_buckets` in the config.
    pub fn num_buckets(&self) -> usize {
        match *self {
            Weight::Constant => 1,
            Weight::HourOfDay { buckets } => buckets,
        }
    }

    /// The histogram bucket a ride contributes to. `Weight::HourOfDay` needs the ride's `hour`,
    /// 0 to 23; `Weight::Constant` ignores it.
    pub fn bucket(&self, ride: &Ride) -> Result<usize, String> {
        match *self {
            Weight::Constant => Ok(0),
            Weight::HourOfDay { buckets } => {
                let hour = ride
                    .hour
                    .ok_or("hour_of_day needs an `hour` column with a value in every row")?;
                if !(0..HOURS as i64).contains(&hour) {
                    return Err(format!("hour = {hour} is outside 0 to {}", HOURS - 1));
                }
                Ok(hour_bucket(hour as usize, buckets))
            }
        }
    }
}

/// The encoded rides of a dataset.
pub struct LoadedInputs {
    /// `(alpha, bucket)` for every ride inside the encoded area, in file order.
    pub inputs: Vec<(Vec<bool>, usize)>,

    /// Number of rides in the file.
    pub total: usize,

    /// Rides outside the area covered by the encoding. They are not sent, so they do not count
    /// towards the number of clients the threshold is computed from.
    pub dropped: usize,
}

/// Load the dataset and encode each ride as `(alpha, bucket)`, in file order. Every row's bucket
/// is computed, also for rides outside the area, so a missing or invalid `hour` is always an
/// error.
pub fn load_inputs(
    dataset: &DatasetConfig,
    data_bits: usize,
    hist_buckets: usize,
) -> Result<LoadedInputs, Box<dyn Error>> {
    let encoder = geo::encoder(&dataset.encoding);
    if encoder.depth() != data_bits {
        return Err(format!(
            "data_bits = {data_bits} but {:?} produces {} bits",
            dataset.encoding,
            encoder.depth()
        )
        .into());
    }
    dataset.weight.validate()?;
    if dataset.weight.num_buckets() != hist_buckets {
        return Err(format!(
            "hist_buckets = {hist_buckets} but weight {:?} needs {}",
            dataset.weight,
            dataset.weight.num_buckets()
        )
        .into());
    }

    let rides = load_rides(&dataset.path)?;
    let mut inputs = Vec::with_capacity(rides.len());
    for (i, ride) in rides.iter().enumerate() {
        // Row numbers as in the file: the header is row 1.
        let bucket = dataset
            .weight
            .bucket(ride)
            .map_err(|e| format!("{}: row {}: {e}", dataset.path, i + 2))?;
        if let Some(alpha) = encoder.encode(ride.start_location_lat, ride.start_location_long) {
            inputs.push((alpha, bucket));
        }
    }
    Ok(LoadedInputs {
        total: rides.len(),
        dropped: rides.len() - inputs.len(),
        inputs,
    })
}

#[cfg(test)]
mod tests {
    use std::{
        fs,
        path::PathBuf,
        sync::atomic::{AtomicUsize, Ordering},
    };

    use super::*;
    use crate::config::Encoding;

    const TEN_LOCATIONS: &str = "data/synthetic/ten_locations.csv";

    fn morton_austin() -> Encoding {
        Encoding::Morton {
            min_lat: 29.9005,
            max_lat: 30.9245,
            min_lon: -98.0505,
            max_lon: -97.0265,
            axis_bits: 10,
        }
    }

    fn dataset(path: &str, weight: Weight) -> DatasetConfig {
        DatasetConfig {
            path: path.into(),
            encoding: morton_austin(),
            weight,
        }
    }

    /// A CSV in the temp folder with a unique name; removed when dropped.
    struct TempCsv(PathBuf);

    impl TempCsv {
        fn new(content: &str) -> Self {
            static NEXT: AtomicUsize = AtomicUsize::new(0);
            let path = std::env::temp_dir().join(format!(
                "mastic-dataset-test-{}-{}.csv",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            fs::write(&path, content).unwrap();
            Self(path)
        }

        fn path(&self) -> &str {
            self.0.to_str().unwrap()
        }
    }

    impl Drop for TempCsv {
        fn drop(&mut self) {
            let _ = fs::remove_file(&self.0);
        }
    }

    fn ride(hour: Option<i64>) -> Ride {
        Ride {
            start_location_lat: 30.0,
            start_location_long: -97.5,
            hour,
        }
    }

    #[test]
    fn hour_buckets_for_24_6_and_1() {
        let hours = 0..HOURS;
        assert_eq!(
            hours.clone().map(|h| hour_bucket(h, 24)).collect::<Vec<_>>(),
            (0..24).collect::<Vec<_>>()
        );
        assert_eq!(
            hours.clone().map(|h| hour_bucket(h, 6)).collect::<Vec<_>>(),
            vec![0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5]
        );
        assert!(hours.map(|h| hour_bucket(h, 1)).all(|b| b == 0));
    }

    #[test]
    fn weight_bucket_reads_the_hour() {
        let w24 = Weight::HourOfDay { buckets: 24 };
        let w6 = Weight::HourOfDay { buckets: 6 };
        assert_eq!(w24.bucket(&ride(Some(0))), Ok(0));
        assert_eq!(w24.bucket(&ride(Some(23))), Ok(23));
        assert_eq!(w6.bucket(&ride(Some(13))), Ok(3));
        assert_eq!(w6.bucket(&ride(Some(23))), Ok(5));
        assert!(w24.bucket(&ride(None)).is_err());
        assert!(w24.bucket(&ride(Some(24))).is_err());
        assert!(w24.bucket(&ride(Some(-1))).is_err());
        // Constant ignores the hour, also a missing or invalid one.
        assert_eq!(Weight::Constant.bucket(&ride(None)), Ok(0));
        assert_eq!(Weight::Constant.bucket(&ride(Some(99))), Ok(0));
    }

    #[test]
    fn bucket_counts_must_divide_24() {
        for buckets in [1, 2, 3, 4, 6, 8, 12, 24] {
            let w = Weight::HourOfDay { buckets };
            assert!(w.validate().is_ok(), "{buckets}");
            assert_eq!(w.num_buckets(), buckets);
        }
        for buckets in [0, 5, 7, 25, 48] {
            assert!(Weight::HourOfDay { buckets }.validate().is_err(), "{buckets}");
        }
        assert_eq!(Weight::Constant.num_buckets(), 1);
        assert!(Weight::Constant.validate().is_ok());
    }

    #[test]
    fn file_without_hour_loads_with_constant_only() {
        let loaded = load_inputs(&dataset(TEN_LOCATIONS, Weight::Constant), 20, 1).unwrap();
        assert_eq!((loaded.total, loaded.dropped), (165, 0));
        assert!(loaded.inputs.iter().all(|(_, b)| *b == 0));

        let err = load_inputs(
            &dataset(TEN_LOCATIONS, Weight::HourOfDay { buckets: 24 }),
            20,
            24,
        )
        .err()
        .unwrap()
        .to_string();
        assert!(err.contains("row 2") && err.contains("hour"), "{err}");
    }

    #[test]
    fn file_with_hour_loads_buckets() {
        let csv = TempCsv::new(
            "start_location_lat,start_location_long,hour,stay_id\n\
             30.197,-97.666,0,a\n\
             30.275,-97.740,7,b\n\
             30.275,-97.740,13,c\n\
             30.286,-97.739,23,d\n\
             40.0,-97.739,5,outside\n",
        );
        let buckets_of = |weight: Weight, hist_buckets| {
            let loaded = load_inputs(&dataset(csv.path(), weight), 20, hist_buckets).unwrap();
            assert_eq!((loaded.total, loaded.dropped), (5, 1));
            loaded.inputs.iter().map(|(_, b)| *b).collect::<Vec<_>>()
        };
        assert_eq!(
            buckets_of(Weight::HourOfDay { buckets: 24 }, 24),
            [0, 7, 13, 23]
        );
        assert_eq!(buckets_of(Weight::HourOfDay { buckets: 6 }, 6), [0, 1, 3, 5]);
        assert_eq!(buckets_of(Weight::Constant, 1), [0, 0, 0, 0]);
        // hist_buckets must match the weight.
        assert!(load_inputs(
            &dataset(csv.path(), Weight::HourOfDay { buckets: 24 }),
            20,
            6
        )
        .is_err());
        assert!(load_inputs(&dataset(csv.path(), Weight::HourOfDay { buckets: 5 }), 20, 5).is_err());
    }

    #[test]
    fn missing_or_invalid_hours_are_errors() {
        let w = Weight::HourOfDay { buckets: 24 };
        for (bad, row) in [
            ("30.2,-97.7,\n", "row 3"),
            ("30.2,-97.7,24\n", "row 3"),
            ("30.2,-97.7,-1\n", "row 3"),
        ] {
            let csv = TempCsv::new(&format!(
                "start_location_lat,start_location_long,hour\n30.2,-97.7,1\n{bad}"
            ));
            let err = load_inputs(&dataset(csv.path(), w), 20, 24)
                .err()
                .unwrap()
                .to_string();
            assert!(err.contains(row), "{bad:?}: {err}");
        }
        // An invalid hour also fails for a ride outside the encoded area.
        let csv = TempCsv::new("start_location_lat,start_location_long,hour\n40.0,-97.7,30\n");
        assert!(load_inputs(&dataset(csv.path(), w), 20, 24).is_err());
    }

    #[test]
    fn weight_config_syntax() {
        #[derive(Deserialize)]
        struct Wrapper {
            dataset: DatasetConfig,
        }
        let parse = |weight_line: &str| {
            let text = format!(
                "[dataset]\npath = \"x.csv\"\nencoding = \"mosaic_grid\"\n{weight_line}\n"
            );
            toml::from_str::<Wrapper>(&text).map(|w| w.dataset.weight)
        };
        assert_eq!(
            parse("weight.hour_of_day = { buckets = 24 }").unwrap(),
            Weight::HourOfDay { buckets: 24 }
        );
        assert_eq!(
            parse("weight = { hour_of_day = { buckets = 6 } }").unwrap(),
            Weight::HourOfDay { buckets: 6 }
        );
        assert_eq!(parse("weight = \"constant\"").unwrap(), Weight::Constant);
        assert_eq!(parse("").unwrap(), Weight::Constant);
        assert!(parse("weight = \"hour_of_day\"").is_err());
    }
}
