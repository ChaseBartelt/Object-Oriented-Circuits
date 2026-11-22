"""Exercise two independent Cortex-M7 external interrupts."""

import unittest

from circuitlab.math.digital import DigitalSimulation
from examples.advanced.CortexM7.firmware.build_firmware import read_model_source
from tests.exporters.test_cortex_m7_freertos_blocking import FIRMWARE


IRQ_ROM = """
chip M7FirmwareRom(address, word, valid) {
    address[32] = input;
    word[32] = output;
    valid = output;
    arm(address, word, valid) {
        .word 0x20000400;
        .word reset + 1;
        .org 0x40;
        .word irq0_handler + 1;
        .word irq1_handler + 1;
    reset:
        LDR r0, scr;
        MOVS r1, #16;
        STR r1, [r0];
        LDR r0, iser0;
        MOVS r1, #IRQ_ENABLE;
        STR r1, [r0];
        LDR r0, ipr0;
        MOVS r1, #128;
        STRB r1, [r0];
        MOVS r1, #IRQ1_PRIORITY;
        STRB r1, [r0, #1];
        LDR r1, [r0];
        LDR r0, ram;
        STR r1, [r0, #12];
    loop:
        WAIT_FOR_IRQ;
        MOVS r4, #42;
        B loop;
    irq0_handler:
        LDR r0, ram;
        LDR r1, [r0];
        ADDS r1, #1;
        STR r1, [r0];
        BX lr;
    irq1_handler:
        LDR r0, iabr0;
        LDR r1, [r0];
        LDR r0, ram;
        STR r1, [r0, #8];
        LDR r1, [r0, #4];
        ADDS r1, #1;
        STR r1, [r0, #4];
        BX lr;
        .align 2;
    scr: .word 0xE000ED10;
    iser0: .word 0xE000E100;
    ipr0: .word 0xE000E400;
    iabr0: .word 0xE000E300;
    ram: .word 0x20000000;
    }
}
"""


class CortexM7IrqTest(unittest.TestCase):
    def build(self, *, irq1_priority=128, enabled=3, wait="WFI"):
        source = read_model_source(FIRMWARE)
        before, begin, remainder = source.partition("// BEGIN LINKED FIRMWARE")
        _, end, after = remainder.partition("// END LINKED FIRMWARE")
        self.assertTrue(begin and end)
        rom = (IRQ_ROM.replace("IRQ1_PRIORITY", str(irq1_priority))
               .replace("IRQ_ENABLE", str(enabled)).replace("WAIT_FOR_IRQ", wait))
        return DigitalSimulation(before + begin + "\n" + rom + end + after)

    def until(self, sim, predicate):
        for _ in range(150):
            state = sim.step()
            self.assertFalse(state["FAULT"], f"PC={state['PC']:#x}, IR={state['IR']:#x}")
            if predicate(state):
                return state
        self.fail(f"Expected state was never reached at PC={sim.values['PC']:#x}.")

    @staticmethod
    def pulse(sim, *inputs):
        for name in inputs:
            sim.set_input(name, 1)
        sim.step()
        for name in inputs:
            sim.set_input(name, 0)

    def test_simultaneous_equal_priorities_select_irq0_first(self):
        sim = self.build()
        ready = self.until(sim, lambda state: state["SLEEPING"])
        self.assertEqual((ready["IRQ0_ENABLED"], ready["IRQ1_ENABLED"], ready["RAM3"]), (1, 1, 0x8080))
        self.pulse(sim, "IRQ0", "IRQ1")
        first = self.until(sim, lambda state: state["IPSR"] != 0)
        self.assertEqual((first["IPSR"], first["IRQ1_PENDING"]), (16, 1))
        second = self.until(sim, lambda state: state["IPSR"] == 17)
        self.assertEqual((second["EXCEPTION_DEPTH"], second["IRQ0_ACTIVE"], second["IRQ1_ACTIVE"]), (1, 0, 1))
        returned = self.until(sim, lambda state: state["SLEEPING"] and state["RAM1"] == 1)
        self.assertEqual((returned["RAM0"], returned["RAM1"], returned["RAM2"], returned["MSP"]),
                         (1, 1, 2, 0x20000400))

    def test_irq1_preempts_irq0_and_preserves_both_active_bits(self):
        sim = self.build(irq1_priority=64)
        ready = self.until(sim, lambda state: state["SLEEPING"])
        self.assertEqual(ready["RAM3"], 0x4080)
        self.pulse(sim, "IRQ0")
        self.until(sim, lambda state: state["IPSR"] == 16)
        self.pulse(sim, "IRQ1")
        nested = self.until(sim, lambda state: state["IPSR"] == 17)
        self.assertEqual((nested["EXCEPTION_DEPTH"], nested["IRQ0_ACTIVE"], nested["IRQ1_ACTIVE"]), (2, 1, 1))
        resumed = self.until(sim, lambda state: state["IPSR"] == 16)
        self.assertEqual((resumed["EXCEPTION_DEPTH"], resumed["IRQ0_ACTIVE"], resumed["IRQ1_ACTIVE"], resumed["RAM2"]),
                         (1, 1, 0, 3))
        returned = self.until(sim, lambda state: state["SLEEPING"])
        self.assertEqual((returned["RAM0"], returned["RAM1"], returned["MSP"]), (1, 1, 0x20000400))

    def test_disabled_irq1_pending_wakes_wfe_with_sevonpend(self):
        sim = self.build(enabled=0, wait="WFE")
        self.until(sim, lambda state: state["SLEEPING"])
        self.pulse(sim, "IRQ1")
        woke = self.until(sim, lambda state: state["R4"] == 42)
        self.assertEqual((woke["IPSR"], woke["IRQ1_ENABLED"], woke["IRQ1_PENDING"], woke["SLEEPING"]),
                         (0, 0, 1, 0))
