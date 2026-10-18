//! Encoders that turn a pickup location into the Mastic input string `alpha`.
//!
//! Every encoder emits the coarsest bit first, so each prefix of `alpha` names a region that
//! contains all of its extensions. Mastic's tree crawl then finds heavy regions top-down.

use h3o::{CellIndex, LatLng, Resolution};

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
        Encoding::H3 { prefix, resolution } => Box::new(H3::new(prefix, *resolution)),
        Encoding::Morton {
            min_lat,
            max_lat,
            min_lon,
            max_lon,
            axis_bits,
        } => Box::new(Morton::new(
            *min_lat, *max_lat, *min_lon, *max_lon, *axis_bits,
        )),
    }
}

/// Morton (Z-order) code of two `axis_bits`-bit cell indices: interleave the axes (lon, lat, lon,
/// ...), most significant bit first, so each prefix names an aligned block of cells.
pub fn interleave(lat_idx: u32, lon_idx: u32, axis_bits: usize) -> Vec<bool> {
    (0..axis_bits)
        .rev()
        .flat_map(|i| [(lon_idx >> i) & 1 == 1, (lat_idx >> i) & 1 == 1])
        .collect()
}

/// Inverse of [`interleave`]: the `(lat_idx, lon_idx)` named by a bit string of even length. A
/// prefix of `2j` bits gives the indices of its block at `j` bits per axis.
pub fn deinterleave(bits: &[bool]) -> (u32, u32) {
    assert_eq!(
        bits.len() % 2,
        0,
        "a Morton code has an even number of bits"
    );
    bits.chunks(2).fold((0, 0), |(lat, lon), pair| {
        ((lat << 1) | pair[1] as u32, (lon << 1) | pair[0] as u32)
    })
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
        interleave(lat_grid, lon_grid, Self::AXIS_BITS)
    }

    pub fn bits_to_grid(bits: &[bool]) -> (u32, u32) {
        assert_eq!(bits.len(), 2 * Self::AXIS_BITS);
        deinterleave(bits)
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

/// Uber's H3 hexagonal grid. A cell index stores one 3-bit digit per resolution below its base
/// cell, so a parent's digits are a prefix of its children's. The digits of a fixed `prefix` cell
/// that covers the whole dataset are not sent.
///
/// Aperture-7 hexagons do not nest exactly: a parent's count is the sum of its 7 children, whose
/// union only approximates the parent hexagon. Tree levels that fall inside a 3-bit digit have no
/// geometric meaning; only every third level is a real resolution.
pub struct H3 {
    prefix: CellIndex,
    resolution: Resolution,
}

impl H3 {
    /// Bit offset of the resolution field in an H3 index.
    const RES_OFFSET: u64 = 52;

    pub fn new(prefix: &str, resolution: u8) -> Self {
        let prefix: CellIndex = prefix
            .parse()
            .unwrap_or_else(|e| panic!("invalid H3 prefix {prefix:?}: {e}"));
        let resolution = Resolution::try_from(resolution)
            .unwrap_or_else(|e| panic!("invalid H3 resolution {resolution}: {e}"));
        assert!(
            resolution > prefix.resolution(),
            "H3 resolution must be finer than the prefix resolution"
        );
        Self { prefix, resolution }
    }

    /// Resolutions whose digits are sent, coarsest first.
    fn sent_resolutions(&self) -> impl Iterator<Item = u8> {
        u8::from(self.prefix.resolution()) + 1..=u8::from(self.resolution)
    }

    /// Bit offset of the digit for resolution `r` in an H3 index.
    fn digit_offset(r: u8) -> u64 {
        3 * (15 - r as u64)
    }

    pub fn cell(&self, bits: &[bool]) -> CellIndex {
        assert_eq!(bits.len(), self.depth());
        let mut index = u64::from(self.prefix);
        index &= !(0xf << Self::RES_OFFSET);
        index |= (u8::from(self.resolution) as u64) << Self::RES_OFFSET;
        for (r, digit) in self.sent_resolutions().zip(bits.chunks(3)) {
            let digit = digit.iter().fold(0, |v, &b| (v << 1) | b as u64);
            index &= !(0b111 << Self::digit_offset(r));
            index |= digit << Self::digit_offset(r);
        }
        CellIndex::try_from(index).expect("bits do not name a valid H3 cell")
    }
}

impl LocationEncoder for H3 {
    fn depth(&self) -> usize {
        3 * self.sent_resolutions().count()
    }

    fn encode(&self, lat: f64, lon: f64) -> Option<Vec<bool>> {
        let cell = LatLng::new(lat, lon).ok()?.to_cell(self.resolution);
        if cell.parent(self.prefix.resolution()) != Some(self.prefix) {
            return None;
        }
        let index = u64::from(cell);
        Some(
            self.sent_resolutions()
                .flat_map(|r| {
                    let digit = (index >> Self::digit_offset(r)) & 0b111;
                    (0..3).rev().map(move |i| (digit >> i) & 1 == 1)
                })
                .collect(),
        )
    }

    fn decode(&self, bits: &[bool]) -> (f64, f64) {
        let center = LatLng::from(self.cell(bits));
        (center.lat(), center.lng())
    }

    fn label(&self, bits: &[bool]) -> String {
        self.cell(bits).to_string()
    }
}

/// Morton (Z-order) code over a configurable bounding box. Each axis of `[min, max)` is split into
/// `2^axis_bits` equal cells and the two cell indices are interleaved with [`interleave`]. A prefix
/// of `2j` bits names an aligned block of `2^(axis_bits - j)` by `2^(axis_bits - j)` cells, so
/// counts nest exactly. Points outside the box are not encoded.
pub struct Morton {
    min_lat: f64,
    max_lat: f64,
    min_lon: f64,
    max_lon: f64,
    axis_bits: usize,
}

impl Morton {
    pub fn new(min_lat: f64, max_lat: f64, min_lon: f64, max_lon: f64, axis_bits: usize) -> Self {
        assert!(
            [min_lat, max_lat, min_lon, max_lon]
                .iter()
                .all(|v| v.is_finite())
                && min_lat < max_lat
                && min_lon < max_lon,
            "invalid Morton bounding box: lat [{min_lat}, {max_lat}), lon [{min_lon}, {max_lon})"
        );
        assert!(
            (1..=31).contains(&axis_bits),
            "Morton axis_bits must be between 1 and 31, got {axis_bits}"
        );
        Self {
            min_lat,
            max_lat,
            min_lon,
            max_lon,
            axis_bits,
        }
    }

    /// Index of the cell containing `v` on the axis `[min, max)`, or `None` outside it (including
    /// NaN). scripts/check_heavy_hitters.py evaluates the same expression in the same order, so
    /// both produce identical bits.
    fn axis_cell(v: f64, min: f64, max: f64, axis_bits: usize) -> Option<u32> {
        if !(min <= v && v < max) {
            return None;
        }
        let cells = (1u64 << axis_bits) as f64;
        let idx = ((v - min) / (max - min) * cells).floor() as u64;
        // Rounding can put a point just below `max` at index `cells`.
        Some(idx.min((1u64 << axis_bits) - 1) as u32)
    }

    /// The cell `(lat_idx, lon_idx)` containing a point, or `None` if it lies outside the box.
    pub fn to_cell(&self, lat: f64, lon: f64) -> Option<(u32, u32)> {
        Some((
            Self::axis_cell(lat, self.min_lat, self.max_lat, self.axis_bits)?,
            Self::axis_cell(lon, self.min_lon, self.max_lon, self.axis_bits)?,
        ))
    }

    /// Bounds `((min_lat, max_lat), (min_lon, max_lon))` of a cell.
    pub fn cell_box(&self, lat_idx: u32, lon_idx: u32) -> ((f64, f64), (f64, f64)) {
        let cells = (1u64 << self.axis_bits) as f64;
        let edge = |min: f64, max: f64, i: u32| min + (max - min) * i as f64 / cells;
        (
            (
                edge(self.min_lat, self.max_lat, lat_idx),
                edge(self.min_lat, self.max_lat, lat_idx + 1),
            ),
            (
                edge(self.min_lon, self.max_lon, lon_idx),
                edge(self.min_lon, self.max_lon, lon_idx + 1),
            ),
        )
    }

    fn cell_of(&self, bits: &[bool]) -> (u32, u32) {
        assert_eq!(bits.len(), self.depth());
        deinterleave(bits)
    }
}

impl LocationEncoder for Morton {
    fn depth(&self) -> usize {
        2 * self.axis_bits
    }

    fn encode(&self, lat: f64, lon: f64) -> Option<Vec<bool>> {
        let (lat_idx, lon_idx) = self.to_cell(lat, lon)?;
        Some(interleave(lat_idx, lon_idx, self.axis_bits))
    }

    fn decode(&self, bits: &[bool]) -> (f64, f64) {
        let (lat_idx, lon_idx) = self.cell_of(bits);
        let ((lat_lo, lat_hi), (lon_lo, lon_hi)) = self.cell_box(lat_idx, lon_idx);
        ((lat_lo + lat_hi) / 2.0, (lon_lo + lon_hi) / 2.0)
    }

    fn label(&self, bits: &[bool]) -> String {
        let (lat_idx, lon_idx) = self.cell_of(bits);
        format!("morton ({lat_idx}, {lon_idx})")
    }
}
