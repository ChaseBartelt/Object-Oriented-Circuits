"""Keep the public APIs while lower-level modules remain independently usable."""

from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ArchitectureTest(unittest.TestCase):
    def run_isolated(self, source):
        result = subprocess.run(
            [sys.executable, "-c", source], cwd=ROOT,
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_document_and_linker_do_not_import_the_compiler_facade(self):
        for module in ("builder", "document", "linked"):
            with self.subTest(module=module):
                self.run_isolated(f"""
import importlib
import sys
importlib.import_module('circuitlab.language.{module}')
assert 'circuitlab.language.compiler' not in sys.modules
assert not any(name.startswith(('PySide6', 'circuitlab.viewer')) for name in sys.modules)
""")

    def test_compiler_compatibility_exports_keep_the_same_types(self):
        from circuitlab import compiler as legacy
        from circuitlab.language import builder, compiler, document

        self.assertIs(legacy, compiler)
        for name in ("Compiler", "Group", "Battery", "compile_program", "resolve_alias"):
            self.assertIs(getattr(compiler, name), getattr(builder, name))
        for name in ("CircuitProgram", "compile_document", "elaborate_document"):
            self.assertIs(getattr(compiler, name), getattr(document, name))

        rows = ["V1(5) between(supply, 0);", "R1(1k) between(supply, 0);"]
        direct = compiler.compile_circuit(rows)
        expanded = document.elaborate_document("\n".join(rows))
        constructed = builder.compile_program(expanded, "auto")
        self.assertEqual(direct, constructed)
        self.assertEqual(direct.components[1].terminal_nets, ("supply", "0"))

    def test_nonlinear_solver_does_not_load_linear_dispatch(self):
        self.run_isolated("""
import sys
from circuitlab.math.nonlinear import TimeDomain
from circuitlab.math.results import Network
assert 'circuitlab.math.linear' not in sys.modules
assert 'circuitlab.math.analysis' not in sys.modules
assert not any(name.startswith(('PySide6', 'circuitlab.viewer')) for name in sys.modules)
""")

    def test_result_types_are_shared_by_old_and_new_solver_paths(self):
        from circuitlab import simulation
        from circuitlab.compiler import compile_circuit
        from circuitlab.math import linear, results
        from circuitlab.math.nonlinear import TimeDomain

        self.assertIs(simulation, linear)
        self.assertIs(linear.Network, results.Network)
        self.assertIs(linear.SimulationBackend, results.SimulationBackend)
        circuit = compile_circuit("V1(5) between(supply, 0);\nR1(1k) between(supply, 0);")
        native = linear.NodalBackend().solve(circuit, ac=False)
        implicit = TimeDomain(circuit).operating_point()
        self.assertIsInstance(native, results.Network)
        self.assertIsInstance(implicit, results.Network)
        self.assertEqual(native.nodes, implicit.nodes)
        self.assertAlmostEqual(native.voltage[native.nodes.index("supply")].real, 5)
        self.assertAlmostEqual(implicit.voltage[implicit.nodes.index("supply")], 5)

    def test_layout_compatibility_exports_keep_the_same_implementations(self):
        from circuitlab import layout, routing
        from circuitlab.layout import automatic, editing, geometry, legacy

        self.assertIs(layout.Layout, geometry.Layout)
        self.assertIs(layout.compact, automatic.compact)
        self.assertIs(layout.route, legacy.route)
        self.assertIs(routing.move_symbol, editing.move_symbol)

    def test_geometry_can_place_either_source_polarity_without_qt(self):
        self.run_isolated("""
import sys
from circuitlab.layout.geometry import grid_symbol
from circuitlab.model.graph import Component
positive = Component('V1', 'V', 'supply', '0', 5)
negative = Component('V2', 'V', '0', 'supply', 5)
assert grid_symbol(positive, 0, 0, {'0'})[1:3] == ((2.15, 0), (2.15, -2.5))
assert grid_symbol(negative, 0, 0, {'0'})[1:3] == ((2.15, -2.5), (2.15, 0))
assert not any(name.startswith(('PySide6', 'circuitlab.viewer')) for name in sys.modules)
""")


if __name__ == "__main__":
    unittest.main()
