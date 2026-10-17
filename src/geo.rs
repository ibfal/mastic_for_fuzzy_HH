//! Encoders that turn a pickup location into the Mastic input string `alpha`.
//!
//! Every encoder emits the coarsest bit first, so each prefix of `alpha` names a region that
//! contains all of its extensions. Mastic's tree crawl then finds heavy regions top-down.

use crate::config::Encoding;

pub trait LocationEncoder: Sync {
    /// Number of bits produced by `encode`. Must equal `data_bits` in the config.
    fn depth(&self) -> usize;

    /// Encode a point, most significant (coarsest) bit first. Returns `None` if the point lies
    /// outside the area the encoder covers.
    fn encode(&self, lat: f64, lon: f64) -> Option<Vec<bool>>;

    /// Center `(lat, lon)` of the cell named by a full-length bit string.
    fn decode(&self, bits: &[bool]) -> (f64, f64);

    /// Human-readable name of the cell named by a full-length bit string.
    fn label(&self, bits: &[bool]) -> String;
}

pub fn encoder(encoding: &Encoding) -> Box<dyn LocationEncoder> {
    match encoding {
        Encoding::MosaicGrid => Box::new(MosaicGrid),
        Encoding::Geohash { prefix, bits } => Box::new(Geohash::new(prefix, *bits)),
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

    fn encode(&self, lat: f64, lon: f64) -> Option<Vec<bool>> {
        // Points outside the grid are clamped to its edge, as Mosaic does.
        let (lat_grid, lon_grid) = Self::to_grid(lat, lon);
        Some(Self::grid_to_bits(lat_grid, lon_grid))
    }

    fn decode(&self, bits: &[bool]) -> (f64, f64) {
        let (lat_grid, lon_grid) = Self::bits_to_grid(bits);
        Self::from_grid(lat_grid, lon_grid)
    }

    fn label(&self, bits: &[bool]) -> String {
        let (lat_grid, lon_grid) = Self::bits_to_grid(bits);
        format!("grid ({lat_grid}, {lon_grid})")
    }
}

const GEOHASH_BASE32: &[u8; 32] = b"0123456789bcdefghjkmnpqrstuvwxyz";

/// Standard geohash: halve the world alternately by longitude and latitude, longitude first.
/// The bits of a fixed `prefix` that covers the whole dataset are not sent.
pub struct Geohash {
    prefix: Vec<bool>,
    bits: usize,
}

impl Geohash {
    pub fn new(prefix: &str, bits: usize) -> Self {
        Self {
            prefix: Self::base32_to_bits(prefix),
            bits,
        }
    }

    /// The first `n` geohash bits of a point.
    pub fn point_to_bits(lat: f64, lon: f64, n: usize) -> Vec<bool> {
        let (mut lat_range, mut lon_range) = ((-90.0, 90.0), (-180.0, 180.0));
        (0..n)
            .map(|i| {
                let (range, v) = if i % 2 == 0 {
                    (&mut lon_range, lon)
                } else {
                    (&mut lat_range, lat)
                };
                let mid = (range.0 + range.1) / 2.0;
                if v >= mid {
                    range.0 = mid;
                    true
                } else {
                    range.1 = mid;
                    false
                }
            })
            .collect()
    }

    /// Bounding box `((min_lat, max_lat), (min_lon, max_lon))` of a geohash bit string.
    pub fn bits_to_box(bits: &[bool]) -> ((f64, f64), (f64, f64)) {
        let (mut lat_range, mut lon_range) = ((-90.0, 90.0), (-180.0, 180.0));
        for (i, &bit) in bits.iter().enumerate() {
            let range: &mut (f64, f64) = if i % 2 == 0 {
                &mut lon_range
            } else {
                &mut lat_range
            };
            let mid = (range.0 + range.1) / 2.0;
            if bit {
                range.0 = mid;
            } else {
                range.1 = mid;
            }
        }
        (lat_range, lon_range)
    }

    pub fn base32_to_bits(s: &str) -> Vec<bool> {
        s.bytes()
            .flat_map(|c| {
                let v = GEOHASH_BASE32
                    .iter()
                    .position(|&b| b == c)
                    .unwrap_or_else(|| panic!("invalid geohash character {:?}", c as char));
                (0..5).rev().map(move |i| (v >> i) & 1 == 1)
            })
            .collect()
    }

    /// Base32 string of the whole 5-bit groups in `bits`; any leftover bits are shown in brackets.
    pub fn bits_to_base32(bits: &[bool]) -> String {
        let chunks = bits.chunks_exact(5);
        let rest = chunks.remainder();
        let mut s: String = chunks
            .map(|c| GEOHASH_BASE32[c.iter().fold(0, |v, &b| (v << 1) | b as usize)] as char)
            .collect();
        if !rest.is_empty() {
            s.push_str(&format!("[{}]", crate::bits_to_bitstring(rest)));
        }
        s
    }

    fn full_bits(&self, bits: &[bool]) -> Vec<bool> {
        assert_eq!(bits.len(), self.bits);
        [&self.prefix[..], bits].concat()
    }
}

impl LocationEncoder for Geohash {
    fn depth(&self) -> usize {
        self.bits
    }

    fn encode(&self, lat: f64, lon: f64) -> Option<Vec<bool>> {
        let full = Self::point_to_bits(lat, lon, self.prefix.len() + self.bits);
        full.starts_with(&self.prefix)
            .then(|| full[self.prefix.len()..].to_vec())
    }

    fn decode(&self, bits: &[bool]) -> (f64, f64) {
        let (lat, lon) = Self::bits_to_box(&self.full_bits(bits));
        ((lat.0 + lat.1) / 2.0, (lon.0 + lon.1) / 2.0)
    }

    fn label(&self, bits: &[bool]) -> String {
        Self::bits_to_base32(&self.full_bits(bits))
    }
}
