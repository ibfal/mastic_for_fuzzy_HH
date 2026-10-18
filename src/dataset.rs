//! Load ride-sharing pickups and turn them into Mastic inputs `(alpha, bucket)`.

use std::error::Error;

use serde::Deserialize;

use crate::{
    config::{DatasetConfig, Weight},
    geo,
};

/// One CSV row. Columns are matched by header name; all other columns are ignored.
#[derive(Deserialize, Debug)]
pub struct Ride {
    pub start_location_lat: f64,
    pub start_location_long: f64,
}

pub fn load_rides(path: &str) -> Result<Vec<Ride>, Box<dyn Error>> {
    let mut reader = csv::Reader::from_path(path)?;
    let rides = reader.deserialize().collect::<Result<Vec<Ride>, _>>()?;
    Ok(rides)
}

impl Weight {
    /// Number of histogram buckets this weight needs. Must equal `hist_buckets` in the config.
    pub fn num_buckets(&self) -> usize {
        match self {
            Weight::Constant => 1,
        }
    }

    /// The histogram bucket a ride contributes to.
    pub fn bucket(&self, _ride: &Ride) -> usize {
        match self {
            Weight::Constant => 0,
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

/// Load the dataset and encode each ride as `(alpha, bucket)`, in file order.
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
    if dataset.weight.num_buckets() != hist_buckets {
        return Err(format!(
            "hist_buckets = {hist_buckets} but weight {:?} needs {}",
            dataset.weight,
            dataset.weight.num_buckets()
        )
        .into());
    }

    let rides = load_rides(&dataset.path)?;
    let inputs = rides
        .iter()
        .filter_map(|ride| {
            let alpha = encoder.encode(ride.start_location_lat, ride.start_location_long)?;
            Some((alpha, dataset.weight.bucket(ride)))
        })
        .collect::<Vec<_>>();
    Ok(LoadedInputs {
        total: rides.len(),
        dropped: rides.len() - inputs.len(),
        inputs,
    })
}
