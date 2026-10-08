"""Decimal spelling oracle from the immutable Pollard 1.6.0 wheel."""
import copy
from decimal import Decimal, localcontext
import hashlib
import json
from pathlib import Path
import sys

wheel = Path(sys.argv[1]).resolve()
sha = "569fb5f130a82c9be327b8dcbd285e3be063200bd9773ca15c5d6bb62edd627f"
assert hashlib.sha256(wheel.read_bytes()).hexdigest() == sha
sys.path.insert(0, str(wheel))
import pollard
from pollard.arbiter import BudgetReservation, WindowReservation
from pollard.stores._transactional import TransactionalKVStore, _reservation_request, _reservation_charges
assert pollard.__version__ == "1.6.0" and str(wheel) in pollard.__file__

class Tx:
    def __init__(self, values): self.values = values
    def get(self, bucket, key): return self.values.get(bucket, {}).get(key)
    def items(self, bucket): return sorted(self.values.get(bucket, {}).items())
    def put(self, bucket, key, value): self.values.setdefault(bucket, {})[key] = value
    def delete(self, bucket, key): self.values.get(bucket, {}).pop(key, None)
    def now(self): return 1000.0

class Memory(TransactionalKVStore):
    def __init__(self): self.values = {"schema": {"version": "1"}}
    def _read(self, callback): return callback(Tx(self.values))
    def _write(self, callback):
        values = copy.deepcopy(self.values)
        result = callback(Tx(values))
        self.values = values
        return result
    def _is_connection_error(self, error): return False
    def reconnect(self): pass

def decmap(values): return {key: Decimal(value) for key, value in values.items()}

rows = []
for amount in ["1E+2", "1.0E+2", "1.00E+2", "-0", "-0.00", "-0E+2", "-0E-7",
               "0E+9", "0E-28", "1.2300", "2.500E+3", "1e-28", "0.0000010"]:
    b = {"scope_id": "decimal:é", "limits": {"usd": "1E+6"}, "baseline": {"usd": "1E+2"}, "estimates": {"usd": amount}}
    w = {"ledger_key": "decimal:☃", "meter": "usd", "limit": "1.0E+6", "amount": amount, "window_seconds": 30.0}
    budget = BudgetReservation(**{key: value if key == "scope_id" else decmap(value) for key, value in b.items()})
    window = WindowReservation(**{**w, "limit": Decimal(w["limit"]), "amount": Decimal(amount)})
    request, digest = _reservation_request([budget], [window], 60.0)
    charges, charges_digest = _reservation_charges({"usd": Decimal(amount)})
    row = {"budgets": [b], "windows": [w], "lease": 60.0, "request": request, "digest": digest,
           "charges": {"usd": amount}, "charges_text": charges, "charges_digest": charges_digest}
    # Tiny amounts plus a large baseline require rounding under the default
    # Python context. Codec parity still applies; strict native arithmetic
    # rejects the unrepresentable result rather than emulate context rounding.
    if amount not in ["1e-28", "0E-28"]:
        store = Memory()
        store._pollard_reserve("decimal", [budget], [window], 60.0)
        row["reserved"] = copy.deepcopy(store.values)
        store._pollard_settle("decimal", {"usd": Decimal(amount)})
        row["settled"] = copy.deepcopy(store.values)
    rows.append(row)

costs = []
maximum = "79228162514264337593543950335"
for input_rate, output_rate, inputs, outputs in [
    ("6e-23", "0", 1, 0), ("1e-28", "0", 1, 0), ("5e-23", "5e-23", 1, 1),
    (maximum, "0", 1_000_000, 0), (maximum, maximum, 500_000, 500_000),
    (maximum, maximum, 1_000_000, 1_000_000),
    ("7.9228162514264337593543950335", "0.0000000000000000000000000065", 1_000_000, 1_000_000),
    ("0.1234567890123456789012345678", "0.0000000000000000000000000001", 1_000_000, 1_000_000),
] + [(f"{rate}e-{scale}", "0", count, 0) for rate in [1, 5, 6, 19, 12345] for scale in [0, 7, 22, 23, 28] for count in [0, 1, 3, 1_000_000, 18446744073709551615]]:
    with localcontext() as context:
        context.prec = 200
        exact = (Decimal(input_rate) * inputs + Decimal(output_rate) * outputs) / 1_000_000
        normalized = exact.normalize()
        _, digits, exponent = normalized.as_tuple()
        coefficient = int("".join(map(str, digits)))
        representable = exponent >= -28 and coefficient * 10**max(0, exponent) <= int(maximum)
    costs.append({"input_rate": input_rate, "output_rate": output_rate, "inputs": inputs, "outputs": outputs, "exact": str(exact), "representable": representable})
output = {"provenance": {"pollard_version": "1.6.0", "wheel_sha256": sha, "generator": Path(__file__).name,
                         "cost_reference": "Python Decimal localcontext precision=200; exact mathematical reference, not default-context emulation"},
          "requests": rows, "costs": costs}
Path(__file__).with_name("pypi160_decimal_wire.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"generated {len(rows)} wire cases and {len(costs)} exact cost cases")
