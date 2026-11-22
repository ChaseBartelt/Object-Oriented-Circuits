"""Run the linked upstream FreeRTOS kernel and Cortex-M port in OOC."""

from collections import Counter
from pathlib import Path
import runpy
import unittest

from circuitlab.math.digital import DigitalSimulation
from examples.advanced.CortexM7.firmware.build_firmware import read_model_source


ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "examples" / "advanced" / "CortexM7" / "CortexM7_Firmware.ckt"
IMAGE = ROOT / "tests" / "fixtures" / "digital" / "freertos_v11_1_0_arm_cm3.bin"
RAM_BASE = 0x20000000
SVC, PENDSV, SYSTICK = 11, 14, 15
ICSR = 0xE000ED04
PENDSVSET = 1 << 28

# Symbols from the fixture's link map. The vector addresses are read from the image.
KERNEL_ENTRIES = {
    0x154: "xTaskCreateStatic",
    0x2AC: "vTaskStartScheduler",
    0x344: "xTaskIncrementTick",
    0x444: "vTaskSwitchContext",
}
TASKS = ((0x20000150, 0x20000250), (0x20000250, 0x20000350))


def stack_task(address):
    return next((i for i, (base, limit) in enumerate(TASKS) if base <= address < limit), None)


class CortexM7FreeRtosTest(unittest.TestCase):
    def test_upstream_kernel_schedules_two_static_tasks(self):
        image = IMAGE.read_bytes()
        prefix, begin, remainder = read_model_source(FIRMWARE).partition("// BEGIN LINKED FIRMWARE")
        _, end, suffix = remainder.partition("// END LINKED FIRMWARE")
        self.assertTrue(begin and end)
        builder = runpy.run_path(str(FIRMWARE.parent / "firmware" / "build_firmware.py"))
        sim = DigitalSimulation(prefix + begin + "\n" + builder["rom_body"](image, rtos=True) + end + suffix)

        visits = Counter()
        handlers = []
        scheduled = []
        writes = Counter()
        switch_requested = False
        before = sim.values
        for _ in range(8000):
            after = sim.step()
            self.assertEqual(after["FAULT_CODE"], 0,
                             f"Cycle {sim.cycles}, PC {after['PC']:#x}, IR {after['IR']:#x}")
            if after["RETIRED"] != before["RETIRED"] and after["LAST_PC"] in KERNEL_ENTRIES:
                visits[KERNEL_ENTRIES[after["LAST_PC"]], before["IPSR"]] += 1

            previous, active = before["IPSR"], after["IPSR"]
            if active:
                self.assertEqual(after["SP"], after["MSP"])
            if before["BUS_WRITE"] and before["BUS_ADDR"] == ICSR and before["BUS_WDATA"] & PENDSVSET:
                self.assertEqual((previous, active, after["PENDSV_PENDING"]), (SYSTICK, SYSTICK, 1))
                switch_requested = True
            if previous != active and active:
                self.assertEqual(previous, 0)
                self.assertIn(active, (SVC, PENDSV, SYSTICK))
                vector_offset = 4 * active
                vector = int.from_bytes(image[vector_offset:vector_offset + 4], "little")
                self.assertEqual((before["BUS_READ"], before["BUS_ADDR"], before["BUS_RDATA"]),
                                 (1, vector_offset, vector))
                self.assertEqual(after["PC"], vector & ~1)
                handlers.append(active)
                if active == PENDSV:
                    self.assertTrue(switch_requested)
                    switch_requested = False
            if previous in (SVC, PENDSV) and not active:
                task = stack_task(after["PSP"])
                self.assertIsNotNone(task)
                self.assertEqual(after["SP"], after["PSP"])
                if scheduled:
                    self.assertNotEqual(task, scheduled[-1])
                scheduled.append(task)

            address = before["BUS_ADDR"]
            if before["BUS_WRITE"] and address in (RAM_BASE, RAM_BASE + 4) and before["BUS_WDATA"]:
                task = (address - RAM_BASE) // 4
                counter = f"RAM{task}"
                self.assertEqual((previous, before["BUS_SIZE"], stack_task(before["PSP"])), (0, 2, task))
                self.assertEqual(after[counter], before[counter] + (1, 3)[task])
                writes[task] += 1
            if len(scheduled) >= 5 and writes[0] >= 2 and writes[1] >= 2:
                break
            before = after
        else:
            self.fail(f"Expected five dispatches and both counters to advance, got {scheduled} and {writes}")

        self.assertEqual(handlers[0], SVC)
        self.assertEqual(handlers.count(SVC), 1)
        # The kernel creates the Idle task after the application's two static tasks.
        self.assertEqual(visits["xTaskCreateStatic", 0], 3)
        self.assertEqual(visits["vTaskStartScheduler", 0], 1)
        self.assertGreaterEqual(visits["xTaskIncrementTick", SYSTICK], 4)
        self.assertGreaterEqual(visits["vTaskSwitchContext", PENDSV], 4)
        self.assertEqual(after["RAM2"], 0)
