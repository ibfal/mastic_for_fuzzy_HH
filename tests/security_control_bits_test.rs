//! Regression tests for the control-bit bug in `PrgSeed::expand_dir`.
//!
//! The PRG used to read the control bits after clearing them, so they were always
//! `(true, true)`. Both servers' bits then cancelled in every correction word, which left
//! `cor_words[i].bits == (!alpha[i], alpha[i])`: one aggregator's key held the whole location.

use mastic::{prg::PrgSeed, vidpf::VidpfKey};
use prio::field::{Field128, FieldElement};
use rand::Rng;

/// The bits server 0 could read from its own key if the control bits were constant.
fn alpha_guess_from_one_key(key: &VidpfKey) -> Vec<bool> {
    let v = serde_json::to_value(key).unwrap();
    v["cor_words"]
        .as_array()
        .unwrap()
        .iter()
        .map(|cw| cw["bits"][1].as_bool().unwrap())
        .collect()
}

#[test]
fn control_bits_are_not_constant() {
    let mut seen = std::collections::HashSet::new();
    for _ in 0..200 {
        let out = PrgSeed::random().expand();
        seen.insert(out.bits);
    }
    // Random seeds give all four combinations; the bug gave only (true, true).
    assert_eq!(seen.len(), 4, "control bits seen: {seen:?}");
}

#[test]
fn one_key_does_not_reveal_alpha() {
    let mut rng = rand::thread_rng();
    let (keys, bits_per_key) = (100, 20);
    let mut matches = 0;
    for _ in 0..keys {
        let alpha: Vec<bool> = (0..bits_per_key).map(|_| rng.gen()).collect();
        let (key_0, _key_1) = VidpfKey::gen(&alpha, &[Field128::one()]);
        let guess = alpha_guess_from_one_key(&key_0);
        matches += alpha.iter().zip(&guess).filter(|(a, g)| a == g).count();
    }
    // With the bug every bit matched (2000 of 2000). Now a match is a coin flip:
    // 1000 expected, and 900..1100 is more than 4 standard deviations wide.
    let total = keys * bits_per_key;
    assert!(
        (900..=1100).contains(&matches),
        "{matches} of {total} location bits readable from one key"
    );
}
