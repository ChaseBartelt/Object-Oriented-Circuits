"""Run floating-point task switching through the upstream FreeRTOS M7 port."""

from collections import Counter
import unittest

from tests.exporters.test_cortex_m7_freertos_blocking import ROOT, firmware_simulation
from tests.exporters.test_cortex_m7_freertos_m7 import PENDSV, SVC, SYSTICK


IMAGE = ROOT / "tests" / "fixtures" / "digital" / "freertos_v11_1_0_arm_cm7_r0p1_fp.bin"
VECTORS = {SVC: 0x5E1, PENDSV: 0x6F1, SYSTICK: 0x755}
STACK_A, STACK_B = 0x20000250, 0x20000350
FLOAT_SAVE, FLOAT_RESTORE = 0x702, 0x742
CANARY_SIGNALS = ("S0", "S15", "S16", "S31", "FPSCR")
CANARIES = {
    STACK_A: (0x3F800000, 0x40C00000, 0x40800000, 0x40E00000, 1),
    STACK_B: (0x40000000, 0x41400000, 0x41000000, 0x41600000, 2),
}
SUMS = {STACK_A: 0x40A00000, STACK_B: 0x41200000}


class CortexM7FreeRtosFloatTest(unittest.TestCase):
    def test_eager_context_preserves_both_tasks(self):
        self.verify_switches(IMAGE.read_bytes(), lazy=False)

    def test_lazy_context_is_materialized_by_the_upstream_pendsv(self):
        image = bytearray(IMAGE.read_bytes())
        # Change only the application's FPCCR constant: MOV.W R2, #0x80000000
        # becomes MOV.W R2, #0xC0000000. The upstream port stays byte-identical.
        fpccr_setup = slice(0x82, 0x86)
        self.assertEqual(image[fpccr_setup], b"\x4f\xf0\x00\x42")
        image[fpccr_setup] = b"\x4f\xf0\x40\x42"
        # Exercise the same simulator path with a full 4 KiB firmware image.
        self.verify_switches(bytes(image).ljust(4096, b"\0"), lazy=True)

    def verify_switches(self, image, *, lazy):
        for exception, vector in VECTORS.items():
            offset = exception * 4
            self.assertEqual(int.from_bytes(image[offset:offset + 4], "little"), vector)
        sim = firmware_simulation(image)
        dispatches = []
        handlers = Counter()
        upper_words = Counter()
        frame_writes = {}
        deferred_writes = {}
        deferred_saves = 0

        for _ in range(9000):
            before = sim.values
            after = sim.step()
            self.assertEqual((after["FAULT_CODE"], after["RAM2"]), (0, 0),
                             f"Cycle {sim.cycles}, PC {after['PC']:#x}, IR {after['IR']:#x}")
            previous, active = before["IPSR"], after["IPSR"]
            if not previous and before["FP_ACTIVE_FRAME"] and before["BUS_WRITE"]:
                frame_writes[before["BUS_ADDR"]] = before["BUS_WDATA"]
            if not previous and active:
                vector = VECTORS[active]
                self.assertEqual((before["BUS_READ"], before["BUS_ADDR"], before["BUS_RDATA"]),
                                 (1, active * 4, vector))
                self.assertEqual(after["PC"], vector & ~1)
                handlers[active] += 1
                if active != SVC:
                    stack, frame = before["PSP"], after["PSP"]
                    s0, s15, _, _, fpscr = CANARIES[stack]
                    expected_fpccr = 0xC0000001 if lazy else 0x80000000
                    self.assertEqual((frame, after["LR"], after["FPCCR"]),
                                     (stack - 104, 0xFFFFFFED, expected_fpccr))
                    if lazy:
                        self.assertEqual(after["FPCAR"], frame + 32)
                        self.assertEqual(sorted(frame_writes), list(range(frame, frame + 32, 4)))
                    else:
                        self.assertEqual(tuple(frame_writes[frame + i] for i in (32, 36, 92, 96)),
                                         (s0, SUMS[stack], s15, fpscr))
                frame_writes.clear()
            if active:
                self.assertEqual(after["SP"], after["MSP"])

            pc = before["PC"]
            if pc == FLOAT_SAVE and before["BUS_WRITE"]:
                if before["FP_LAZY_ACTIVE"]:
                    deferred_writes[before["BUS_ADDR"]] = before["BUS_WDATA"]
                else:
                    upper_words[pc] += 1
            if pc == FLOAT_RESTORE and before["BUS_READ"]:
                upper_words[pc] += 1
            if previous == PENDSV and before["FP_LAZY_ACTIVE"] and not after["FP_LAZY_ACTIVE"]:
                self.assertEqual((active, pc, after["PC"]), (PENDSV, FLOAT_SAVE, FLOAT_SAVE))
                area = before["FPCAR"]
                stack = area + 72
                s0, s15, _, _, fpscr = CANARIES[stack]
                self.assertEqual(sorted(deferred_writes), list(range(area, area + 68, 4)))
                self.assertEqual(tuple(deferred_writes[area + i] for i in (0, 4, 60, 64)),
                                 (s0, SUMS[stack], s15, fpscr))
                deferred_writes.clear()
                deferred_saves += 1

            if previous in (SVC, PENDSV) and not active:
                stack = after["PSP"]
                self.assertEqual(after["SP"], stack)
                dispatches.append(stack)
                if len(dispatches) >= 3:
                    self.assertEqual(after["CONTROL"] & 6, 6)
                    self.assertEqual(tuple(after[name] for name in CANARY_SIGNALS), CANARIES[stack])
                else:
                    self.assertEqual(after["CONTROL"] & 6, 2)
                if len(dispatches) == 5:
                    break
        else:
            self.fail(f"Expected five task dispatches, got {dispatches}.")

        self.assertEqual(dispatches, [STACK_B, STACK_A, STACK_B, STACK_A, STACK_B])
        self.assertEqual(handlers, {SVC: 1, PENDSV: 4, SYSTICK: 4})
        self.assertEqual(upper_words, {FLOAT_SAVE: 64, FLOAT_RESTORE: 48})
        self.assertEqual(deferred_saves, handlers[PENDSV] if lazy else 0)
        self.assertGreaterEqual(min(after["RAM0"], after["RAM1"]), 2)
        self.assertEqual((after["RAM3"], after["RAM4"]), (SUMS[STACK_A], SUMS[STACK_B]))
