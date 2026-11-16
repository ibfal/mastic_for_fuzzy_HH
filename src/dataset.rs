//! Load ride-sharing pickups and turn them into Mastic inputs `(alpha, bucket)`.

use std::error::Error;

use serde::Deserialize;

use crate::{
    config::{DatasetConfig, Weight, Zone},
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
    #[serde(default)]
    pub hour_of_day: Option<usize>,
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

impl Weight {
    /// Name used in printed and exported results.
    pub fn name(&self) -> &'static str {
        match self {
            Weight::Constant => "constant",
            Weight::HourOfDay => "hour_of_day",
            Weight::CarCategory => "car_category",
            Weight::Surge => "surge",
            Weight::DropoffZone => "dropoff_zone",
            Weight::Company => "company",
        }
    }

    /// Name of each bucket, in order. `zones` are the dataset's drop-off zones and `companies`
    /// its company names.
    pub fn bucket_labels(&self, zones: &[Zone], companies: &[String]) -> Vec<String> {
        match self {
            Weight::Constant => vec!["rides".into()],
            Weight::HourOfDay => (0..24).map(|h| format!("{h:02}h")).collect(),
            Weight::CarCategory => CAR_CATEGORIES.iter().map(|c| c.to_string()).collect(),
            Weight::Surge => vec!["no surge".into(), "surge".into()],
            Weight::DropoffZone => zones
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
            Weight::HourOfDay => {
                let hour = column(ride.hour_of_day, "hour_of_day")?;
                if hour < 24 {
                    Ok(hour)
                } else {
                    Err(format!("hour_of_day = {hour} is not in 0..24"))
                }
            }
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

    /// Check the weights against each other and against `hist_buckets`.
    pub fn check_weights(&self, hist_buckets: usize) -> Result<(), String> {
        if self.weights.contains(&Weight::DropoffZone) && self.zones.is_empty() {
            return Err("weight dropoff_zone needs at least one [[dataset.zones]]".into());
        }
        if !self.weights.contains(&Weight::DropoffZone) && !self.zones.is_empty() {
            return Err("[[dataset.zones]] given but no dropoff_zone weight".into());
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

/// Load the dataset and encode each ride as `(alpha, buckets)`, in file order. Fails if any ride
/// inside the encoded area cannot be bucketed, rather than silently dropping it.
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
    for (row, ride) in rides.iter().enumerate() {
        let Some(alpha) = encoder.encode(ride.start_location_lat, ride.start_location_long) else {
            continue;
        };
        let buckets = dataset
            .buckets(ride)
            .map_err(|e| format!("{} row {}: {e}", dataset.path, row + 1))?;
        inputs.push((alpha, buckets));
    }
    Ok(LoadedInputs {
        total: rides.len(),
        dropped: rides.len() - inputs.len(),
        inputs,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::config::Encoding;

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
        assert_eq!(Weight::HourOfDay.bucket(&r, &[], &[]), Ok(17));
        assert_eq!(Weight::HourOfDay.bucket_labels(&[], &[]).len(), 24);
        let bad = Ride {
            hour_of_day: Some(24),
            ..ride()
        };
        assert!(Weight::HourOfDay.bucket(&bad, &[], &[]).is_err());
        assert!(Weight::HourOfDay.bucket(&ride(), &[], &[]).is_err());
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
            weights,
            zones,
            companies: vec![],
        }
    }

    #[test]
    fn several_weights_give_one_bucket_per_block() {
        let d = dataset(
            vec![Weight::HourOfDay, Weight::Surge, Weight::DropoffZone],
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
    fn haversine_matches_a_known_distance() {
        // One degree of latitude is about 111.2 km.
        assert!((haversine_km(30.0, -97.0, 31.0, -97.0) - 111.19).abs() < 0.05);
    }
}
