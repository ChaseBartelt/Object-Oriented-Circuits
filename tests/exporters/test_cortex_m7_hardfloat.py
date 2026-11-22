"""Boot the optimized Cortex-M7 C demo with a hardware single-precision ABI."""

from pathlib import Path
import runpy
import unittest

from circuitlab.math.digital import DigitalSimulation
from examples.advanced.CortexM7.firmware.build_firmware import read_model_source


ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "examples" / "advanced" / "CortexM7" / "CortexM7_Firmware.ckt"
IMAGE = ROOT / "tests" / "fixtures" / "digital" / "cortex_m7_hardfloat.bin"
IDLE_PC = 0x40


class CortexM7HardFloatTest(unittest.TestCase):
    def test_compiler_generated_hardfloat_calibration(self):
        source = read_model_source(MODEL)
        start, begin, remaining = source.partition("// BEGIN LINKED FIRMWARE")
        _, end, tail = remaining.partition("// END LINKED FIRMWARE")
        builder = runpy.run_path(str(MODEL.parent / "firmware" / "build_firmware.py"))
        rom = builder["rom_body"](IMAGE.read_bytes(), large=True)
        sim = DigitalSimulation(start + begin + "\n" + rom + end + tail)
        for _ in range(512):
            values = sim.step()
            self.assertEqual(values["FAULT_CODE"], 0, f"PC={values['PC']:#x}, IR={values['IR']:#x}")
            if values["PC"] == IDLE_PC:
                break
        else:
            self.fail("The hard-float firmware did not return to its reset loop.")

        self.assertEqual(tuple(values[f"RAM{index}"] for index in range(6)),
                         (98, 0b1001, 0x41A80000, 0x40A80000, 8, 4))
        self.assertEqual(values["MSP"], 0x20001000)
        self.assertEqual(values["CPACR"], 0x00F00000)
