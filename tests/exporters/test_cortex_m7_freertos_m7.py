"""Run the upstream ARM_CM7/r0p1 FreeRTOS port with two integer tasks."""

from collections import Counter
import unittest

from tests.exporters.test_cortex_m7_freertos_blocking import ROOT, firmware_simulation


IMAGE = ROOT / "tests" / "fixtures" / "digital" / "freertos_v11_1_0_arm_cm7_r0p1.bin"
SVC, PENDSV, SYSTICK = 11, 14, 15
VECTORS = {SVC: 0x541, PENDSV: 0x651, SYSTICK: 0x6B5}
STACK_A, STACK_B = 0x20000248, 0x20000348
# The port's IT EQ predicated VSTMDB and VLDMIA instructions.
FP_TRANSFERS = {0x662: 0x8A10ED20, 0x6A2: 0x8A10ECB0}


class CortexM7FreeRtosM7Test(unittest.TestCase):
    def test_integer_tasks_run_on_the_upstream_m7_port(self):
        image = IMAGE.read_bytes()
        for exception, vector in VECTORS.items():
            offset = exception * 4
            self.assertEqual(int.from_bytes(image[offset:offset + 4], "little"), vector)
        sim = firmware_simulation(image)
        handlers = Counter()
        skipped = Counter()
        dispatches = []

        for _ in range(8000):
            before = sim.values
            after = sim.step()
            self.assertEqual(after["FAULT_CODE"], 0,
                             f"Cycle {sim.cycles}, PC {after['PC']:#x}, IR {after['IR']:#x}")
            previous, active = before["IPSR"], after["IPSR"]
            if not previous and active:
                vector = VECTORS[active]
                self.assertEqual((before["BUS_READ"], before["BUS_ADDR"], before["BUS_RDATA"]),
                                 (1, active * 4, vector))
                self.assertEqual(after["PC"], vector & ~1)
                self.assertEqual((after["CPACR"], after["FPCCR"]), (0x00F00000, 0xC0000000))
                handlers[active] += 1
            if active:
                self.assertEqual(after["SP"], after["MSP"])

            pc = before["PC"]
            if pc in FP_TRANSFERS:
                self.assertEqual((previous, before["IR"], before["WIDE"]), (PENDSV, FP_TRANSFERS[pc], 1))
                self.assertEqual((before["LR"] & 16, before["NZCV"] & 4), (16, 0))
                self.assertEqual((before["BUS_READ"], before["BUS_WRITE"]), (0, 0))
                self.assertEqual((after["PC"], after["LAST_PC"], after["RETIRED"]),
                                 (pc + 4, pc, before["RETIRED"] + 1))
                self.assertEqual((after["R0"], after["PSP"]), (before["R0"], before["PSP"]))
                skipped[pc] += 1

            if previous in (SVC, PENDSV) and not active:
                self.assertEqual(after["SP"], after["PSP"])
                dispatches.append(after["PSP"])
                if len(dispatches) == 5:
                    break
        else:
            self.fail(f"Expected SVC and four PendSV dispatches, got {dispatches}.")

        self.assertEqual(dispatches, [STACK_B, STACK_A, STACK_B, STACK_A, STACK_B])
        self.assertEqual(handlers, {SVC: 1, PENDSV: 4, SYSTICK: 4})
        self.assertEqual(skipped, {pc: 4 for pc in FP_TRANSFERS})
        self.assertGreater(after["RAM0"], 0)
        self.assertGreater(after["RAM1"], 0)
        self.assertEqual(after["RAM2"], 0)
