use rust_decimal::Decimal;

/// Parse a bounded decimal without silently rounding input digits.
///
/// Accepts ordinary decimal text and scientific notation, preserving supported
/// scale. Values requiring more than 28 fractional digits or a coefficient
/// outside Decimal's 96-bit representation are rejected. Pollard's accounting
/// operations also reject results that cannot be represented exactly.
pub fn parse_decimal_exact(input: &str) -> std::result::Result<Decimal, rust_decimal::Error> {
    if !input.contains(['e', 'E']) {
        if let Ok(value) = Decimal::from_str_exact(input) {
            return Ok(value);
        }
    }
    let invalid = || rust_decimal::Error::from("invalid exact decimal notation");
    let (coefficient, exponent) = match input.split_once(['e', 'E']) {
        Some((coefficient, exponent)) => {
            (coefficient, exponent.parse::<i64>().map_err(|_| invalid())?)
        }
        None => (input, 0),
    };
    let (negative, coefficient) = if let Some(value) = coefficient.strip_prefix('-') {
        (true, value)
    } else {
        (false, coefficient.strip_prefix('+').unwrap_or(coefficient))
    };
    let mut digits = String::with_capacity(coefficient.len());
    let mut point = false;
    let mut fractional = 0i64;
    for byte in coefficient.bytes() {
        match byte {
            b'0'..=b'9' => {
                digits.push(char::from(byte));
                if point {
                    fractional = fractional.checked_add(1).ok_or_else(invalid)?;
                }
            }
            b'.' if !point => point = true,
            _ => return Err(invalid()),
        }
    }
    if digits.is_empty() {
        return Err(invalid());
    }
    let mut scale = fractional.checked_sub(exponent).ok_or_else(invalid)?;
    let significant = digits.trim_start_matches('0');
    let mut significant = if significant.is_empty() {
        "0".to_owned()
    } else {
        significant.to_owned()
    };
    let oversized = |digits: &str| {
        digits.len() > 29 || (digits.len() == 29 && digits > "79228162514264337593543950335")
    };
    if significant == "0" {
        scale = scale.min(i64::from(Decimal::MAX_SCALE));
    }
    // Discard only redundant zeros, and only when the requested representation
    // would otherwise be unsupported. Never round a significant input digit.
    while scale > 0
        && significant.ends_with('0')
        && (scale > i64::from(Decimal::MAX_SCALE) || oversized(&significant))
    {
        significant.pop();
        scale -= 1;
    }
    if scale > i64::from(Decimal::MAX_SCALE) {
        return Err(rust_decimal::Error::Underflow);
    }
    let scale = if scale < 0 {
        if significant != "0" {
            let zeros = scale
                .checked_neg()
                .and_then(|n| usize::try_from(n).ok())
                .ok_or_else(invalid)?;
            if significant
                .len()
                .checked_add(zeros)
                .map_or(true, |n| n > 29)
            {
                return Err(rust_decimal::Error::ExceedsMaximumPossibleValue);
            }
            significant.push_str(&"0".repeat(zeros));
        }
        0
    } else {
        scale as usize
    };
    if oversized(&significant) {
        return Err(rust_decimal::Error::ExceedsMaximumPossibleValue);
    }
    let mut expanded = if negative {
        "-".to_owned()
    } else {
        String::new()
    };
    if scale == 0 {
        expanded.push_str(&significant);
    } else if scale >= significant.len() {
        expanded.push_str("0.");
        expanded.push_str(&"0".repeat(scale - significant.len()));
        expanded.push_str(&significant);
    } else {
        let point = significant.len() - scale;
        expanded.push_str(&significant[..point]);
        expanded.push('.');
        expanded.push_str(&significant[point..]);
    }
    Decimal::from_str_exact(&expanded)
}

// Intermediate coefficients can be wider than Decimal even when the final
// amount fits (for example MAX * 1_000_000 / 1_000_000). Decimal's checked
// arithmetic only checks overflow: it can silently round nonzero digits.
// These small base-10 integers retain all digits until the final range check.
#[derive(Clone)]
struct Wide {
    digits: Vec<u8>, // least significant first
    negative: bool,
    scale: u32,
}
impl Wide {
    fn from_decimal(value: Decimal) -> Self {
        Self {
            digits: value
                .mantissa()
                .unsigned_abs()
                .to_string()
                .bytes()
                .rev()
                .map(|b| b - b'0')
                .collect(),
            negative: value.is_sign_negative(),
            scale: value.scale(),
        }
    }
    fn trim(&mut self) {
        while self.digits.len() > 1 && self.digits.last() == Some(&0) {
            self.digits.pop();
        }
        if self.digits == [0] {
            self.negative = false;
        }
    }
    fn align(&mut self, scale: u32) {
        if scale > self.scale {
            self.digits.splice(
                0..0,
                std::iter::repeat(0).take((scale - self.scale) as usize),
            );
            self.scale = scale;
        }
    }
    fn add(mut self, mut other: Self) -> Self {
        let scale = self.scale.max(other.scale);
        self.align(scale);
        other.align(scale);
        self.trim();
        other.trim();
        if self.negative == other.negative {
            let mut carry = 0;
            self.digits
                .resize(self.digits.len().max(other.digits.len()), 0);
            for (index, digit) in self.digits.iter_mut().enumerate() {
                let sum = *digit + other.digits.get(index).copied().unwrap_or(0) + carry;
                *digit = sum % 10;
                carry = sum / 10;
            }
            if carry != 0 {
                self.digits.push(carry);
            }
        } else {
            let order = self
                .digits
                .len()
                .cmp(&other.digits.len())
                .then_with(|| self.digits.iter().rev().cmp(other.digits.iter().rev()));
            if order.is_lt() {
                std::mem::swap(&mut self, &mut other);
            }
            let mut borrow = 0i16;
            for (index, digit) in self.digits.iter_mut().enumerate() {
                let difference = i16::from(*digit)
                    - i16::from(other.digits.get(index).copied().unwrap_or(0))
                    - borrow;
                *digit = difference.rem_euclid(10) as u8;
                borrow = i16::from(difference < 0);
            }
        }
        self.trim();
        self
    }
    fn multiply(self, other: Self) -> Self {
        let mut digits = vec![0u8; self.digits.len() + other.digits.len()];
        for (i, a) in self.digits.iter().enumerate() {
            let mut carry = 0;
            for (j, b) in other.digits.iter().enumerate() {
                let product = digits[i + j] + a * b + carry;
                digits[i + j] = product % 10;
                carry = product / 10;
            }
            digits[i + other.digits.len()] = carry;
        }
        let mut result = Self {
            digits,
            negative: self.negative != other.negative,
            scale: self.scale + other.scale,
        };
        result.trim();
        result
    }
    fn finish(self) -> Option<Decimal> {
        let digits: String = self
            .digits
            .iter()
            .rev()
            .map(|n| char::from(b'0' + n))
            .collect();
        parse_decimal_exact(&format!(
            "{}{digits}e-{}",
            if self.negative { "-" } else { "" },
            self.scale
        ))
        .ok()
    }
}

fn from_coefficient(mut coefficient: i128, mut scale: u32) -> Option<Decimal> {
    const MAX: u128 = 79_228_162_514_264_337_593_543_950_335;
    while (coefficient.unsigned_abs() > MAX || scale > Decimal::MAX_SCALE)
        && scale > 0
        && coefficient % 10 == 0
    {
        coefficient /= 10;
        scale -= 1;
    }
    Decimal::try_from_i128_with_scale(coefficient, scale).ok()
}
/// Add without dropping significant digits; `None` means the exact result does
/// not fit Decimal's 96-bit coefficient and maximum scale of 28.
pub fn exact_add(left: Decimal, right: Decimal) -> Option<Decimal> {
    let scale = left.scale().max(right.scale());
    let aligned = |value: Decimal| {
        value
            .mantissa()
            .checked_mul(10i128.pow(scale - value.scale()))
    };
    if let Some(sum) = aligned(left)
        .zip(aligned(right))
        .and_then(|(a, b)| a.checked_add(b))
    {
        return from_coefficient(sum, scale);
    }
    Wide::from_decimal(left)
        .add(Wide::from_decimal(right))
        .finish()
}
/// Subtract without rounding; `None` means the exact result is unrepresentable.
pub fn exact_subtract(left: Decimal, right: Decimal) -> Option<Decimal> {
    exact_add(left, -right)
}
pub(crate) fn cost_per_million(terms: &[(u64, Decimal)]) -> Option<Decimal> {
    let scale = terms
        .iter()
        .map(|(_, rate)| rate.scale())
        .max()
        .unwrap_or(0);
    let coefficient = terms.iter().try_fold(0i128, |sum, (count, rate)| {
        rate.mantissa()
            .checked_mul(i128::from(*count))
            .and_then(|value| value.checked_mul(10i128.pow(scale - rate.scale())))
            .and_then(|value| sum.checked_add(value))
    });
    if let Some(coefficient) = coefficient {
        return from_coefficient(coefficient, scale + 6).map(|value| value.normalize());
    }
    let mut total = Wide::from_decimal(Decimal::ZERO);
    for (count, rate) in terms {
        total = total
            .add(Wide::from_decimal(Decimal::from(*count)).multiply(Wide::from_decimal(*rate)));
    }
    total.scale += 6;
    total.finish().map(|value| value.normalize())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn checked_accounting_rejects_rounding_but_keeps_representable_results() {
        let tiny = parse_decimal_exact("1e-28").unwrap();
        assert!(cost_per_million(&[(1, tiny)]).is_none());
        assert!(cost_per_million(&[(1, parse_decimal_exact("6e-23").unwrap())]).is_none());
        assert_eq!(cost_per_million(&[(0, tiny)]), Some(Decimal::ZERO));
        assert_eq!(
            cost_per_million(&[(1_000_000, Decimal::MAX)]),
            Some(Decimal::MAX)
        );
        assert!(exact_add(Decimal::MAX, tiny).is_none());
        assert!(exact_subtract(Decimal::MAX, tiny).is_none());
        assert_eq!(exact_add(Decimal::MAX, -Decimal::MAX), Some(Decimal::ZERO));
        assert_eq!(
            exact_subtract(tiny, Decimal::ONE),
            Some(parse_decimal_exact("-0.9999999999999999999999999999").unwrap())
        );
        assert_eq!(
            exact_add(
                parse_decimal_exact("7.9228162514264337593543950335").unwrap(),
                parse_decimal_exact("0.0000000000000000000000000065").unwrap()
            ),
            Some(parse_decimal_exact("7.92281625142643375935439504").unwrap())
        );
    }
}
