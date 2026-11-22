"""Exercise Cortex-M7 FP frame layout, lazy stacking, and exception return."""

from struct import pack
import unittest

from tests.exporters.test_cortex_m7_freertos_blocking import (
    HARDFAULT, ICSR, IRQ0, MSP_TOP, RAM_BASE, SYSTICK, firmware_simulation, thumb_image,
)


PSP_TOP = RAM_BASE + 0x804
RESUME_PC = 0x98
INTEGER_HANDLER = 0xBC
FP_HANDLER = 0xC6
FLOAT_REGISTERS = (0x3F800000, 0xBF800000, 0x40000000)
THREAD_FPSCR = 0x00400000
FP_MODE_BITS = 0x07C00000


def fp_interrupt_image(*, use_psp, lazy, fp_handler):
    """Configure the FPU, seed registers, and pend SysTick from Thread mode."""
    instructions = (
        0x4826, 0x4927, 0x6001,                 # CPACR = full access to CP10/11.
        0xF3BF, 0x8F4F, 0xF3BF, 0x8F6F,        # DSB and ISB.
        0x4825, 0xF8DF, 0x1094, 0x6001,         # Enable ASPEN and select LSPEN.
        0x4825, 0xF380, 0x8809,                 # Initialize PSP.
        0x2002 if use_psp else 0x2000,
        0xF380, 0x8814, 0xF3BF, 0x8F6F,         # Select the Thread stack and ISB.
        0x4822, 0x2101, 0x6001,                 # Enable external IRQ0.
        0x4821, 0x2180, 0x7001,                 # SysTick priority is below IRQ0.
        0x4921, 0xEE00, 0x1A10,                 # VMOV S0, R1.
        0x4920, 0xEE07, 0x1A90,                 # VMOV S15, R1.
        0x4920, 0xEE08, 0x1A10,                 # VMOV S16, R1.
        0x491F, 0xEEE1, 0x1A10,                 # VMSR FPSCR, R1.
        0x4C1F, 0x481F, 0x2104, 0x0609, 0x6001, # Set ICSR.PENDSTSET.
        0xEE10, 0x0A10, 0x6020,                 # Save S0 to RAM0 after exception return.
        0xEE17, 0x0A90, 0x6060,                 # Save S15 to RAM1.
        0xEE18, 0x0A10, 0x60A0,                 # Save S16 to RAM2.
        0xEEF1, 0x0A10, 0x60E0,                 # Save FPSCR to RAM3.
        0xF3EF, 0x8014, 0x6120,                 # Save CONTROL to RAM4.
        0x2001, 0x6160, 0xE7FE,                 # Signal completion in RAM5 and idle.
        0x2000, 0xBF18, 0xEE00, 0x0A10, 0x4770, # Integer ISR skips a predicated VMOV.
        0xEEF1, 0x0A10, 0x6160,                 # Log the FP handler's initial FPSCR.
        0x2055, 0xEE00, 0x0A10,                 # Replace the lower FP register values.
        0xEE07, 0x0A90,
        0x2000, 0xEEE1, 0x0A10, 0x4770,         # Clear FPSCR and return.
        0xE7FE,                                 # Unexpected vector.
    )
    constants = (
        0xE000ED88, 0x00F00000, 0xE000EF34, 0xC0000000 if lazy else 0x80000000,
        PSP_TOP, 0xE000E100, 0xE000ED23, *FLOAT_REGISTERS, THREAD_FPSCR, RAM_BASE, ICSR,
    )
    code = pack(f"<{len(instructions)}H", *instructions) + pack("<13I", *constants)
    systick = FP_HANDLER if fp_handler else INTEGER_HANDLER
    return thumb_image(code, unused=0xDF, systick=systick | 1, irq0=FP_HANDLER | 1)


def fp_denied_image(privileged_only):
    """Skip one FP operation before a faulting VMOV, then resume in Thread."""
    instructions = (
        0x480F, 0x4910, 0x6001,                 # Configure CPACR.
        0xF3BF, 0x8F4F, 0xF3BF, 0x8F6F,        # DSB and ISB.
        0x2001 if privileged_only else 0x2000,
        0xF380, 0x8814, 0xF3BF, 0x8F6F,         # Set CONTROL and ISB.
        0x2000, 0xBF18, 0xEE00, 0x0A10,         # VMOVNE S0,R0 with false condition.
        0x2033, 0xEE00, 0x0A10,                 # VMOV S0,R0 faults.
        0x4808, 0x215A, 0x6001, 0xE7FE,         # RAM0 = 0x5A after recovery.
        0x4A07, 0x6811, 0x4A05, 0x6051,         # HardFault saves CFSR to RAM1.
        0x9806, 0x3004, 0x9006, 0x4770,         # Skip the 32-bit instruction and return.
        0xE7FE,                                 # Unexpected vector.
    )
    constants = (0xE000ED88, 0x00500000 if privileged_only else 0, RAM_BASE, 0xE000ED28)
    code = pack(f"<{len(instructions)}H", *instructions) + pack("<4I", *constants)
    return thumb_image(code, unused=0x83, hardfault=0x73)


class CortexM7FloatingPointTest(unittest.TestCase):
    def test_eager_frame_restores_registers_on_msp_and_psp(self):
        for use_psp in (False, True):
            with self.subTest(use_psp=use_psp):
                self.check_interrupt(use_psp=use_psp, lazy=False, fp_handler=True)

    def test_lazy_frame_is_skipped_when_handler_does_not_execute_fp(self):
        self.check_interrupt(use_psp=True, lazy=True, fp_handler=False)

    def test_lazy_frame_is_materialized_by_nested_fp_handler(self):
        self.check_interrupt(use_psp=True, lazy=True, fp_handler=False, nested=True)

    def test_denied_fp_instruction_escalates_to_hardfault(self):
        for privileged_only in (False, True):
            with self.subTest(privileged_only=privileged_only):
                sim = firmware_simulation(fp_denied_image(privileged_only))
                skipped = entered = returned = False
                for _ in range(100):
                    before = sim.values
                    after = sim.step()
                    self.assertEqual(after["FAULT_CODE"], 0)
                    if before["PC"] == 0x60 and after["PC"] == 0x64:
                        skipped = True
                        self.assertEqual((after["IPSR"], after["CFSR"]), (0, 0))
                    if not before["IPSR"] and after["IPSR"] == HARDFAULT:
                        entered = True
                        self.assertEqual((before["PC"], after["LR"], after["MSP"]),
                                         (0x66, 0xFFFFFFF9, MSP_TOP - 32))
                    if before["IPSR"] == HARDFAULT and not after["IPSR"]:
                        returned = True
                        self.assertEqual((after["PC"], after["MSP"], after["PRIVILEGED"]),
                                         (0x6A, MSP_TOP, int(not privileged_only)))
                    if after["RAM0"] == 0x5A:
                        break
                self.assertTrue(skipped and entered and returned)
                self.assertEqual((after["RAM0"], after["RAM1"], after["CFSR"], after["HFSR"]),
                                 (0x5A, 1 << 19, 1 << 19, 1 << 30))

    def check_interrupt(self, *, use_psp, lazy, fp_handler, nested=False):
        sim = firmware_simulation(fp_interrupt_image(use_psp=use_psp, lazy=lazy, fp_handler=fp_handler))
        stack_top = PSP_TOP if use_psp else MSP_TOP
        frame = (stack_top & ~7) - 104
        fp_addresses = set(range(frame + 32, frame + 100, 4))
        writes = {}
        reads = set()
        fp_writer_contexts = set()
        transitions = []
        handler_fpscr = []
        pulse = False

        for _ in range(240):
            before = sim.values
            after = sim.step()
            self.assertEqual(after["FAULT_CODE"], 0, f"Cycle {sim.cycles}, PC {after['PC']:#x}")
            if pulse:
                sim.set_input("IRQ0", 0)
                pulse = False
            previous, active = before["IPSR"], after["IPSR"]
            address = before["BUS_ADDR"]
            if before["BUS_WRITE"]:
                if frame <= address < frame + 104:
                    writes[address] = before["BUS_WDATA"]
                    if address in fp_addresses:
                        fp_writer_contexts.add(previous)
                if address == RAM_BASE + 20 and previous:
                    handler_fpscr.append(before["BUS_WDATA"] & FP_MODE_BITS)
            if before["BUS_READ"] and address in fp_addresses:
                reads.add(address)

            if previous != active:
                transitions.append((previous, active))
                if previous == 0 and active == SYSTICK:
                    self.assertEqual(after["LR"], 0xFFFFFFED if use_psp else 0xFFFFFFE9)
                    self.assertEqual(after["PSP" if use_psp else "MSP"], frame)
                    self.assertEqual(after["SP"], MSP_TOP if use_psp else frame)
                    self.assertEqual(after["CONTROL"] & 4, 0)
                    self.assertEqual(after["FP_LAZY_ACTIVE"], int(lazy))
                    self.assertEqual(fp_addresses & writes.keys(), set() if lazy else fp_addresses)
                    if lazy:
                        self.assertEqual(after["FPCAR"], frame + 32)
                    if nested:
                        sim.set_input("IRQ0", 1)
                        pulse = True
                if active == IRQ0:
                    self.assertEqual((after["MSP"], after["PSP"], after["LR"]),
                                     (MSP_TOP - 32, frame, 0xFFFFFFF1))
                    self.assertEqual((after["FP_LAZY_ACTIVE"], after["FPCAR"]), (1, frame + 32))
                if previous == IRQ0 and active == SYSTICK:
                    self.assertEqual(after["MSP"], MSP_TOP)
                    self.assertEqual(after["CONTROL"] & 4, 0)
                    self.assertEqual(after["FP_LAZY_ACTIVE"], 0)
                if active == 0:
                    self.assertEqual((after["PC"], after["SP"], after["CONTROL"]),
                                     (RESUME_PC, stack_top, 6 if use_psp else 4))
                    self.assertEqual(after["FP_LAZY_ACTIVE"], 0)

            if after["RAM5"] == 1:
                break
        else:
            self.fail("The interrupted floating-point program did not resume.")

        expected_transitions = ([(0, SYSTICK), (SYSTICK, IRQ0), (IRQ0, SYSTICK), (SYSTICK, 0)] if nested
                                else [(0, SYSTICK), (SYSTICK, 0)])
        self.assertEqual(transitions, expected_transitions)
        self.assertEqual(writes[frame + 24], RESUME_PC)
        self.assertEqual(writes[frame + 28] & (1 << 9), (stack_top & 4) << 7)
        self.assertNotIn(frame + 100, writes)
        if not lazy or nested:
            self.assertEqual((writes[frame + 32], writes[frame + 92], writes[frame + 96]),
                             (FLOAT_REGISTERS[0], FLOAT_REGISTERS[1], THREAD_FPSCR))
            self.assertEqual(fp_addresses & writes.keys(), fp_addresses)
            self.assertEqual(reads, fp_addresses)
            self.assertEqual(fp_writer_contexts, {IRQ0 if nested else 0})
            self.assertEqual(handler_fpscr, [0])
        else:
            self.assertEqual((fp_writer_contexts, reads, handler_fpscr), (set(), set(), []))
        self.assertEqual(tuple(after[f"RAM{i}"] for i in range(5)),
                         (*FLOAT_REGISTERS, THREAD_FPSCR, 6 if use_psp else 4))
