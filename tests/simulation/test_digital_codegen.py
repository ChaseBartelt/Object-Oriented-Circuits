"""Check bytecode lowering against independent unsigned expression/edge evaluation."""

import operator
import random
import unittest
from unittest.mock import patch

from circuitlab.math.digital import DigitalSimulation, _expression


OPERATORS = {"+": operator.add, "-": operator.sub, "&": operator.and_,
             "|": operator.or_, "^": operator.xor, "<<": operator.lshift,
             ">>": operator.rshift, "==": operator.eq, "!=": operator.ne,
             "<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge,
             "&&": lambda a, b: bool(a) and bool(b),
             "||": lambda a, b: bool(a) or bool(b)}
BOOLEAN = {"==", "!=", "<", "<=", ">", ">=", "&&", "||"}


def evaluate(tree, values, widths):
    """Small direct reference evaluator; return the value and expression width."""
    op = tree[0]
    if op == "literal":
        return tree[1], tree[2]
    if op == "name":
        return values[tree[1]], widths[tree[1]]
    if op in {"bit", "slice"}:
        high, low = (tree[2], tree[2]) if op == "bit" else tree[2:]
        width = high - low + 1
        return (values[tree[1]] >> low) & ((1 << width) - 1), width
    if op == "concat":
        result, width = 0, 0
        for part in tree[1]:
            value, size = evaluate(part, values, widths)
            result = result << size | value & ((1 << size) - 1)
            width += size
        return result, width
    left, size = evaluate(tree[1], values, widths)
    if op == "!":
        return int(not left), 1
    if op == "?:":
        yes, yes_width = evaluate(tree[2], values, widths)
        no, no_width = evaluate(tree[3], values, widths)
        return yes if left else no, max(yes_width, no_width)
    right, right_width = evaluate(tree[2], values, widths)
    width = 1 if op in BOOLEAN else size if op in {"<<", ">>"} else max(size, right_width)
    value = 0 if op in {"<<", ">>"} and right >= size else OPERATORS[op](left, right)
    return value & ((1 << width) - 1), width


class DigitalCodegenTest(unittest.TestCase):
    def test_generated_expressions_match_unsigned_reference(self):
        rng = random.Random(781)
        widths = {"a": 4, "b": 8, "count": 64}
        names = {name: name for name in widths}
        slots = {name: index for index, name in enumerate(widths)}
        leaves = [("name", name) for name in widths] + [
            ("literal", 0, 1), ("literal", 15, 4), ("literal", 255, 8),
            ("bit", "b", 7), ("slice", "b", 5, 2)]

        def expression(depth):
            if depth == 0 or rng.randrange(4) == 0:
                return rng.choice(leaves)
            op = rng.choice((*OPERATORS, "!", "?:", "concat"))
            if op == "!":
                return op, expression(depth - 1)
            if op == "?:":
                return op, *(expression(depth - 1) for _ in range(3))
            if op == "concat":
                return op, tuple(expression(depth - 1) for _ in range(3))
            return op, expression(depth - 1), expression(depth - 1)

        samples = [{"a": rng.randrange(16), "b": rng.randrange(256), "count": count}
                   for count in (0, 1, 3, 7, 8, 63, 1 << 32, (1 << 64) - 1)]
        trees = [("<<", ("name", "a"), ("name", "count")),
                 (">>", ("name", "b"), ("name", "count")),
                 ("+", ("+", ("name", "a"), ("literal", 1, 4)), ("name", "b"))]
        trees.extend(expression(4) for _ in range(300))
        for tree in trees:
            compiled, width, _ = _expression(tree, names, widths, slots)
            for values in samples:
                with self.subTest(tree=tree, values=values):
                    expected, expected_width = evaluate(tree, values, widths)
                    self.assertEqual(width, expected_width)
                    self.assertEqual(compiled([values[name] for name in widths]), expected)

    def test_clocked_codegen_preserves_old_state_partial_writes_and_snapshots(self):
        # Deliberately overlap full/slice writes and separate edge blocks. All
        # RHS expressions must sample old state, with the last selected write
        # deciding each updated bit and no mutation of retained public values.
        hdl = """module Edge(input wire clk, input wire enable,
                             output wire [7:0] result, output wire [7:0] prior);
            reg [7:0] a = 8'd165;
            reg [7:0] b = 8'd90;
            assign result = a;
            assign prior = b;
            always @(posedge clk) begin
                a <= b;
                a[7:4] <= a[3:0];
                if (enable) begin
                    a[3:0] <= b[7:4];
                end else begin
                    a[1:0] <= 2'd3;
                end
            end
            always @(posedge clk) begin
                b <= a;
            end
        endmodule"""
        with patch("circuitlab.math.digital.compile_chips", return_value=[("Edge", (), (), hdl)]):
            sim = DigitalSimulation("")
        a, b = 165, 90
        for enabled in (0, 1, 1, 0, 1, 0):
            sim.set_input("enable", enabled)
            before = sim.values
            retained = before.copy()
            merged = b & 15 | (a & 15) << 4
            a, b = ((merged & 240 | b >> 4) if enabled else (merged & 252 | 3)), a
            after = sim.step()
            self.assertEqual((after["result"], after["prior"]), (a, b))
            self.assertEqual(after["clk"], 0)
            self.assertEqual(before, retained)
            self.assertIsNot(after, before)

    def test_deep_clocked_branches_keep_supported_nesting(self):
        body = "held <= 1'b1;"
        for _ in range(60):
            body = f"if (enabled) begin {body} end"
        hdl = f"""module Deep(input wire clk, input wire enabled, output wire y);
            reg held = 1'b0; assign y = held;
            always @(posedge clk) begin {body} end
        endmodule"""
        with patch("circuitlab.math.digital.compile_chips", return_value=[("Deep", (), (), hdl)]):
            sim = DigitalSimulation("")
        self.assertEqual(sim.step()["y"], 0)
        sim.set_input("enabled", 1)
        self.assertEqual(sim.step()["y"], 1)
