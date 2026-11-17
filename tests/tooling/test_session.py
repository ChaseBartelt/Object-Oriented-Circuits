"""Document compilation and source ownership without a GUI dependency."""

from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from circuitlab.language.linked import CHIP_LIBRARY
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
