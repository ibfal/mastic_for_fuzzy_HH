use h3o::Resolution;
use mastic::geo::{deinterleave, interleave, Geohash, LocationEncoder, Morton, MosaicGrid, H3};

#[test]
fn mosaic_grid_matches_mosaic() {
    // First row of Mosaic's sample_busiest_day.csv and its entry in start_busiest_day.json.
    assert_eq!(MosaicGrid::to_grid(30.265, -97.732), (998, 1011));
}

#[test]
fn mosaic_grid_round_trip() {
    let enc = MosaicGrid;
    for &(lat, lon) in &[
        (30.265, -97.732),
        (30.203, -97.667),
        (29.998, -98.026),
        (30.631, -97.355),
    ] {
        let bits = enc.encode(lat, lon).unwrap();
        assert_eq!(bits.len(), enc.depth());
        let (lat2, lon2) = enc.decode(&bits);
        assert!((lat - lat2).abs() < 1e-9 && (lon - lon2).abs() < 1e-9);
    }
}

#[test]
fn mosaic_grid_prefix_is_enclosing_region() {
    // Grid cells (998, 1011) and (999, 1009) lie in the same 8x8 block (top 8 bits of each axis
    // agree), so their encodings share the first 16 bits. A far-away pickup does not.
    let a = MosaicGrid::grid_to_bits(998, 1011);
    let b = MosaicGrid::grid_to_bits(999, 1009);
    let far = MosaicGrid::grid_to_bits(1364, 1388);
    assert_eq!(a[..16], b[..16]);
    assert_ne!(a[..16], far[..16]);
    assert_eq!(MosaicGrid::bits_to_grid(&a), (998, 1011));
}

#[test]
fn geohash_matches_reference() {
    // Example from https://en.wikipedia.org/wiki/Geohash.
    let bits = Geohash::point_to_bits(57.64911, 10.40744, 55);
    assert_eq!(Geohash::bits_to_base32(&bits), "u4pruydqqvj");
    assert_eq!(Geohash::base32_to_bits("u4pruydqqvj"), bits);
}

#[test]
fn geohash_drops_shared_prefix() {
    // Downtown Austin is geohash 9v6s0j9; only the 20 bits after "9v6" are sent.
    let enc = Geohash::new("9v6", 20);
    let bits = enc.encode(30.265, -97.732).unwrap();
    assert_eq!(bits.len(), enc.depth());
    assert_eq!(enc.label(&bits), "9v6s0j9");
    let (lat, lon) = enc.decode(&bits);
    assert!((lat - 30.265).abs() < 0.001 && (lon - -97.732).abs() < 0.001);
}

#[test]
fn geohash_rejects_points_outside_prefix() {
    // Dallas is not inside 9v6.
    assert!(Geohash::new("9v6", 20).encode(32.78, -96.80).is_none());
}

#[test]
fn geohash_prefix_is_enclosing_region() {
    // A shorter geohash is a prefix of a longer one, and its box contains the point.
    let long = Geohash::point_to_bits(30.265, -97.732, 35);
    let short = Geohash::point_to_bits(30.265, -97.732, 20);
    assert_eq!(long[..20], short[..]);
    let ((lat_lo, lat_hi), (lon_lo, lon_hi)) = Geohash::bits_to_box(&short);
    assert!(lat_lo <= 30.265 && 30.265 < lat_hi && lon_lo <= -97.732 && -97.732 < lon_hi);
}

#[test]
fn h3_matches_reference() {
    // Downtown Austin at resolution 9, as given by Uber's reference library (h3-py 4.5).
    let enc = H3::new("82489ffffffffff", 9);
    let bits = enc.encode(30.265, -97.732).unwrap();
    assert_eq!(bits.len(), 21);
    assert_eq!(enc.label(&bits), "89489e3464bffff");
    let (lat, lon) = enc.decode(&bits);
    assert!((lat - 30.263433).abs() < 1e-5 && (lon - -97.731642).abs() < 1e-5);
}

#[test]
fn h3_prefix_is_parent_cell() {
    // The first 3 * k bits of a resolution 9 cell name its parent at resolution 2 + k.
    let fine_enc = H3::new("82489ffffffffff", 9);
    let fine = fine_enc.encode(30.265, -97.732).unwrap();
    let parent = fine_enc.cell(&fine).parent(Resolution::Seven).unwrap();
    assert_eq!(
        H3::new("82489ffffffffff", 7).label(&fine[..15]),
        parent.to_string()
    );
}

#[test]
fn h3_cells_do_not_nest_exactly() {
    // The resolution 7 hexagon containing this point is not the parent of its resolution 9
    // cell (checked with h3-py 4.5), so prefix counts only approximate coarser hexagons.
    let fine_enc = H3::new("82489ffffffffff", 9);
    let fine = fine_enc.encode(30.265, -97.732).unwrap();
    assert_eq!(
        H3::new("82489ffffffffff", 7).label(&fine[..15]),
        "87489e346ffffff"
    );
    let coarse_enc = H3::new("82489ffffffffff", 7);
    let coarse = coarse_enc.encode(30.265, -97.732).unwrap();
    assert_eq!(coarse_enc.label(&coarse), "87489e355ffffff");
}

#[test]
fn h3_rejects_points_outside_prefix() {
    // Dallas is not inside 82489ffffffffff.
    assert!(H3::new("82489ffffffffff", 9)
        .encode(32.78, -96.80)
        .is_none());
}

/// The box of the ride-austin-*-morton.toml configs: 1.024 degrees, edges half a 0.001-degree
/// step off the data.
const MORTON_BOX: (f64, f64, f64, f64) = (29.9005, 30.9245, -98.0505, -97.0265);

/// Pickups from Mosaic's data, all inside both Morton boxes below.
const SAMPLE_POINTS: [(f64, f64); 4] = [
    (30.265, -97.732),
    (30.203, -97.667),
    (29.998, -98.026),
    (30.631, -97.355),
];

fn recommended_morton() -> Morton {
    let (min_lat, max_lat, min_lon, max_lon) = MORTON_BOX;
    Morton::new(min_lat, max_lat, min_lon, max_lon, 10)
}

/// Mosaic's grid as a Morton box: 2048 cells of 0.001 degrees whose edges lie half a step below
/// Mosaic's grid points, so flooring into a cell agrees with Mosaic's rounding to a point.
fn mosaic_equivalent_morton() -> Morton {
    Morton::new(29.2665, 29.2665 + 2.048, -98.7435, -98.7435 + 2.048, 11)
}

/// The largest f64 below `v`.
fn next_down(v: f64) -> f64 {
    if v > 0.0 {
        f64::from_bits(v.to_bits() - 1)
    } else {
        f64::from_bits(v.to_bits() + 1)
    }
}

#[test]
fn morton_interleave_known_vector() {
    // lat 0b10, lon 0b01: (lon bit 1, lat bit 1, lon bit 0, lat bit 0) = 0 1 1 0.
    let bits = interleave(0b10, 0b01, 2);
    assert_eq!(bits, vec![false, true, true, false]);
    assert_eq!(deinterleave(&bits), (0b10, 0b01));
}

#[test]
fn morton_matches_mosaic_grid() {
    let morton = mosaic_equivalent_morton();
    assert_eq!(morton.depth(), MosaicGrid.depth());
    for &(lat, lon) in &SAMPLE_POINTS {
        let bits = morton.encode(lat, lon).unwrap();
        assert_eq!(bits, MosaicGrid.encode(lat, lon).unwrap());
        let (m_lat, m_lon) = morton.decode(&bits);
        let (g_lat, g_lon) = MosaicGrid.decode(&bits);
        assert!((m_lat - g_lat).abs() < 1e-9 && (m_lon - g_lon).abs() < 1e-9);
    }
    // First row of Mosaic's sample_busiest_day.csv and its entry in start_busiest_day.json.
    assert_eq!(morton.to_cell(30.265, -97.732), Some((998, 1011)));
}

#[test]
fn morton_recommended_box_has_mosaic_cells() {
    // Same cell centres as Mosaic's grid, with 20 bits instead of 22.
    let morton = recommended_morton();
    assert_eq!(morton.depth(), 20);
    for &(lat, lon) in &SAMPLE_POINTS {
        let (m_lat, m_lon) = morton.decode(&morton.encode(lat, lon).unwrap());
        let (g_lat, g_lon) = MosaicGrid.decode(&MosaicGrid.encode(lat, lon).unwrap());
        assert!(
            (m_lat - g_lat).abs() < 1e-9 && (m_lon - g_lon).abs() < 1e-9,
            "({lat}, {lon}): Morton centre ({m_lat}, {m_lon}), grid point ({g_lat}, {g_lon})"
        );
    }
}

#[test]
fn morton_round_trip() {
    let morton = recommended_morton();
    let half_cell = 1.024 / 1024.0 / 2.0;
    for &(lat, lon) in &SAMPLE_POINTS {
        let bits = morton.encode(lat, lon).unwrap();
        assert_eq!(bits.len(), morton.depth());
        let (c_lat, c_lon) = morton.decode(&bits);
        assert!((lat - c_lat).abs() <= half_cell && (lon - c_lon).abs() <= half_cell);
        assert_eq!(morton.encode(c_lat, c_lon).unwrap(), bits);
    }
}

#[test]
fn morton_prefix_is_enclosing_region() {
    let (min_lat, max_lat, min_lon, max_lon) = MORTON_BOX;
    let morton = recommended_morton();
    let (lat, lon) = (30.265, -97.732);
    let bits = morton.encode(lat, lon).unwrap();
    let (lat_idx, lon_idx) = morton.to_cell(lat, lon).unwrap();
    for j in 1..=10 {
        // The first 2j bits are the point's cell on the same box with j bits per axis.
        let (block_lat, block_lon) = deinterleave(&bits[..2 * j]);
        assert_eq!(
            (block_lat, block_lon),
            (lat_idx >> (10 - j), lon_idx >> (10 - j))
        );
        let coarse = Morton::new(min_lat, max_lat, min_lon, max_lon, j);
        assert_eq!(coarse.encode(lat, lon).unwrap(), bits[..2 * j]);
        let ((lat_lo, lat_hi), (lon_lo, lon_hi)) = coarse.cell_box(block_lat, block_lon);
        assert!(lat_lo <= lat && lat < lat_hi && lon_lo <= lon && lon < lon_hi);
    }
    // A pickup one cell away shares the 8 x 8 block (14 bits); the Domain does not.
    let near = morton.encode(30.266, -97.733).unwrap();
    let far = morton.encode(30.402, -97.725).unwrap();
    assert_eq!(bits[..14], near[..14]);
    assert_ne!(bits[..14], far[..14]);
}

#[test]
fn morton_rejects_points_outside_bbox() {
    let (min_lat, max_lat, min_lon, max_lon) = MORTON_BOX;
    let morton = recommended_morton();
    // Dallas.
    assert!(morton.encode(32.78, -96.80).is_none());
    // The north and east edges are exclusive, the south-west corner is inside.
    assert!(morton.encode(max_lat, -97.7).is_none());
    assert!(morton.encode(30.3, max_lon).is_none());
    assert_eq!(morton.to_cell(min_lat, min_lon), Some((0, 0)));
    assert!(morton.encode(f64::NAN, -97.7).is_none());
    // Just inside the north-east corner is the last cell, even where rounding reaches 2^10.
    assert_eq!(
        morton.to_cell(next_down(max_lat), next_down(max_lon)),
        Some((1023, 1023))
    );
}

#[test]
#[should_panic(expected = "invalid Morton bounding box")]
fn morton_rejects_inverted_box() {
    Morton::new(30.9245, 29.9005, -98.0505, -97.0265, 10);
}
