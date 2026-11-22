"""Structural libraries expose interfaces without pretending to be assemblies."""

from pathlib import Path
import unittest

from circuitlab.exporters.verilog import compile_chips
from circuitlab.language.digital_structure import digital_library_interfaces
from circuitlab.model.graph import CircuitError


BOARDS = (Path(__file__).resolve().parents[2] / "examples/advanced/CortexM7/boards")
PARENT = "chip Parent(a, y) { a = input; y = output; Missing(a, y); }"


class DigitalLibraryInterfacesTest(unittest.TestCase):
    def test_generated_platform_fragments_expose_ports_and_child_calls(self):
        for name in (
            "DaisySeed_AudioPassthru_platform.ckt",
            "DaisySeed_STM32H750_Boot_platform.ckt",
            "DaisySeed_STM32H750_HAL_Blink_platform.ckt",
        ):
            with self.subTest(name=name):
                source = (BOARDS / name).read_text(encoding="utf-8")
                modules = digital_library_interfaces(source)
                by_name = {module[0]: module for module in modules}
                self.assertTrue({"M7FirmwarePlatform", "M7FirmwareMemoryBank",
                                 "M7FirmwareMemoryWindow"}.issubset(by_name))
                platform = by_name["M7FirmwarePlatform"]
                self.assertEqual(platform[1][0], ("clk", "input"))
                self.assertIn(("data", "output"), platform[1])
                self.assertTrue(any(row.startswith("STM32H750StartupRegisters(")
                                    for row in platform[2]))
                self.assertTrue(all(module[3] == "" for module in modules))

    def test_interface_preview_leaves_dependencies_for_the_entry_document(self):
        modules = digital_library_interfaces('include("not-present.ckt");\n' + PARENT)
        self.assertEqual(modules, (("Parent", (("a", "input"), ("y", "output")),
                                   ("a = input", "y = output", "Missing(a, y)"), ""),))

    def test_main_and_behavior_only_documents_keep_the_existing_compiler_path(self):
        self.assertIsNone(digital_library_interfaces(
            PARENT + " main { a = input; y = output; Parent(a, y); }"))
        self.assertIsNone(digital_library_interfaces(
            "chip Invert(a, y) { a = input; y = output; y = !a; }"))
        self.assertIsNone(digital_library_interfaces(
            "chip Clocked(a, y) { a = input; y = output; clock(a) { Missing(a, y); } }"))

    def test_structural_preview_still_validates_ports_and_state_declarations(self):
        for source in (
            "chip Bad(a, y) { a = input; Missing(a, y); }",
            "chip Bad(a, a) { a = input; Missing(a, a); }",
            "chip Bad(a, y) { a = input; y, extra = output; Missing(a, y); }",
            "chip Bad(a, y) { a = input; y = output; state[3] = 8; Missing(a, y); }",
            "chip Bad(a, y) { a = input; y = output; state[0] = 0; Missing(a, y); }",
            "chip Bad(a, y) { a = input; y = output; state = invalid; Missing(a, y); }",
            "chip Bad(a, y) { a = input; y = output; Missing(a, y);",
        ):
            with self.subTest(source=source), self.assertRaises(CircuitError):
                digital_library_interfaces(source)

    def test_export_directs_structural_libraries_to_an_entry_document(self):
        with self.assertRaisesRegex(CircuitError, "entry document with main"):
            compile_chips(PARENT)


if __name__ == "__main__":
    unittest.main()
