//! Encoders that turn a pickup location into the Mastic input string `alpha`.
//!
//! Every encoder emits the coarsest bit first, so each prefix of `alpha` names a region that
//! contains all of its extensions. Mastic's tree crawl then finds heavy regions top-down.

use crate::config::Encoding;

pub trait LocationEncoder: Sync {
    /// Number of bits produced by `encode`. Must equal `data_bits` in the config.
    fn depth(&self) -> usize;

    /// Encode a point, most significant (coarsest) bit first.
    fn encode(&self, lat: f64, lon: f64) -> Vec<bool>;

    /// Center `(lat, lon)` of the cell named by a full-length bit string.
    fn decode(&self, bits: &[bool]) -> (f64, f64);
}

pub fn encoder(encoding: &Encoding) -> Box<dyn LocationEncoder> {
    match encoding {
        Encoding::MosaicGrid => Box::new(MosaicGrid),
    }
}

/// The grid used by Mosaic for RideAustin: 0.001 degree cells, 11 bits per axis, with the origin
/// one degree south-west of downtown Austin. Using it keeps cells identical to Mosaic's.
pub struct MosaicGrid;

impl MosaicGrid {
    pub const MIN_LAT: f64 = 30.267 - 1.0;
    pub const MIN_LON: f64 = -97.743 - 1.0;
    pub const SCALE: f64 = 1000.0;
    pub const AXIS_BITS: usize = 11;
    pub const GRID_SIZE: u32 = 2000;

    /// Same as Mosaic's `geo_to_grid`: round to the nearest cell and clamp to the grid.
    pub fn to_grid(lat: f64, lon: f64) -> (u32, u32) {
        let to_cell = |v: f64| (v.round().max(0.0) as u32).min(Self::GRID_SIZE - 1);
        (
            to_cell((lat - Self::MIN_LAT) * Self::SCALE),
            to_cell((lon - Self::MIN_LON) * Self::SCALE),
        )
    }

    pub fn from_grid(lat_grid: u32, lon_grid: u32) -> (f64, f64) {
        (
            Self::MIN_LAT + lat_grid as f64 / Self::SCALE,
            Self::MIN_LON + lon_grid as f64 / Self::SCALE,
        )
    }

    /// Interleave the two axes (lon, lat, lon, ...), most significant bit first, like geohash.
    pub fn grid_to_bits(lat_grid: u32, lon_grid: u32) -> Vec<bool> {
        (0..Self::AXIS_BITS)
            .rev()
            .flat_map(|i| [(lon_grid >> i) & 1 == 1, (lat_grid >> i) & 1 == 1])
            .collect()
    }

    pub fn bits_to_grid(bits: &[bool]) -> (u32, u32) {
        assert_eq!(bits.len(), 2 * Self::AXIS_BITS);
        bits.chunks(2).fold((0, 0), |(lat, lon), pair| {
            ((lat << 1) | pair[1] as u32, (lon << 1) | pair[0] as u32)
        })
    }
}

impl LocationEncoder for MosaicGrid {
    fn depth(&self) -> usize {
        2 * Self::AXIS_BITS
    }

    fn encode(&self, lat: f64, lon: f64) -> Vec<bool> {
        let (lat_grid, lon_grid) = Self::to_grid(lat, lon);
        Self::grid_to_bits(lat_grid, lon_grid)
    }

    fn decode(&self, bits: &[bool]) -> (f64, f64) {
        let (lat_grid, lon_grid) = Self::bits_to_grid(bits);
        Self::from_grid(lat_grid, lon_grid)
    }
}
