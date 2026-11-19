"""Nominal unloaded gate truth tables; these are not manufacturer accuracy tests."""
# Truth tables check nominal unloaded model behavior at the chosen supply.
# They do not assert real IC input ranges, propagation delay, or output-drive specifications.

from itertools import product
import unittest

from circuitlab.math.analysis import solve_input


class GateLogicTest(unittest.TestCase):
    def test_nominal_five_volt_truth_tables(self):
        # Exercise every input combination at the audited arities. The 2.5 V
        # decision threshold is a regression convention, not a silicon rating.
        cases = (
            ("not", (1,), lambda bits: not bits[0]),
            ("buf", (1,), lambda bits: bool(bits[0])),
            ("and", (2, 3), all),
            ("nand", (2, 3), lambda bits: not all(bits)),
            ("or", (2, 3), any),
            ("nor", (1, 2, 3), lambda bits: not any(bits)),
            ("xor", (2,), lambda bits: bits[0] != bits[1]),
            ("xnor", (2,), lambda bits: bits[0] == bits[1]),
        )
        for name, arities, expected in cases:
            for count in arities:
                inputs = [f"in{i}" for i in range(count)]
                for bits in product((0, 1), repeat=count):
                    with self.subTest(gate=name, inputs=bits):
                        source = ["VCC(5) between(vcc, 0);"]
                        source.extend(
                            f"V{i}({bit * 5}) between({net}, 0);"
                            for i, (net, bit) in enumerate(zip(inputs, bits))
                        )
                        source.append(f"{name}(vcc, 0, {', '.join(inputs)}, out);")
                        result = solve_input("\n".join(source))
                        network = result.network
                        voltage = float(network.voltage[network.nodes.index("out")].real)
                        self.assertFalse(result.diagnostics)
                        if expected(bits):
                            self.assertGreater(voltage, 2.5)
                        else:
                            self.assertLess(voltage, 2.5)
