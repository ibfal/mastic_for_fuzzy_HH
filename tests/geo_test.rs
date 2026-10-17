use h3o::Resolution;
use mastic::geo::{Geohash, LocationEncoder, MosaicGrid, H3};

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
