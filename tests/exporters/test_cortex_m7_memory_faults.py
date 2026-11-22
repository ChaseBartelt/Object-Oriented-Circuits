"""Verify precise MemManage and BusFault recovery on the Cortex-M7 model."""

import unittest

from circuitlab.math.digital import DigitalSimulation
from examples.advanced.CortexM7.firmware.build_firmware import read_model_source
from tests.exporters.test_cortex_m7_freertos_blocking import FIRMWARE, MSP_TOP


HARDFAULT, MEMMANAGE, BUSFAULT = 3, 4, 5
IACCVIOL, DACCVIOL, MMARVALID = 1 << 0, 1 << 1, 1 << 7
PRECISERR, BFARVALID, FORCED = 1 << 9, 1 << 15, 1 << 30
MEM_ENABLE, BUS_ENABLE = 1 << 16, 1 << 17
MEM_ACTIVE, BUS_ACTIVE = 1 << 0, 1 << 1
DATA_GUARD, UNMAPPED_DATA = 0x20000100, 0x40000000

ROM = """
chip M7FirmwareRom(address, word, valid) {
    address[32] = input;
    word[32] = output;
    valid = output;
    arm(address, word, valid) {
        .word 0x20001000;
        .word reset + 1;
        .org 0x0C;
        .word hardfault + 1, memmanage + 1, busfault + 1;
        .org 0x40;
    reset:
        LDR r0, mpu_ctrl;
        MOVS r1, #0;
        STR r1, [r0, #4];       // MPU_RNR
        LDR r2, blocked_data;
        STR r2, [r0, #8];       // MPU_RBAR
        LDR r2, data_region;
        STR r2, [r0, #12];      // MPU_RASR
        MOVS r1, #1;
        STR r1, [r0, #4];
        LDR r2, xn_base;
        STR r2, [r0, #8];
        LDR r2, code_region;
        STR r2, [r0, #12];
        MOVS r1, #5;            // MPU enable plus privileged background map
        STR r1, [r0];
        DSB SY;
        ISB SY;

        LDR r0, shcsr;
        LDR r1, both_enabled;
        STR r1, [r0];
        LDR r0, shpr1;
        MOVS r1, #32;
        STRB r1, [r0];
        MOVS r1, #48;
        STRB r1, [r0, #1];
        MOVS r4, #42;
        B trials;

        .org 0xC0;
    trials:
        LDR r5, blocked_data;
        LDR r4, [r5];           // 0xC2: MemManage data violation
        LDR r5, unmapped_data;
        LDR r4, [r5];           // 0xC6: precise BusFault
        LDR r5, xn_entry;
        BX r5;
    after_xn:
        LDR r0, shcsr;
        LDR r1, bus_enabled;
        STR r1, [r0];
        LDR r5, blocked_data;
        LDR r4, [r5];           // 0xD4: disabled MemManage escalates
        MOVS r1, #48;
        .hword 0xF381, 0x8811;  // MSR BASEPRI, r1
        LDR r5, unmapped_data;
        LDR r4, [r5];           // 0xDE: BASEPRI masks BusFault at equal priority
        MOVS r1, #0;
        .hword 0xF381, 0x8811;  // MSR BASEPRI, r1
        LDR r0, results;
        STR r4, [r0];
    idle:
        B idle;

    hardfault: B recover;
    memmanage: B recover;
    busfault: B recover;
    recover:
        LDR r0, [sp, #24];
        LDR r1, results;
        STR r0, [r1, #4];       // RAM1 records the stacked faulting PC
        LDR r2, shcsr;
        LDR r3, [r2];
        STR r3, [r1, #8];       // RAM2 records handler enable and active bits
        LDR r2, cfsr;
        LDR r3, [r2];
        STR r3, [r2];
        LDR r2, hfsr;
        LDR r3, [r2];
        STR r3, [r2];
        LDR r2, xn_base;
        CMP r0, r2;
        BEQ skip_xn;
        ADDS r0, #2;
        B resume;
    skip_xn:
        LDR r0, xn_resume;
    resume:
        STR r0, [sp, #24];
        BX lr;

        .align 2;
    mpu_ctrl: .word 0xE000ED94;
    shcsr: .word 0xE000ED24;
    shpr1: .word 0xE000ED18;
    cfsr: .word 0xE000ED28;
    hfsr: .word 0xE000ED2C;
    blocked_data: .word 0x20000100;
    unmapped_data: .word 0x40000000;
    data_region: .word 0x10000009; // 32 bytes with no data permissions
    code_region: .word 0x16000009; // 32 bytes, read-only and execute-never
    both_enabled: .word 0x30000;
    bus_enabled: .word 0x20000;
    results: .word 0x20000000;
    xn_base: .word blocked_code;
    xn_entry: .word blocked_code + 1;
    xn_resume: .word after_xn;

        .org 0x200;
    blocked_code:
        MOVS r4, #99;
        B blocked_code;
    }
}
"""


class CortexM7MemoryFaultTest(unittest.TestCase):
    def test_precise_memory_faults_route_recover_and_escalate(self):
        before, begin, tail = read_model_source(FIRMWARE).partition("// BEGIN LINKED FIRMWARE")
        _, end, after = tail.partition("// END LINKED FIRMWARE")
        self.assertTrue(begin and end)
        sim = DigitalSimulation(before + begin + "\n" + ROM + end + after)
        expected = [
            # exception, faulting PC, resume PC, CFSR, HFSR, SHCSR readback
            (MEMMANAGE, 0xC2, 0xC4, DACCVIOL | MMARVALID, 0, MEM_ENABLE | BUS_ENABLE | MEM_ACTIVE),
            (BUSFAULT, 0xC6, 0xC8, PRECISERR | BFARVALID, 0, MEM_ENABLE | BUS_ENABLE | BUS_ACTIVE),
            (MEMMANAGE, 0x200, 0xCC, IACCVIOL, 0, MEM_ENABLE | BUS_ENABLE | MEM_ACTIVE),
            (HARDFAULT, 0xD4, 0xD6, DACCVIOL | MMARVALID, FORCED, BUS_ENABLE),
            (HARDFAULT, 0xDE, 0xE0, PRECISERR | BFARVALID, FORCED, BUS_ENABLE),
        ]
        fault_pcs = {case[1] for case in expected}
        entries = returns = 0
        for _ in range(300):
            old = sim.values
            state = sim.step()
            self.assertFalse(state["FAULT"], f"PC={state['PC']:#x}, fault={state['FAULT_CODE']}")
            if state["RETIRED"] != old["RETIRED"]:
                self.assertNotIn(state["LAST_PC"], fault_pcs)
            if not old["IPSR"] and state["IPSR"]:
                self.assertLess(entries, len(expected), "Unexpected extra fault")
                exception, fault_pc, _, cfsr, hfsr, _ = expected[entries]
                self.assertEqual((old["BUS_READ"], old["BUS_ADDR"], old["PC"]), (1, exception * 4, fault_pc))
                self.assertEqual((state["IPSR"], state["PC"]), (exception, old["BUS_RDATA"] & ~1))
                self.assertEqual((state["CFSR"], state["HFSR"], state["MSP"], state["R4"]),
                                 (cfsr, hfsr, MSP_TOP - 32, 42))
                if cfsr & MMARVALID:
                    self.assertEqual(state["MMFAR"], DATA_GUARD)
                if cfsr & BFARVALID:
                    self.assertEqual(state["BFAR"], UNMAPPED_DATA)
                entries += 1
            elif old["IPSR"] and not state["IPSR"]:
                _, fault_pc, resume_pc, _, _, shcsr = expected[returns]
                self.assertEqual((state["RAM1"], state["RAM2"]), (fault_pc, shcsr))
                self.assertEqual((state["PC"], state["MSP"], state["R4"]), (resume_pc, MSP_TOP, 42))
                self.assertEqual((state["CFSR"], state["HFSR"]), (0, 0))
                returns += 1
            if state["RAM0"] == 42:
                break
        else:
            self.fail("Thread mode did not finish after recovering from the faults.")

        self.assertEqual((entries, returns), (len(expected), len(expected)))
