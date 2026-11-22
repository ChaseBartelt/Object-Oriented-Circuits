"""Exercise direct UsageFault recovery and escalation on the OOC firmware core."""

from pathlib import Path
import unittest

from circuitlab.math.digital import DigitalSimulation
from examples.advanced.CortexM7.firmware.build_firmware import read_model_source


MODEL = (Path(__file__).resolve().parents[2] / "examples" / "advanced"
         / "CortexM7" / "CortexM7_Firmware.ckt")
MSP_TOP = 0x20001000
DIVBYZERO = 1 << 25
FORCED = 1 << 30
UDIV_R4_R2_R3 = 0xF4F3FBB2

ROM = """
chip M7FirmwareRom(address, word, valid) {
    address[32] = input;
    word[32] = output;
    valid = output;
    arm(address, word, valid) {
        .word 0x20001000;
        .word reset + 1;
        .org 0x0C;
        .word recover + 1;
        .org 0x18;
        .word recover + 1;
    reset:
        MOVS r2, #13;
        MOVS r3, #0;
        MOVS r4, #42;
        UDIV r4, r2, r3;   // CCR reset: return zero without taking a fault
        LDR r0, shcsr;
        LDR r1, enable;
        STR r1, [r0];
        LDR r0, ccr;
        MOVS r1, #16;
        STR r1, [r0];
        MOVS r4, #42;
        UDIV r4, r2, r3;   // Configured UsageFault
        .hword 0xB672;    // CPSID i
        UDIV r4, r2, r3;   // PRIMASK escalates to HardFault
        .hword 0xB662;    // CPSIE i
        LDR r0, shcsr;
        STR r3, [r0];
        UDIV r4, r2, r3;   // Disabled UsageFault escalates to HardFault
        LDR r0, ram;
        STR r4, [r0];
    idle:
        B idle;
    recover:
        LDR r0, ram;
        LDR r1, shcsr;
        LDR r2, [r1];
        STR r2, [r0, #4];
        LDR r1, cfsr;
        LDR r2, [r1];
        STR r2, [r1];
        LDR r1, hfsr;
        LDR r2, [r1];
        STR r2, [r1];
        LDR r0, [sp, #24];
        ADDS r0, #4;
        STR r0, [sp, #24];
        BX lr;
        .align 2;
    shcsr: .word 0xE000ED24;
    cfsr: .word 0xE000ED28;
    ccr: .word 0xE000ED14;
    hfsr: .word 0xE000ED2C;
    enable: .word 0x40000;
    ram: .word 0x20000000;
    }
}
"""


class CortexM7UsageFaultTest(unittest.TestCase):
    def test_trapped_division_recovers_and_escalates_when_masked_or_disabled(self):
        before, begin, tail = read_model_source(MODEL).partition("// BEGIN LINKED FIRMWARE")
        _, end, after = tail.partition("// END LINKED FIRMWARE")
        self.assertTrue(begin and end)
        sim = DigitalSimulation(before + begin + "\n" + ROM + end + after)
        expected = [(6, 0, 0x40008), (3, FORCED, 0x40000), (3, FORCED, 0)]
        entries, returns, divisions = [], [], []
        for _ in range(200):
            old = sim.values
            state = sim.step()
            self.assertFalse(state["FAULT"], f"PC={state['PC']:#x}, fault={state['FAULT_CODE']}")
            if state["RETIRED"] != old["RETIRED"] and state["LAST_IR"] == UDIV_R4_R2_R3:
                divisions.append(state["R4"])
            if not old["IPSR"] and state["IPSR"]:
                entries.append((state["IPSR"], state["HFSR"]))
                self.assertEqual((state["CFSR"], state["MSP"], state["R4"]), (DIVBYZERO, MSP_TOP - 32, 42))
                self.assertEqual(old["BUS_ADDR"], state["IPSR"] * 4)
                fault_pc = old["PC"]
            elif old["IPSR"] and not state["IPSR"]:
                returns.append(state["RAM1"])
                self.assertEqual((state["PC"], state["MSP"], state["R4"]), (fault_pc + 4, MSP_TOP, 42))
                self.assertEqual((state["CFSR"], state["HFSR"]), (0, 0))
            if state["RAM0"] == 42:
                break
        else:
            self.fail("Thread mode did not finish after recovering from three traps.")

        self.assertEqual(divisions, [0])
        self.assertEqual(entries, [(exception, hfsr) for exception, hfsr, _ in expected])
        self.assertEqual(returns, [shcsr for _, _, shcsr in expected])
