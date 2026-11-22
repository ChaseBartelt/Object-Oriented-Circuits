"""Document compilation and source ownership without a GUI dependency."""
# Keep accepted text, compilation, drawing, and diagnostics transactional without importing Qt.
# A model error may still yield a useful package preview, while a new revision invalidates old
# results.

from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from circuitlab.language.linked import CHIP_LIBRARY
from circuitlab.math.digital import DigitalSimulation
from circuitlab.tooling.session import CircuitSession

ROOT = Path(__file__).resolve().parents[2]
DIVIDER = "V1(5) between(supply, 0);\nR1(1k) between(supply, out);\nR2(1k) between(out, 0);\n"


class SessionTest(unittest.TestCase):
    def test_session_compiles_without_importing_qt_or_editor(self):
        script = """
import sys
from circuitlab.tooling.session import CircuitSession
session = CircuitSession(source='V = 12\\nR = 3')
session.compile()
assert session.solution.calculated['I'] == 4
assert not any(name.startswith('PySide6') for name in sys.modules)
assert 'circuitlab.viewer.legacy' not in sys.modules
assert 'circuitlab.viewer.editor' not in sys.modules
"""
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_load_is_transactional_and_source_change_invalidates_results(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "good.ckt"
            path.write_text(DIVIDER)
            bad = path.with_name("bad.ckt")
            bad.write_bytes(b"\xff")
            session = CircuitSession()
            session.load(path)
            session.compile()
            self.assertFalse(session.issues)
            self.assertEqual(len(session.drawing.components), 3)
            before = session.path, session.source, session.revision, session.solution
            with self.assertRaises(UnicodeError):
                session.load(bad)
            self.assertEqual((session.path, session.source, session.revision, session.solution), before)
            session.set_source("not valid source")
            self.assertIsNone(session.solution)
            self.assertIsNone(session.drawing)
            session.compile()
            self.assertTrue(session.issues)
            self.assertIsNone(session.solution)

    def test_quantity_only_solution_does_not_report_a_render_error(self):
        session = CircuitSession(source="V = 12\nR = 3")
        session.compile()
        self.assertEqual(session.solution.calculated["I"], 4)
        self.assertFalse(session.issues)
        self.assertIsNone(session.drawing)

    def test_digital_chip_is_previewed_separately_from_analog_analysis(self):
        session = CircuitSession(source="chip invert(a, y) { a = input; y = output; y = !a; }")
        session.compile()
        self.assertTrue(session.preview_only)
        self.assertFalse(session.issues)
        self.assertIsNone(session.solution)
        self.assertEqual(session.drawing.instances[0].name, "invert")
        self.assertIn("assign y = !a;", session.verilog())

    def test_structural_module_file_has_an_interface_preview_without_a_fake_simulation(self):
        boards = ROOT / "examples/advanced/CortexM7/boards"
        for path in sorted(boards.glob("*_platform.ckt")):
            with self.subTest(path=path.name):
                session = CircuitSession(path=path, source=path.read_text(encoding="utf-8"))
                session.compile()
                self.assertFalse(session.issues)
                self.assertTrue(session.preview_only)
                self.assertTrue(session.interface_preview)
                self.assertIsNone(session.digital_design)
                self.assertIsNone(session.solution)
                self.assertTrue(session.drawing.instances)
                platform = next(instance for instance in session.drawing.instances
                                if instance.name == "M7FirmwarePlatform")
                self.assertIn(("clk", "@M7FirmwarePlatform.clk"), platform.pins)
                self.assertFalse(platform.model_available)
                self.assertEqual(platform.body, ())

    def test_changing_away_from_a_structural_module_clears_interface_preview(self):
        session = CircuitSession(source="chip Parent(a,y) { a=input; y=output; Child(a,y); }")
        session.compile()
        self.assertTrue(session.interface_preview)
        session.set_source("chip Leaf(a,y) { a=input; y=output; y=!a; }")
        self.assertFalse(session.interface_preview)
        session.compile()
        self.assertFalse(session.issues)
        self.assertFalse(session.interface_preview)
        self.assertIsNotNone(session.digital_design)
        self.assertIn("module Leaf", session.verilog())

    def test_internal_states_remain_digital_in_preview_and_export(self):
        session = CircuitSession(source="""chip StateBits(a, y, z) {
            a = input;
            y, z = output;
            phase[32] = 100;
            led_state;
            flag = 1;
            y = a ^ phase[6];
            z = !led_state & flag;
        }""")
        session.compile()
        self.assertTrue(session.preview_only)
        self.assertFalse(session.issues)
        self.assertIsNone(session.solution)
        self.assertFalse(session.drawing.components)
        self.assertEqual(session.drawing.instances[0].pins, (("a", "a"), ("y", "y"), ("z", "z")))
        verilog = session.verilog()
        self.assertIn("reg [31:0] phase = 32'd100;", verilog)
        self.assertIn("reg led_state = 1'b0;", verilog)
        self.assertIn("reg flag = 1'b1;", verilog)
        self.assertIn("assign y = a ^ phase[6];", verilog)
        self.assertIn("assign z = !led_state & flag;", verilog)
        self.assertNotRegex(verilog, r"\b(always|posedge|negedge)\b")

    def test_digital_assembly_resolves_nested_includes_for_viewer_and_simulation(self):
        entry = ROOT / "main.ckt"
        source = """include("chips/bundle.ckt");
include("chips/inverter.ckt");
main TOP { a = input; y = output; Invert(a, y); }
"""
        inverter = entry.parent / "chips" / "inverter.ckt"
        parts = {
            entry.parent / "chips" / "bundle.ckt": 'include("inverter.ckt");',
            inverter: "chip Invert(a, y) { a = input; y = output; y = !a; }",
        }
        session = CircuitSession(path=entry, source=source, read_source=parts.__getitem__)
        session.compile()
        self.assertFalse(session.issues)
        instance, = session.drawing.instances
        self.assertEqual((instance.name, instance.path, instance.line), ("Invert", entry, 3))
        self.assertIn("module TOP", session.verilog())
        sim = DigitalSimulation(source, path=entry, read_source=parts.__getitem__)
        self.assertEqual(sim.values["y"], 1)
        sim.set_input("a", 1)
        self.assertEqual(sim.values["y"], 0)

        parts[inverter] = parts[inverter].replace("!a", "missing")
        session.compile()
        issue, = session.issues
        self.assertEqual((issue.path, issue.line), (inverter, 1))

    def test_library_alias_and_pinout_file_preview_without_editor(self):
        for relative, expected in (("logic/74HC00.ckt", "SN74HC00"), ("converters/ADS1115.ckt", "ADS1115")):
            with self.subTest(relative=relative):
                session = CircuitSession()
                session.load(CHIP_LIBRARY / relative)
                session.compile()
                self.assertTrue(session.preview_only)
                self.assertFalse(session.issues)
                self.assertIsNone(session.solution)
                self.assertEqual(session.drawing.instances[0].name, expected)

    def test_unavailable_model_still_has_an_electrical_package_view(self):
        session = CircuitSession(source='include("@chips/converters/ADS1115.ckt");\nmain { ADS1115(a,b,0,c,d,e,f,v,sda,scl); }')
        session.compile()
        self.assertIsNone(session.solution)
        self.assertEqual(session.drawing.instances[0].name, "ADS1115")
        self.assertIn("simulation model is required", session.issues[0].message)

    def test_pin_overrides_never_change_source(self):
        source = DIVIDER + '# @pin {"node": "out", "name": "Original"}\n'
        session = CircuitSession(source=source)
        self.assertEqual(session.pins, {"out": "Original"})
        session.pin_overrides = {"out": None, "supply": "Input"}
        self.assertEqual(session.pins, {"supply": "Input"})
        self.assertEqual(session.source, source)
