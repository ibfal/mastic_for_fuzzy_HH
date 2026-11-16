//! A vector of one-hot blocks, for putting several attributes of one report in `beta`.
//!
//! `beta` is the concatenation of `blocks.len()` one-hot vectors, e.g. pickup hour (24) | car
//! category (4) | surge (2). Summed over many reports, each block is that attribute's histogram.
//!
//! The validity circuit generalises prio's `Histogram` (prio 0.15.5, `flp/types.rs`). It checks
//!
//! 1. every entry is 0 or 1, and
//! 2. every block sums to exactly 1,
//!
//! so each report adds exactly 1 to exactly one bucket of every block. Prio's `SumVec` with
//! `bits = 1` would check only (1), which lets a client set every bucket of a block. With one
//! block this is exactly `Histogram`: the same gadget, joint randomness and output.

use std::{fmt, marker::PhantomData};

use prio::{
    field::FftFriendlyFieldElement,
    flp::{
        gadgets::{Mul, ParallelSumGadget},
        FlpError, Gadget, Type,
    },
};

pub struct MultiHistogram<F, S> {
    /// Number of buckets of each block.
    blocks: Vec<usize>,
    /// Total number of buckets, the sum of `blocks`.
    length: usize,
    chunk_length: usize,
    gadget_calls: usize,
    phantom: PhantomData<(F, S)>,
}

impl<F: FftFriendlyFieldElement, S: ParallelSumGadget<F, Mul<F>>> MultiHistogram<F, S> {
    /// A [`MultiHistogram`] with blocks of the given sizes.
    pub fn new(blocks: Vec<usize>, chunk_length: usize) -> Result<Self, FlpError> {
        if blocks.is_empty() {
            return Err(FlpError::InvalidParameter("no blocks".into()));
        }
        if blocks.contains(&0) {
            return Err(FlpError::InvalidParameter("a block has no buckets".into()));
        }
        if chunk_length == 0 {
            return Err(FlpError::InvalidParameter(
                "chunk_length cannot be zero".into(),
            ));
        }
        let length = blocks
            .iter()
            .try_fold(0usize, |acc, &b| acc.checked_add(b))
            .filter(|&l| l < u32::MAX as usize)
            .ok_or_else(|| {
                FlpError::Encode("invalid length: number of buckets exceeds maximum".into())
            })?;

        Ok(Self {
            blocks,
            length,
            chunk_length,
            gadget_calls: length.div_ceil(chunk_length),
            phantom: PhantomData,
        })
    }

    pub fn blocks(&self) -> &[usize] {
        &self.blocks
    }
}

impl<F, S> Clone for MultiHistogram<F, S> {
    fn clone(&self) -> Self {
        Self {
            blocks: self.blocks.clone(),
            length: self.length,
            chunk_length: self.chunk_length,
            gadget_calls: self.gadget_calls,
            phantom: PhantomData,
        }
    }
}

impl<F, S> PartialEq for MultiHistogram<F, S> {
    fn eq(&self, other: &Self) -> bool {
        self.blocks == other.blocks && self.chunk_length == other.chunk_length
    }
}

impl<F, S> Eq for MultiHistogram<F, S> {}

impl<F, S> fmt::Debug for MultiHistogram<F, S> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("MultiHistogram")
            .field("blocks", &self.blocks)
            .field("chunk_length", &self.chunk_length)
            .finish()
    }
}

/// `n` as a field element. prio's `valid_integer_try_from` is `pub(crate)`; `n` is the number of
/// shares, so a sum of ones is enough.
fn small<F: FftFriendlyFieldElement>(n: usize) -> F {
    (0..n).fold(F::zero(), |acc, _| acc + F::one())
}

/// Shares of zero iff every entry of `input` is 0 or 1, else nonzero with high probability. Copied
/// from prio 0.15.5 `flp::types::parallel_sum_range_checks`, which is `pub(crate)`.
fn parallel_sum_range_checks<F: FftFriendlyFieldElement>(
    gadget: &mut Box<dyn Gadget<F>>,
    input: &[F],
    joint_randomness: F,
    chunk_length: usize,
    num_shares: usize,
) -> Result<F, FlpError> {
    let num_shares_inverse = small::<F>(num_shares).inv();

    let mut output = F::zero();
    let mut r_power = joint_randomness;
    let mut padded_chunk = vec![F::zero(); 2 * chunk_length];

    for chunk in input.chunks(chunk_length) {
        for (input, args) in chunk.iter().zip(padded_chunk.chunks_exact_mut(2)) {
            args[0] = r_power * *input;
            args[1] = *input - num_shares_inverse;
            r_power *= joint_randomness;
        }
        // Pad a final partial chunk with zeros, which pass the range check.
        for args in padded_chunk[chunk.len() * 2..].chunks_exact_mut(2) {
            args[0] = F::zero();
            args[1] = -num_shares_inverse;
        }
        output += gadget.call(&padded_chunk)?;
    }

    Ok(output)
}

impl<F, S> Type for MultiHistogram<F, S>
where
    F: FftFriendlyFieldElement,
    S: ParallelSumGadget<F, Mul<F>> + Eq + 'static,
{
    /// Not a registered VDAF type; only used locally.
    const ID: u32 = 0xFFFF_0003;
    /// One bucket index per block.
    type Measurement = Vec<usize>;
    type AggregateResult = Vec<F::Integer>;
    type Field = F;

    fn encode_measurement(&self, measurement: &Vec<usize>) -> Result<Vec<F>, FlpError> {
        if measurement.len() != self.blocks.len() {
            return Err(FlpError::Encode(format!(
                "{} bucket indices for {} blocks",
                measurement.len(),
                self.blocks.len()
            )));
        }
        let mut data = vec![F::zero(); self.length];
        let mut offset = 0;
        for (&bucket, &size) in measurement.iter().zip(&self.blocks) {
            if bucket >= size {
                return Err(FlpError::Encode(format!(
                    "bucket {bucket} out of range for a block of {size}"
                )));
            }
            data[offset + bucket] = F::one();
            offset += size;
        }
        Ok(data)
    }

    fn decode_result(
        &self,
        data: &[F],
        _num_measurements: usize,
    ) -> Result<Vec<F::Integer>, FlpError> {
        if data.len() != self.length {
            return Err(FlpError::Decode("unexpected input length".into()));
        }
        Ok(data.iter().map(|&x| F::Integer::from(x)).collect())
    }

    fn gadget(&self) -> Vec<Box<dyn Gadget<F>>> {
        vec![Box::new(S::new(
            Mul::new(self.gadget_calls),
            self.chunk_length,
        ))]
    }

    fn valid(
        &self,
        g: &mut Vec<Box<dyn Gadget<F>>>,
        input: &[F],
        joint_rand: &[F],
        num_shares: usize,
    ) -> Result<F, FlpError> {
        self.valid_call_check(input, joint_rand)?;

        // (1) Every entry is 0 or 1.
        let range_check = parallel_sum_range_checks(
            &mut g[0],
            input,
            joint_rand[0],
            self.chunk_length,
            num_shares,
        )?;

        // (2) Every block sums to 1. Each aggregator holds an additive share, so each subtracts
        // its share of the constant 1.
        let one_share = small::<F>(num_shares).inv();

        // A random linear combination r * range + r^2 * sum_0 + r^3 * sum_1 + ... It is zero for
        // a valid input. For an invalid one it is a nonzero polynomial in r of degree at most
        // blocks + 1, so it is zero with probability at most (blocks + 1) / |F|.
        let r = joint_rand[1];
        let mut r_power = r;
        let mut out = r_power * range_check;
        let mut offset = 0;
        for &size in &self.blocks {
            let block_sum = input[offset..offset + size]
                .iter()
                .fold(-one_share, |acc, &x| acc + x);
            r_power *= r;
            out += r_power * block_sum;
            offset += size;
        }
        Ok(out)
    }

    fn truncate(&self, input: Vec<F>) -> Result<Vec<F>, FlpError> {
        self.truncate_call_check(&input)?;
        Ok(input)
    }

    fn input_len(&self) -> usize {
        self.length
    }

    fn proof_len(&self) -> usize {
        (self.chunk_length * 2) + 2 * ((1 + self.gadget_calls).next_power_of_two() - 1) + 1
    }

    fn verifier_len(&self) -> usize {
        2 + self.chunk_length * 2
    }

    fn output_len(&self) -> usize {
        self.input_len()
    }

    fn joint_rand_len(&self) -> usize {
        2
    }

    fn prove_rand_len(&self) -> usize {
        self.chunk_length * 2
    }

    fn query_rand_len(&self) -> usize {
        1
    }
}

#[cfg(test)]
mod tests {
    use prio::{
        field::{random_vector, Field128, FieldElement},
        flp::{
            gadgets::{Mul, ParallelSum},
            types::Histogram,
        },
    };

    use super::*;

    type Multi = MultiHistogram<Field128, ParallelSum<Field128, Mul<Field128>>>;

    /// Prove `input` (any vector, valid or not), split input and proof into two shares, and
    /// return whether the two verifiers accept.
    fn accepts<T: Type<Field = Field128>>(typ: &T, input: &[Field128]) -> bool {
        let joint_rand = random_vector(typ.joint_rand_len()).unwrap();
        let prove_rand = random_vector(typ.prove_rand_len()).unwrap();
        let query_rand = random_vector(typ.query_rand_len()).unwrap();
        let proof = typ.prove(input, &prove_rand, &joint_rand).unwrap();

        let split = |v: &[Field128]| {
            let s0: Vec<Field128> = random_vector(v.len()).unwrap();
            let s1: Vec<Field128> = v.iter().zip(&s0).map(|(&a, &b)| a - b).collect();
            (s0, s1)
        };
        let (in_0, in_1) = split(input);
        let (pf_0, pf_1) = split(&proof);
        let v0 = typ
            .query(&in_0, &pf_0, &query_rand, &joint_rand, 2)
            .unwrap();
        let v1 = typ
            .query(&in_1, &pf_1, &query_rand, &joint_rand, 2)
            .unwrap();
        let verifier: Vec<Field128> = v0.iter().zip(&v1).map(|(&a, &b)| a + b).collect();
        typ.decide(&verifier).unwrap()
    }

    fn field(v: &[i64]) -> Vec<Field128> {
        v.iter()
            .map(|&x| {
                if x >= 0 {
                    Field128::from(x as u128)
                } else {
                    -Field128::from((-x) as u128)
                }
            })
            .collect()
    }

    #[test]
    fn encodes_one_hot_per_block() {
        let typ = Multi::new(vec![3, 2, 4], 2).unwrap();
        assert_eq!(
            typ.encode_measurement(&vec![2, 0, 1]).unwrap(),
            field(&[0, 0, 1, 1, 0, 0, 1, 0, 0])
        );
        assert!(typ.encode_measurement(&vec![3, 0, 0]).is_err());
        assert!(typ.encode_measurement(&vec![0, 0]).is_err());
    }

    #[test]
    fn accepts_every_valid_measurement() {
        let typ = Multi::new(vec![3, 2, 4], 2).unwrap();
        for a in 0..3 {
            for b in 0..2 {
                for c in 0..4 {
                    let input = typ.encode_measurement(&vec![a, b, c]).unwrap();
                    assert!(accepts(&typ, &input), "rejected {a} {b} {c}");
                }
            }
        }
    }

    #[test]
    fn rejects_invalid_blocks() {
        let typ = Multi::new(vec![3, 2, 4], 2).unwrap();
        for bad in [
            // Two buckets set in the first block.
            [1, 1, 0, 1, 0, 1, 0, 0, 0],
            // No bucket set in the second block.
            [1, 0, 0, 0, 0, 1, 0, 0, 0],
            // Every bucket set: what SumVec with bits = 1 would let through.
            [1, 1, 1, 1, 1, 1, 1, 1, 1],
            // Block sums are right but entries are not 0/1: 2 and -1 in the first block.
            [2, -1, 0, 1, 0, 1, 0, 0, 0],
            // All zero.
            [0, 0, 0, 0, 0, 0, 0, 0, 0],
            // One 1 per block, but one block holds a 5.
            [0, 0, 1, 0, 1, 0, 5, 0, 0],
        ] {
            assert!(!accepts(&typ, &field(&bad)), "accepted {bad:?}");
        }
    }

    #[test]
    fn one_block_matches_prio_histogram() {
        // Same proof length, and the same accept/reject decisions as prio's Histogram.
        let multi = Multi::new(vec![5], 2).unwrap();
        let hist = Histogram::<Field128, ParallelSum<Field128, Mul<Field128>>>::new(5, 2).unwrap();
        assert_eq!(multi.proof_len(), hist.proof_len());
        assert_eq!(multi.verifier_len(), hist.verifier_len());
        for input in [
            field(&[0, 0, 1, 0, 0]),
            field(&[1, 0, 0, 0, 0]),
            field(&[0, 1, 1, 0, 0]),
            field(&[0, 0, 0, 0, 0]),
            field(&[2, -1, 0, 0, 0]),
        ] {
            assert_eq!(accepts(&multi, &input), accepts(&hist, &input), "{input:?}");
        }
        // And the same circuit output, r * range + r^2 * (sum - 1), on valid and invalid inputs.
        let joint_rand = random_vector(2).unwrap();
        for (input, valid) in [
            (field(&[0, 1, 0, 0, 0]), true),
            (field(&[0, 1, 1, 0, 3]), false),
        ] {
            let m = multi
                .valid(&mut multi.gadget(), &input, &joint_rand, 1)
                .unwrap();
            let h = hist
                .valid(&mut hist.gadget(), &input, &joint_rand, 1)
                .unwrap();
            assert_eq!(m, h);
            assert_eq!(m == Field128::zero(), valid);
        }
    }
}
