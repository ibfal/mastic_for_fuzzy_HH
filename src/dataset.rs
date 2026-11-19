//! Load ride-sharing pickups (or other located reports, such as parking stays) and turn them into
//! Mastic inputs `(alpha, buckets)`.

use std::error::Error;

use serde::Deserialize;

pub use crate::config::HOURS;
use crate::{
    config::{Alpha, DatasetConfig, Weight, Zone},
    geo,
};

/// Values of `requested_car_category`, in bucket order. The CSV stores them as Python byte-string
/// literals (`b'REGULAR'`); both spellings are accepted.
pub const CAR_CATEGORIES: [&str; 4] = ["REGULAR", "SUV", "PREMIUM", "LUXURY"];

/// One CSV row. Columns are matched by header name; all other columns are ignored. Only the
/// pickup is required: the other columns are read when the weight needs them.
#[derive(Deserialize, Debug, Default)]
pub struct Ride {
    pub start_location_lat: f64,
    pub start_location_long: f64,
    /// Pickup hour, 0 to 23, for `Weight::HourOfDay` (the ride data).
    #[serde(default)]
    pub hour_of_day: Option<usize>,
    /// Hour of day, 0 to 23, for `Weight::HourOfDay` in files without `hour_of_day` (the parking
    /// data). Optional: files without the column load as `None`, and so does an empty cell.
    #[serde(default)]
    pub hour: Option<i64>,
    #[serde(default)]
    pub dropoff_hour: Option<usize>,
    #[serde(default)]
    pub requested_car_category: Option<String>,
    #[serde(default)]
    pub surge_factor: Option<f64>,
    #[serde(default)]
    pub end_location_lat: Option<f64>,
    #[serde(default)]
    pub end_location_long: Option<f64>,
    #[serde(default)]
    pub company: Option<String>,
}

pub fn load_rides(path: &str) -> Result<Vec<Ride>, Box<dyn Error>> {
    let mut reader = csv::Reader::from_path(path)?;
    let rides = reader.deserialize().collect::<Result<Vec<Ride>, _>>()?;
    Ok(rides)
}

/// Great-circle distance in km.
fn haversine_km(lat1: f64, lon1: f64, lat2: f64, lon2: f64) -> f64 {
    const EARTH_RADIUS_KM: f64 = 6371.0088;
    let (phi1, phi2) = (lat1.to_radians(), lat2.to_radians());
    let dphi = (lat2 - lat1).to_radians();
    let dlambda = (lon2 - lon1).to_radians();
    let a = (dphi / 2.0).sin().powi(2) + phi1.cos() * phi2.cos() * (dlambda / 2.0).sin().powi(2);
    2.0 * EARTH_RADIUS_KM * a.sqrt().asin()
}

fn column<T: Copy>(value: Option<T>, name: &str) -> Result<T, String> {
    value.ok_or_else(|| format!("missing or empty column `{name}`"))
}

/// An hour column's value, which must be in 0..24.
fn hour(value: Option<usize>, name: &str) -> Result<usize, String> {
    let hour = column(value, name)?;
    if hour < HOURS {
        Ok(hour)
    } else {
        Err(format!("{name} = {hour} is not in 0..24"))
    }
}

/// The pickup hour for `Weight::HourOfDay`: the `hour_of_day` column, or else the `hour` column.
fn pickup_hour(ride: &Ride) -> Result<usize, String> {
    match (ride.hour_of_day, ride.hour) {
        (Some(_), Some(_)) => Err("both `hour_of_day` and `hour` are set; use one".into()),
        (Some(_), None) => hour(ride.hour_of_day, "hour_of_day"),
        (None, Some(h)) if (0..HOURS as i64).contains(&h) => Ok(h as usize),
        (None, Some(h)) => Err(format!("hour = {h} is outside 0 to {}", HOURS - 1)),
        (None, None) => Err(
            "hour_of_day needs an `hour_of_day` or `hour` column with a value in every row".into(),
        ),
    }
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
            _ => Ok(()),
        }
    }

    /// Name used in printed and exported results.
    pub fn name(&self) -> &'static str {
        match self {
            Weight::Constant => "constant",
            Weight::HourOfDay { .. } => "hour_of_day",
            Weight::DropoffHour => "dropoff_hour",
            Weight::CarCategory => "car_category",
            Weight::Surge => "surge",
            Weight::DropoffZone => "dropoff_zone",
            Weight::Company => "company",
            Weight::PickupZone => "pickup_zone",
        }
    }

    /// Name of each bucket, in order. `zones` are the dataset's drop-off zones and `companies`
    /// its company names.
    pub fn bucket_labels(&self, zones: &[Zone], companies: &[String]) -> Vec<String> {
        match self {
            Weight::Constant => vec!["rides".into()],
            Weight::HourOfDay { buckets: HOURS } | Weight::DropoffHour => {
                (0..HOURS).map(|h| format!("{h:02}h")).collect()
            }
            // Blocks of the day: "00-04h", "04-08h", ... for 6 buckets.
            Weight::HourOfDay { buckets } => {
                let width = HOURS / buckets.max(&1);
                (0..*buckets)
                    .map(|b| format!("{:02}-{:02}h", b * width, (b + 1) * width))
                    .collect()
            }
            Weight::CarCategory => CAR_CATEGORIES.iter().map(|c| c.to_string()).collect(),
            Weight::Surge => vec!["no surge".into(), "surge".into()],
            Weight::DropoffZone | Weight::PickupZone => zones
                .iter()
                .map(|z| z.name.clone())
                .chain(std::iter::once("other".into()))
                .collect(),
            Weight::Company => companies.to_vec(),
        }
    }

    /// The bucket a ride goes into, or why the ride cannot be bucketed.
    pub fn bucket(
        &self,
        ride: &Ride,
        zones: &[Zone],
        companies: &[String],
    ) -> Result<usize, String> {
        match self {
            Weight::Constant => Ok(0),
            Weight::HourOfDay { buckets } => Ok(hour_bucket(pickup_hour(ride)?, *buckets)),
            Weight::DropoffHour => hour(ride.dropoff_hour, "dropoff_hour"),
            Weight::CarCategory => {
                let raw = ride
                    .requested_car_category
                    .as_deref()
                    .ok_or("missing column `requested_car_category`")?;
                let name = raw
                    .strip_prefix("b'")
                    .and_then(|s| s.strip_suffix('\''))
                    .unwrap_or(raw);
                CAR_CATEGORIES
                    .iter()
                    .position(|&c| c == name)
                    .ok_or_else(|| format!("unknown requested_car_category {raw:?}"))
            }
            Weight::Surge => Ok(usize::from(
                column(ride.surge_factor, "surge_factor")? > 1.0,
            )),
            Weight::DropoffZone => {
                let lat = column(ride.end_location_lat, "end_location_lat")?;
                let lon = column(ride.end_location_long, "end_location_long")?;
                Ok(zones
                    .iter()
                    .position(|z| haversine_km(lat, lon, z.lat, z.lon) <= z.radius_km)
                    .unwrap_or(zones.len()))
            }
            Weight::PickupZone => Ok(zones
                .iter()
                .position(|z| {
                    haversine_km(
                        ride.start_location_lat,
                        ride.start_location_long,
                        z.lat,
                        z.lon,
                    ) <= z.radius_km
                })
                .unwrap_or(zones.len())),
            Weight::Company => {
                let name = ride.company.as_deref().ok_or("missing column `company`")?;
                companies
                    .iter()
                    .position(|c| c == name)
                    .ok_or_else(|| format!("company {name:?} is not in `companies`"))
            }
        }
    }
}

impl DatasetConfig {
    /// One `(weight name, bucket labels)` per block of `beta`, in order.
    pub fn blocks(&self) -> Vec<(&'static str, Vec<String>)> {
        self.weights
            .iter()
            .map(|w| (w.name(), w.bucket_labels(&self.zones, &self.companies)))
            .collect()
    }

    /// Number of buckets of each block of `beta`.
    pub fn block_sizes(&self) -> Vec<usize> {
        self.blocks()
            .iter()
            .map(|(_, labels)| labels.len())
            .collect()
    }

    /// The bucket of each block a ride goes into: the Mastic measurement for `beta`.
    pub fn buckets(&self, ride: &Ride) -> Result<Vec<usize>, String> {
        self.weights
            .iter()
            .map(|w| w.bucket(ride, &self.zones, &self.companies))
            .collect()
    }

    /// The `(lat, lon)` of a ride that is encoded as `alpha`.
    pub fn alpha_point(&self, ride: &Ride) -> Result<(f64, f64), String> {
        match self.alpha {
            Alpha::Pickup => Ok((ride.start_location_lat, ride.start_location_long)),
            Alpha::Dropoff => Ok((
                column(ride.end_location_lat, "end_location_lat")?,
                column(ride.end_location_long, "end_location_long")?,
            )),
        }
    }

    /// Check the weights against each other and against `hist_buckets`.
    pub fn check_weights(&self, hist_buckets: usize) -> Result<(), String> {
        for weight in &self.weights {
            weight.validate()?;
        }
        let uses_zones = self.weights.contains(&Weight::DropoffZone)
            || self.weights.contains(&Weight::PickupZone);
        if uses_zones && self.zones.is_empty() {
            return Err(
                "weights dropoff_zone and pickup_zone need at least one [[dataset.zones]]".into(),
            );
        }
        if !uses_zones && !self.zones.is_empty() {
            return Err("[[dataset.zones]] given but no dropoff_zone or pickup_zone weight".into());
        }
        if self.weights.contains(&Weight::Company) && self.companies.is_empty() {
            return Err("weight company needs a non-empty `companies` list".into());
        }
        if !self.weights.contains(&Weight::Company) && !self.companies.is_empty() {
            return Err("`companies` given but no company weight".into());
        }
        let total: usize = self.block_sizes().iter().sum();
        if total != hist_buckets {
            return Err(format!(
                "hist_buckets = {hist_buckets} but weights {:?} need {total} ({:?})",
                self.weights,
                self.block_sizes()
            ));
        }
        Ok(())
    }
}

/// The encoded rides of a dataset.
pub struct LoadedInputs {
    /// `(alpha, buckets)` for every ride inside the encoded area, in file order.
    pub inputs: Vec<(Vec<bool>, Vec<usize>)>,

    /// Number of rides in the file.
    pub total: usize,

    /// Rides outside the area covered by the encoding. They are not sent, so they do not count
    /// towards the number of clients the threshold is computed from.
    pub dropped: usize,
}

/// Load the dataset and encode each ride as `(alpha, buckets)`, in file order. Every row's buckets
/// are computed, also for rides outside the area, so a ride that cannot be bucketed (e.g. a
/// missing or invalid hour) is always an error rather than silently dropped. Row numbers in errors
/// are as in the file: the header is row 1.
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
    dataset.check_weights(hist_buckets)?;

    let rides = load_rides(&dataset.path)?;
    let mut inputs = Vec::with_capacity(rides.len());
    for (i, ride) in rides.iter().enumerate() {
        let at_row = |e: String| format!("{}: row {}: {e}", dataset.path, i + 2);
        let buckets = dataset.buckets(ride).map_err(at_row)?;
        let (lat, lon) = dataset.alpha_point(ride).map_err(at_row)?;
        if let Some(alpha) = encoder.encode(lat, lon) {
            inputs.push((alpha, buckets));
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

    const HOUR24: Weight = Weight::HourOfDay { buckets: HOURS };

    fn ride() -> Ride {
        Ride {
            start_location_lat: 30.27,
            start_location_long: -97.74,
            ..Default::default()
        }
    }

    #[test]
    fn hour_of_day_uses_the_hour() {
        let r = Ride {
            hour_of_day: Some(17),
            ..ride()
        };
        assert_eq!(HOUR24.bucket(&r, &[], &[]), Ok(17));
        assert_eq!(HOUR24.bucket_labels(&[], &[]).len(), 24);
        let bad = Ride {
            hour_of_day: Some(24),
            ..ride()
        };
        assert!(HOUR24.bucket(&bad, &[], &[]).is_err());
        assert!(HOUR24.bucket(&ride(), &[], &[]).is_err());

        // The drop-off hour reads its own column, not the pickup hour.
        let late = Ride {
            dropoff_hour: Some(18),
            ..r
        };
        assert_eq!(Weight::DropoffHour.bucket(&late, &[], &[]), Ok(18));
        assert_eq!(Weight::DropoffHour.bucket_labels(&[], &[]).len(), 24);
        assert!(Weight::DropoffHour.bucket(&ride(), &[], &[]).is_err());
    }

    #[test]
    fn car_category_accepts_both_spellings() {
        let with = |c: &str| Ride {
            requested_car_category: Some(c.into()),
            ..ride()
        };
        assert_eq!(
            Weight::CarCategory.bucket(&with("b'REGULAR'"), &[], &[]),
            Ok(0)
        );
        assert_eq!(Weight::CarCategory.bucket(&with("SUV"), &[], &[]), Ok(1));
        assert_eq!(
            Weight::CarCategory.bucket(&with("b'LUXURY'"), &[], &[]),
            Ok(3)
        );
        assert!(Weight::CarCategory
            .bucket(&with("b'TRUCK'"), &[], &[])
            .is_err());
    }

    #[test]
    fn surge_splits_at_one() {
        let with = |s: f64| Ride {
            surge_factor: Some(s),
            ..ride()
        };
        assert_eq!(Weight::Surge.bucket(&with(1.0), &[], &[]), Ok(0));
        assert_eq!(Weight::Surge.bucket(&with(1.25), &[], &[]), Ok(1));
    }

    fn zone(name: &str, lat: f64, lon: f64, radius_km: f64) -> Zone {
        Zone {
            name: name.into(),
            lat,
            lon,
            radius_km,
        }
    }

    fn to(lat: f64, lon: f64) -> Ride {
        Ride {
            end_location_lat: Some(lat),
            end_location_long: Some(lon),
            ..ride()
        }
    }

    #[test]
    fn dropoff_zone_picks_the_first_containing_zone() {
        let zones = [
            zone("airport", 30.197, -97.666, 2.0),
            zone("wide", 30.197, -97.666, 50.0),
        ];
        let w = Weight::DropoffZone;
        assert_eq!(w.bucket_labels(&zones, &[]), ["airport", "wide", "other"]);
        assert_eq!(w.bucket(&to(30.200, -97.670), &zones, &[]), Ok(0));
        // About 8 km north of the airport: only the wide circle.
        assert_eq!(w.bucket(&to(30.270, -97.666), &zones, &[]), Ok(1));
        assert_eq!(w.bucket(&to(31.5, -97.666), &zones, &[]), Ok(2));
        assert!(w.bucket(&ride(), &zones, &[]).is_err());
    }

    fn dataset(weights: Vec<Weight>, zones: Vec<Zone>) -> DatasetConfig {
        DatasetConfig {
            path: String::new(),
            encoding: Encoding::MosaicGrid,
            alpha: Alpha::Pickup,
            weights,
            zones,
            companies: vec![],
        }
    }

    #[test]
    fn several_weights_give_one_bucket_per_block() {
        let d = dataset(
            vec![HOUR24, Weight::Surge, Weight::DropoffZone],
            vec![zone("airport", 30.197, -97.666, 2.0)],
        );
        assert_eq!(d.block_sizes(), [24, 2, 2]);
        assert!(d.check_weights(28).is_ok());
        assert!(d.check_weights(24).is_err());
        let r = Ride {
            hour_of_day: Some(20),
            surge_factor: Some(1.5),
            ..to(30.200, -97.670)
        };
        assert_eq!(d.buckets(&r), Ok(vec![20, 1, 0]));
        // A missing column in any block fails the whole ride.
        let no_surge = Ride {
            surge_factor: None,
            ..r
        };
        assert!(d.buckets(&no_surge).is_err());
    }

    #[test]
    fn zones_and_dropoff_weight_go_together() {
        let zones = vec![zone("airport", 30.197, -97.666, 2.0)];
        assert!(dataset(vec![Weight::DropoffZone], vec![])
            .check_weights(1)
            .is_err());
        assert!(dataset(vec![Weight::Surge], zones)
            .check_weights(2)
            .is_err());
    }

    #[test]
    fn company_uses_the_configured_order() {
        let companies: Vec<String> = ["Uber", "Lyft", "Waymo"].map(String::from).into();
        let with = |c: &str| Ride {
            company: Some(c.into()),
            ..ride()
        };
        let w = Weight::Company;
        assert_eq!(w.bucket_labels(&[], &companies), companies);
        assert_eq!(w.bucket(&with("Uber"), &[], &companies), Ok(0));
        assert_eq!(w.bucket(&with("Waymo"), &[], &companies), Ok(2));
        assert!(w.bucket(&with("Juno"), &[], &companies).is_err());
        assert!(w.bucket(&ride(), &[], &companies).is_err());

        let mut d = dataset(vec![Weight::Company], vec![]);
        assert!(d.check_weights(3).is_err());
        d.companies = companies;
        assert!(d.check_weights(3).is_ok());
        assert!(dataset(vec![Weight::Surge], vec![])
            .check_weights(2)
            .is_ok());
    }

    #[test]
    fn pickup_zone_uses_the_pickup_and_alpha_can_be_the_dropoff() {
        let zones = vec![zone("airport", 30.197, -97.666, 2.0)];
        // Picked up at the airport, dropped off far away.
        let r = Ride {
            start_location_lat: 30.200,
            start_location_long: -97.670,
            ..to(31.5, -97.666)
        };
        assert_eq!(Weight::PickupZone.bucket(&r, &zones, &[]), Ok(0));
        assert_eq!(Weight::DropoffZone.bucket(&r, &zones, &[]), Ok(1));
        assert_eq!(
            Weight::PickupZone.bucket_labels(&zones, &[]),
            ["airport", "other"]
        );

        let mut d = dataset(vec![Weight::PickupZone], zones);
        assert!(d.check_weights(2).is_ok());
        assert_eq!(d.alpha_point(&r), Ok((30.200, -97.670)));
        d.alpha = Alpha::Dropoff;
        assert_eq!(d.alpha_point(&r), Ok((31.5, -97.666)));
        assert!(d.alpha_point(&ride()).is_err());
    }

    #[test]
    fn haversine_matches_a_known_distance() {
        // One degree of latitude is about 111.2 km.
        assert!((haversine_km(30.0, -97.0, 31.0, -97.0) - 111.19).abs() < 0.05);
    }

    // ---- Hour blocks and the `hour` column (from the parking branch) ----

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

    fn dataset_at(path: &str, weight: Weight) -> DatasetConfig {
        DatasetConfig {
            path: path.into(),
            encoding: morton_austin(),
            alpha: Alpha::Pickup,
            weights: vec![weight],
            zones: vec![],
            companies: vec![],
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

    fn ride_at_hour(hour: Option<i64>) -> Ride {
        Ride {
            start_location_lat: 30.0,
            start_location_long: -97.5,
            hour,
            ..Default::default()
        }
    }

    #[test]
    fn hour_buckets_for_24_6_and_1() {
        let hours = 0..HOURS;
        assert_eq!(
            hours
                .clone()
                .map(|h| hour_bucket(h, 24))
                .collect::<Vec<_>>(),
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
        let b = |w: Weight, r: &Ride| w.bucket(r, &[], &[]);
        assert_eq!(b(w24, &ride_at_hour(Some(0))), Ok(0));
        assert_eq!(b(w24, &ride_at_hour(Some(23))), Ok(23));
        assert_eq!(b(w6, &ride_at_hour(Some(13))), Ok(3));
        assert_eq!(b(w6, &ride_at_hour(Some(23))), Ok(5));
        assert!(b(w24, &ride_at_hour(None)).is_err());
        assert!(b(w24, &ride_at_hour(Some(24))).is_err());
        assert!(b(w24, &ride_at_hour(Some(-1))).is_err());
        // Constant ignores the hour, also a missing or invalid one.
        assert_eq!(b(Weight::Constant, &ride_at_hour(None)), Ok(0));
        assert_eq!(b(Weight::Constant, &ride_at_hour(Some(99))), Ok(0));
    }

    #[test]
    fn hour_of_day_reads_either_hour_column_but_not_both() {
        let w6 = Weight::HourOfDay { buckets: 6 };
        let city = Ride {
            hour_of_day: Some(13),
            ..ride()
        };
        assert_eq!(w6.bucket(&city, &[], &[]), Ok(3));
        let both = Ride {
            hour: Some(13),
            ..city
        };
        assert!(w6.bucket(&both, &[], &[]).is_err());
    }

    #[test]
    fn bucket_counts_must_divide_24() {
        for buckets in [1, 2, 3, 4, 6, 8, 12, 24] {
            let w = Weight::HourOfDay { buckets };
            assert!(w.validate().is_ok(), "{buckets}");
            assert_eq!(w.bucket_labels(&[], &[]).len(), buckets);
            assert!(dataset_at("", w).check_weights(buckets).is_ok());
        }
        for buckets in [0, 5, 7, 25, 48] {
            let w = Weight::HourOfDay { buckets };
            assert!(w.validate().is_err(), "{buckets}");
            assert!(dataset_at("", w).check_weights(buckets).is_err());
        }
        assert_eq!(
            Weight::HourOfDay { buckets: 6 }.bucket_labels(&[], &[]),
            ["00-04h", "04-08h", "08-12h", "12-16h", "16-20h", "20-24h"]
        );
        assert_eq!(Weight::Constant.bucket_labels(&[], &[]).len(), 1);
        assert!(Weight::Constant.validate().is_ok());
    }

    #[test]
    fn file_without_hour_loads_with_constant_only() {
        let loaded = load_inputs(&dataset_at(TEN_LOCATIONS, Weight::Constant), 20, 1).unwrap();
        assert_eq!((loaded.total, loaded.dropped), (165, 0));
        assert!(loaded.inputs.iter().all(|(_, b)| *b == [0]));

        let err = load_inputs(&dataset_at(TEN_LOCATIONS, HOUR24), 20, 24)
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
            let loaded = load_inputs(&dataset_at(csv.path(), weight), 20, hist_buckets).unwrap();
            assert_eq!((loaded.total, loaded.dropped), (5, 1));
            loaded.inputs.iter().map(|(_, b)| b[0]).collect::<Vec<_>>()
        };
        assert_eq!(buckets_of(HOUR24, 24), [0, 7, 13, 23]);
        assert_eq!(
            buckets_of(Weight::HourOfDay { buckets: 6 }, 6),
            [0, 1, 3, 5]
        );
        assert_eq!(buckets_of(Weight::Constant, 1), [0, 0, 0, 0]);
        // hist_buckets must match the weight.
        assert!(load_inputs(&dataset_at(csv.path(), HOUR24), 20, 6).is_err());
        assert!(load_inputs(
            &dataset_at(csv.path(), Weight::HourOfDay { buckets: 5 }),
            20,
            5
        )
        .is_err());
    }

    #[test]
    fn missing_or_invalid_hours_are_errors() {
        for (bad, row) in [
            ("30.2,-97.7,\n", "row 3"),
            ("30.2,-97.7,24\n", "row 3"),
            ("30.2,-97.7,-1\n", "row 3"),
        ] {
            let csv = TempCsv::new(&format!(
                "start_location_lat,start_location_long,hour\n30.2,-97.7,1\n{bad}"
            ));
            let err = load_inputs(&dataset_at(csv.path(), HOUR24), 20, 24)
                .err()
                .unwrap()
                .to_string();
            assert!(err.contains(row), "{bad:?}: {err}");
        }
        // An invalid hour also fails for a ride outside the encoded area.
        let csv = TempCsv::new("start_location_lat,start_location_long,hour\n40.0,-97.7,30\n");
        assert!(load_inputs(&dataset_at(csv.path(), HOUR24), 20, 24).is_err());
    }

    #[test]
    fn weight_config_syntax() {
        #[derive(Deserialize)]
        struct Wrapper {
            dataset: DatasetConfig,
        }
        let parse = |weight_line: &str| {
            let text =
                format!("[dataset]\npath = \"x.csv\"\nencoding = \"mosaic_grid\"\n{weight_line}\n");
            toml::from_str::<Wrapper>(&text).map(|w| w.dataset.weights)
        };
        // The parking configs: one weight with its number of hour blocks.
        assert_eq!(
            parse("weight.hour_of_day = { buckets = 24 }").unwrap(),
            [HOUR24]
        );
        assert_eq!(
            parse("weight = { hour_of_day = { buckets = 6 } }").unwrap(),
            [Weight::HourOfDay { buckets: 6 }]
        );
        assert_eq!(parse("weight = \"constant\"").unwrap(), [Weight::Constant]);
        assert_eq!(parse("").unwrap(), [Weight::Constant]);
        // The city configs: names, alone or in a list; a bare `hour_of_day` is 24 buckets.
        assert_eq!(parse("weight = \"hour_of_day\"").unwrap(), [HOUR24]);
        assert_eq!(
            parse("weight = [\"company\", \"hour_of_day\", \"surge\"]").unwrap(),
            [Weight::Company, HOUR24, Weight::Surge]
        );
        // Both styles in one list.
        assert_eq!(
            parse("weight = [\"surge\", { hour_of_day = { buckets = 6 } }]").unwrap(),
            [Weight::Surge, Weight::HourOfDay { buckets: 6 }]
        );
        assert!(parse("weight = []").is_err());
        assert!(parse("weight = \"hour_of_week\"").is_err());
        assert!(parse("weight = { hour_of_day = { buckets = 6, extra = 1 } }").is_err());
    }
}
