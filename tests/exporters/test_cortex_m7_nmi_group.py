"""Exercise Cortex-M7 NMI and the AIRCR priority split in a small ROM."""

import unittest

from circuitlab.math.digital import DigitalSimulation
from examples.advanced.CortexM7.firmware.build_firmware import read_model_source
from tests.exporters.test_cortex_m7_freertos_blocking import FIRMWARE


NMI, HARDFAULT, IRQ0, IRQ1 = 2, 3, 16, 17
STACK_TOP = 0x20000400
NMI_SET = 1 << 31
EXCEPTION_FIELD = 0x1FF

ROM = """
chip M7FirmwareRom(address, word, valid) {
    address[32] = input;
    word[32] = output;
    valid = output;
    arm(address, word, valid) {
        PROGRAM
    }
}
"""

NMI_PROGRAM = """
        .word 0x20000400, reset + 1, nmi_handler + 1, hardfault_handler + 1;
    reset:
        .hword 0xB673;         // CPSID if
        LDR r0, icsr;
        LDR r1, nmi_set;
        STR r1, [r0];          // Software NMI while PRIMASK and FAULTMASK are set.
        WFI;                   // An external NMI can wake the masked core.
        .hword 0xB661;         // CPSIE f, leaving PRIMASK set.
        UDF #42;               // UsageFault is disabled, so this enters HardFault.
        MOVS r4, #42;
    idle:
        B idle;

    nmi_handler:
        LDR r0, ram;
        LDR r1, [r0];
        CMP r1, #0;
        BNE count_nmi;
        LDR r2, icsr;
        LDR r3, [r2];
        STR r3, [r0, #8];      // Entry consumed the first pending NMI.
        LDR r3, nmi_set;
        STR r3, [r2];          // A running NMI cannot preempt itself.
        MOVS r3, #0;
        STR r3, [r2];          // Zero leaves NMIPENDSET unchanged.
        LDR r2, [r2];
        STR r2, [r0, #4];      // Snapshot ICSR while the second NMI is pending.
    count_nmi:
        ADDS r1, #1;
        STR r1, [r0];
        BX lr;

    hardfault_handler:
        LDR r0, [sp, #24];
        ADDS r0, #2;
        STR r0, [sp, #24];
        BX lr;

        .align 2;
    icsr: .word 0xE000ED04;
    nmi_set: .word 0x80000000;
    ram: .word 0x20000000;
"""

GROUP_PROGRAM = """
        .word 0x20000400, reset + 1;
        .org 0x40;
        .word irq0_handler + 1, irq1_handler + 1;
    reset:
        LDR r2, ram;
        LDR r0, aircr;
        LDR r1, [r0];
        STR r1, [r2];
        LDR r1, valid_key;
        STR r1, [r0];
        LDR r1, [r0];
        STR r1, [r2, #4];
        LDR r1, invalid_key;
        STR r1, [r0];
        LDR r1, [r0];
        STR r1, [r2, #8];

        LDR r0, iser0;
        MOVS r1, #3;
        STR r1, [r0];
        LDR r0, ipr0;
        MOVS r1, #96;
        STRB r1, [r0];         // IRQ0 group 1, subpriority 2.
        MOVS r1, #64;
        STRB r1, [r0, #1];     // IRQ1 group 1, subpriority 0.
        MOVS r3, #8;
        MOVS r1, #96;
        .hword 0xF381, 0x8811; // MSR BASEPRI, r1. Both IRQs share its group.
    delay:
        SUBS r3, #1;
        BNE delay;
        MOVS r1, #0;
        .hword 0xF381, 0x8811; // MSR BASEPRI, r1
    idle:
        WFI;
        B idle;

    irq0_handler:
        LDR r0, ram;
        LDR r1, [r0, #12];
        ADDS r1, #1;
        STR r1, [r0, #12];
        BX lr;
    irq1_handler:
        LDR r0, ram;
        LDR r1, [r0, #16];
        ADDS r1, #1;
        STR r1, [r0, #16];
        BX lr;

        .align 2;
    aircr: .word 0xE000ED0C;
    valid_key: .word 0x05FA0500;
    invalid_key: .word 0x12340200;
    iser0: .word 0xE000E100;
    ipr0: .word 0xE000E400;
    ram: .word 0x20000000;
"""


class CortexM7NmiGroupTest(unittest.TestCase):
    def build(self, program):
        before, begin, remainder = read_model_source(FIRMWARE).partition("// BEGIN LINKED FIRMWARE")
        _, end, after = remainder.partition("// END LINKED FIRMWARE")
        self.assertTrue(begin and end)
        return DigitalSimulation(before + begin + "\n" + ROM.replace("PROGRAM", program) + end + after)

    def until(self, sim, predicate):
        for _ in range(150):
            state = sim.values
            self.assertFalse(state["FAULT"], f"PC={state['PC']:#x}, IR={state['IR']:#x}")
            if predicate(state):
                return state
            sim.step()
        self.fail(f"Expected state was never reached at cycle {sim.cycles}, PC={sim.values['PC']:#x}.")

    @staticmethod
    def pulse(sim, *inputs):
        for name in inputs:
            sim.set_input(name, 1)
        sim.step()
        for name in inputs:
            sim.set_input(name, 0)

    def enter_nmi(self, sim, depth):
        vector = self.until(sim, lambda state: state["BUS_READ"] and state["BUS_ADDR"] == 8)
        entered = sim.step()
        self.assertEqual((entered["IPSR"], entered["PC"], entered["EXCEPTION_DEPTH"]),
                         (NMI, vector["BUS_RDATA"] & ~1, depth))
        self.assertEqual((entered["NMI_PENDING"], entered["NMI_ACTIVE"]), (0, 1))
        return entered

    def test_nmi_software_pending_wakes_through_masks_and_preempts_hardfault(self):
        sim = self.build(NMI_PROGRAM)
        request = self.until(sim, lambda state: state["NMI_PENDING"])
        self.assertEqual((request["PRIMASK"], request["FAULTMASK"]), (1, 1))
        self.enter_nmi(sim, 1)
        repended = self.until(sim, lambda state: state["NMI_PENDING"])
        self.assertEqual((repended["IPSR"], repended["EXCEPTION_DEPTH"]), (NMI, 1))
        self.enter_nmi(sim, 1)
        asleep = self.until(sim, lambda state: state["SLEEPING"])
        self.assertEqual((asleep["RAM0"], asleep["PRIMASK"], asleep["FAULTMASK"], asleep["IPSR"]), (2, 1, 1, 0))
        self.assertEqual(asleep["RAM2"] & (NMI_SET | EXCEPTION_FIELD), NMI)
        self.assertEqual(asleep["RAM1"] & (NMI_SET | EXCEPTION_FIELD), NMI_SET | NMI)

        self.pulse(sim, "NMI")
        self.enter_nmi(sim, 1)
        hardfault = self.until(sim, lambda state: state["IPSR"] == HARDFAULT)
        self.assertEqual((hardfault["RAM0"], hardfault["MSP"], hardfault["FAULTMASK"]),
                         (3, STACK_TOP - 32, 0))

        self.pulse(sim, "NMI")
        interrupted_pc = sim.values["PC"]
        nested = self.enter_nmi(sim, 2)
        self.assertEqual((nested["MSP"], nested["PRIMASK"]), (STACK_TOP - 64, 1))
        returned = self.until(sim, lambda state: state["IPSR"] == HARDFAULT)
        self.assertEqual((returned["PC"], returned["MSP"], returned["RAM0"], returned["NMI_ACTIVE"]),
                         (interrupted_pc, hardfault["MSP"], 4, 0))
        finished = self.until(sim, lambda state: state["R4"] == 42)
        self.assertEqual((finished["IPSR"], finished["MSP"]), (0, STACK_TOP))

    def test_aircr_key_and_group_priority_control_arbitration(self):
        sim = self.build(GROUP_PROGRAM)
        configured = self.until(sim, lambda state: state["BASEPRI"] == 0x60)
        self.assertEqual((configured["PRIGROUP"], configured["RAM0"], configured["RAM1"], configured["RAM2"]),
                         (5, 0xFA050000, 0xFA050500, 0xFA050500))
        self.assertEqual((configured["IRQ0_PRIORITY"], configured["IRQ1_PRIORITY"]), (0x60, 0x40))
        self.pulse(sim, "IRQ0", "IRQ1")
        self.assertEqual((sim.values["IRQ0_PENDING"], sim.values["IRQ1_PENDING"]), (1, 1))
        while sim.values["BASEPRI"]:
            self.assertEqual(sim.values["IPSR"], 0)
            self.assertLess(sim.cycles, 120)
            sim.step()
        first = self.until(sim, lambda state: state["IPSR"] != 0)
        self.assertEqual((first["IPSR"], first["IRQ0_PENDING"]), (IRQ1, 1))
        second = self.until(sim, lambda state: state["IPSR"] == IRQ0)
        self.assertEqual((second["EXCEPTION_DEPTH"], second["IRQ1_ACTIVE"]), (1, 0))
        asleep = self.until(sim, lambda state: state["SLEEPING"])
        self.assertEqual((asleep["RAM3"], asleep["RAM4"]), (1, 1))

        self.pulse(sim, "IRQ0")
        self.until(sim, lambda state: state["IPSR"] == IRQ0)
        self.pulse(sim, "IRQ1")
        self.assertEqual((sim.values["IPSR"], sim.values["IRQ1_PENDING"]), (IRQ0, 1))
        delivered = self.until(sim, lambda state: state["IPSR"] == IRQ1)
        self.assertEqual((delivered["EXCEPTION_DEPTH"], delivered["IRQ0_ACTIVE"]), (1, 0))
        finished = self.until(sim, lambda state: state["SLEEPING"])
        self.assertEqual((finished["RAM3"], finished["RAM4"], finished["MSP"]), (2, 2, STACK_TOP))
