use mastic::geo::{LocationEncoder, MosaicGrid};

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
        let bits = enc.encode(lat, lon);
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
