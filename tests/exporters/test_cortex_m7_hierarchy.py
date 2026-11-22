"""The hierarchy entry boots the connected CPU instead of placeholder models."""

from pathlib import Path
import unittest

from circuitlab.math.digital import DigitalSimulation
from circuitlab.tooling.session import CircuitSession


ENTRY = (Path(__file__).resolve().parents[2] / "examples/advanced/CortexM7"
         / "CortexM7_Hierarchy.ckt")


class CortexM7HierarchyTest(unittest.TestCase):
    def test_hierarchy_entry_exposes_working_blocks_and_runs_the_boot_program(self):
        source = ENTRY.read_text(encoding="utf-8")
        session = CircuitSession(source=source, path=ENTRY)
        self.assertTrue(session.is_digital())
        session.compile()
        self.assertFalse(session.issues)
        self.assertFalse(session.interface_preview)
        self.assertIsNotNone(session.digital_design)
        self.assertIsNotNone(session.drawing)
        self.assertTrue(all(instance.model_available for instance in session.drawing.instances))
        names = {instance.name for instance in session.drawing.instances}
        self.assertTrue({"M7FirmwareCore", "M7FirmwareRegisters", "M7FirmwareRom",
                         "M7FirmwareRAM"} <= names)

        sim = DigitalSimulation(source, design=session.digital_design)
        for _ in range(24):
            sim.step()
            self.assertEqual(sim.values["FAULT"], 0)
            self.assertEqual(sim.values["CFSR"], 0)
            self.assertEqual(sim.values["HFSR"], 0)
        self.assertEqual(sim.values["RAM0"], 13)
        self.assertEqual(sim.values["MSP"], 0x20000400)
        self.assertEqual(sim.values["PC"], 0x0C)


if __name__ == "__main__":
    unittest.main()
