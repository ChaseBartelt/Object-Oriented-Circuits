"""Exercise all NVIC banks and external exceptions beyond the two legacy aliases."""

import random
import unittest

from circuitlab.math.digital import DigitalSimulation
from examples.advanced.CortexM7.firmware.build_firmware import read_model_source
from tests.exporters.test_cortex_m7_freertos_blocking import FIRMWARE


ISER, ICER, ISPR, ICPR, IABR, IPR, STIR = (
    0xE000E100, 0xE000E180, 0xE000E200, 0xE000E280,
    0xE000E300, 0xE000E400, 0xE000EF00,
)
IRQ_IDS = (31, 35, 92, 139, 239)

NVIC_HARNESS = """
main NVIC_BANKS {
    CLK, RESET, READ, WRITE = input;
    ADDRESS[32], WRITE_DATA[32], SIZE[2] = input;
    LINES[240], ACK[240], ACTIVE[240], RETURN[240] = input;
    DATA[32], PRIORITY[8], ACTIVE_PRIORITY[8], EXCEPTION[9], PENDING[240] = output;
    MAPPED, IRQ0_ENABLED, IRQ0_PENDING, IRQ1_ENABLED, IRQ1_PENDING = output;
    IRQ0_PRIORITY[8], IRQ1_PRIORITY[8] = output;
    zero, expanded, aliases[10] = input;
    M7FirmwareNVIC(CLK, RESET, READ, WRITE, ADDRESS, SIZE, WRITE_DATA,
                   zero, zero, zero, zero, DATA, MAPPED,
                   IRQ0_ENABLED, IRQ0_PENDING, IRQ0_PRIORITY,
                   zero, zero, zero, zero, IRQ1_ENABLED, IRQ1_PENDING, IRQ1_PRIORITY,
                   aliases, expanded, LINES, ACK, ACTIVE, RETURN,
                   EXCEPTION, PRIORITY, ACTIVE_PRIORITY, PENDING);
}
"""


class CortexM7NvicBanksTest(unittest.TestCase):
    def setUp(self):
        interrupts = FIRMWARE.parent / "core" / "system" / "interrupts.ckt"
        self.sim = DigitalSimulation(interrupts.read_text(encoding="utf-8") + NVIC_HARNESS)
        self.sim.set_input("READ", 1)
        self.sim.set_input("SIZE", 2)
        self.sim.set_input("aliases", 32)
        self.sim.set_input("expanded", 1)

    def write(self, address, value, size=2):
        self.sim.set_input("ADDRESS", address)
        self.sim.set_input("SIZE", size)
        self.sim.set_input("WRITE_DATA", value)
        self.sim.set_input("WRITE", 1)
        self.sim.step()
        self.sim.set_input("WRITE", 0)

    def read(self, address, size=2):
        self.sim.set_input("ADDRESS", address)
        self.sim.set_input("SIZE", size)
        self.assertEqual(self.sim.values["MAPPED"], 1)
        return self.sim.values["DATA"]

    def test_all_eight_banks_and_the_last_implemented_irq(self):
        self.assertEqual(self.read(0xE000E004), 7)
        self.write(0xE000E004, 0)
        self.assertEqual(self.read(0xE000E004), 7)
        for bank in range(8):
            self.write(ISER + 4 * bank, 0xFFFFFFFF)
            expected = 0xFFFF if bank == 7 else 0xFFFFFFFF
            self.assertEqual(self.read(ISER + 4 * bank), expected)
            self.assertEqual(self.read(ICER + 4 * bank), expected)
            self.write(ISPR + 4 * bank, 0xFFFFFFFF)
            self.assertEqual(self.read(ISPR + 4 * bank), expected)
            self.write(ICPR + 4 * bank, 0xFFFFFFFF)
            self.assertEqual(self.read(ISPR + 4 * bank), 0)
            self.write(ICER + 4 * bank, 0xFFFFFFFF)
            self.assertEqual(self.read(ISER + 4 * bank), 0)
        self.write(ISER + 28, 1 << 15)
        self.write(STIR, 239)
        self.assertEqual((self.sim.values["EXCEPTION"], self.sim.values["PENDING"]),
                         (255, 1 << 239))
        self.write(STIR, 240)
        self.assertEqual(self.sim.values["PENDING"], 1 << 239)

    def test_priority_bytes_and_active_bits_across_every_bank(self):
        for number in IRQ_IDS:
            self.write(ISER + 4 * (number // 32), 1 << (number % 32))
            self.write(IPR + number, 0x8F, size=0)
            self.assertEqual(self.read(IPR + number, size=0), 0x80)
            self.write(STIR, number)
        # A full-priority tie resolves by exception number across bank boundaries.
        self.assertEqual((self.sim.values["EXCEPTION"], self.sim.values["PRIORITY"]), (47, 128))
        self.write(IPR + 239, 0x4F, size=0)
        self.assertEqual((self.sim.values["EXCEPTION"], self.sim.values["PRIORITY"]), (255, 64))
        active = (1 << 35) | (1 << 239)
        self.sim.set_input("ACTIVE", active)
        self.assertEqual(self.sim.values["ACTIVE_PRIORITY"], 64)
        self.assertEqual(self.read(IABR + 4), 8)
        self.assertEqual(self.read(IABR + 28), 1 << 15)
        self.write(IPR + 236, 0xA1B2C3D4)
        self.assertEqual(self.read(IPR + 236), 0xA0B0C0D0)
        self.assertEqual(self.sim.values["ACTIVE_PRIORITY"], 128)

    def test_every_irq_has_an_independent_priority_byte(self):
        for number in range(240):
            self.write(IPR + number, number, size=0)
        for number in range(240):
            self.assertEqual(self.read(IPR + number, size=0), number & 0xF0)
        self.sim.set_input("ADDRESS", IPR + 240)
        self.assertEqual(self.sim.values["MAPPED"], 0)

    def test_grouped_encoder_matches_priority_and_number_order(self):
        priorities = [(number * 7 % 16) << 4 for number in range(240)]
        for bank in range(8):
            self.write(ISER + 4 * bank, 0xFFFFFFFF)
        for number, priority in enumerate(priorities):
            self.write(IPR + number, priority, size=0)
        rng = random.Random(240)
        for _ in range(24):
            pending = rng.sample(range(240), 12)
            active = rng.sample(range(240), 7)
            self.sim.set_input("LINES", 0)
            self.sim.set_input("ACK", (1 << 240) - 1)
            self.sim.step()
            self.sim.set_input("ACK", 0)
            self.sim.set_input("LINES", sum(1 << number for number in pending))
            self.sim.set_input("ACTIVE", sum(1 << number for number in active))
            self.sim.step()
            winner = min(pending, key=lambda number: (priorities[number], number))
            self.assertEqual((self.sim.values["EXCEPTION"], self.sim.values["PRIORITY"],
                              self.sim.values["ACTIVE_PRIORITY"]),
                             (winner + 16, priorities[winner], min(priorities[number] for number in active)))

    def test_acknowledgement_clear_and_level_retrigger(self):
        mask = 1 << 139
        self.write(ISER + 16, 1 << 11)
        self.sim.set_input("LINES", mask)
        self.sim.step()
        self.assertEqual(self.sim.values["EXCEPTION"], 155)
        # ICPR cannot erase a level that is still asserted at the device.
        self.write(ICPR + 16, 1 << 11)
        self.assertEqual(self.sim.values["PENDING"], mask)
        self.sim.set_input("ACK", mask)
        self.sim.step()
        self.sim.set_input("ACK", 0)
        self.assertEqual(self.sim.values["PENDING"], 0)
        self.sim.step()
        self.assertEqual(self.sim.values["PENDING"], 0)
        self.sim.set_input("RETURN", mask)
        self.sim.step()
        self.sim.set_input("RETURN", 0)
        self.assertEqual(self.sim.values["PENDING"], mask)
        self.sim.set_input("LINES", 0)
        self.write(ICPR + 16, 1 << 11)
        self.assertEqual(self.sim.values["PENDING"], 0)


def irq_rom(priorities, *, group=0, mask="", wait="WFI", enabled=True):
    """Assemble real vectors, register setup and short handlers for chosen IRQs."""
    vectors, setup, handlers, constants = [], [], [], []
    for slot, (number, priority) in enumerate(priorities.items()):
        vectors.extend((f".org {4 * (number + 16)};", f".word handler{number} + 1;"))
        if enabled:
            setup.extend((f"LDR r0, iser{number};", f"LDR r1, bit{number};", "STR r1, [r0];"))
        setup.extend((f"LDR r0, ipr{number};", f"MOVS r1, #{priority};", "STRB r1, [r0];"))
        handlers.extend((f"handler{number}:", "LDR r0, ram;", f"LDR r1, [r0, #{4 * slot}];",
                         "ADDS r1, #1;", f"STR r1, [r0, #{4 * slot}];", "BX lr;"))
        constants.extend((f"iser{number}: .word {ISER + 4 * (number // 32)};",
                          f"bit{number}: .word {1 << (number % 32)};",
                          f"ipr{number}: .word {IPR + number};"))
    body = "\n".join((".word 0x20000400;", ".word reset + 1;", *vectors,
                      ".org 0x400;", "reset:", "LDR r0, aircr;", "LDR r1, group;", "STR r1, [r0];",
                      "LDR r0, scr;", "MOVS r1, #16;", "STR r1, [r0];", *setup, mask,
                      "loop:", f"{wait};", "MOVS r4, #42;", "B loop;", *handlers, ".align 2;",
                      "aircr: .word 0xE000ED0C;", f"group: .word {0x05FA0000 | group << 8};",
                      "scr: .word 0xE000ED10;", "ram: .word 0x20000000;", *constants))
    return ("chip M7FirmwareRom(address, word, valid) {\n"
            "address[32] = input; word[32], valid = output;\n"
            f"arm(address, word, valid) {{\n{body}\n}}\n}}\n")


class CortexM7FullIrqTest(unittest.TestCase):
    def build(self, priorities, **options):
        source = read_model_source(FIRMWARE)
        before, begin, remainder = source.partition("// BEGIN LINKED FIRMWARE")
        _, end, after = remainder.partition("// END LINKED FIRMWARE")
        self.assertTrue(begin and end)
        # Expose the same mask wires used by the bus as probes for assertions.
        after = after.replace("external_pending_lines[240] = wire;", "external_pending_lines[240] = output;")
        after = after.replace("external_return_lines[240] = wire;", "external_return_lines[240] = output;")
        return DigitalSimulation(before + begin + "\n" + irq_rom(priorities, **options) + end + after)

    def until(self, sim, predicate, limit=220):
        for _ in range(limit):
            state = sim.step()
            self.assertFalse(state["FAULT"], f"PC={state['PC']:#x}, IR={state['IR']:#x}")
            if predicate(state):
                return state
        self.fail(f"Expected state was never reached at PC={sim.values['PC']:#x}.")

    @staticmethod
    def pulse(sim, *numbers):
        sim.set_input("IRQ_LINES", sum(1 << number for number in numbers))
        sim.step()
        sim.set_input("IRQ_LINES", 0)

    def test_high_vectors_take_all_irqs_and_restore_the_stack(self):
        sim = self.build(dict.fromkeys(IRQ_IDS, 128))
        self.until(sim, lambda state: state["SLEEPING"])
        self.pulse(sim, *IRQ_IDS)
        for slot, number in enumerate(IRQ_IDS):
            entered = self.until(sim, lambda state: state["IPSR"] == number + 16)
            self.assertEqual(entered["EXCEPTION_DEPTH"], 1)
            returned = self.until(sim, lambda state: state[f"RAM{slot}"] == 1 and state["IPSR"] == 0)
            self.assertEqual(returned["MSP"], 0x20000400)
        self.until(sim, lambda state: state["SLEEPING"])

    def test_high_irq_preemption_and_group_subpriority(self):
        # PRIGROUP 5 leaves two group bits and two subpriority bits. 0x80 and
        # 0x90 share a group; 0x40 can preempt both, including IRQ239 (IPSR255).
        sim = self.build({35: 0x90, 92: 0x80, 239: 0x40}, group=5)
        self.until(sim, lambda state: state["SLEEPING"])
        self.pulse(sim, 35)
        self.until(sim, lambda state: state["IPSR"] == 51)
        self.pulse(sim, 92)
        sim.step()
        self.assertEqual((sim.values["IPSR"], sim.values["EXCEPTION_DEPTH"]), (51, 1))
        self.pulse(sim, 239)
        nested = self.until(sim, lambda state: state["IPSR"] == 255)
        self.assertEqual(nested["EXCEPTION_DEPTH"], 2)
        self.assertEqual(nested["external_active_lines"], (1 << 35) | (1 << 239))
        resumed = self.until(sim, lambda state: state["IPSR"] == 51)
        self.assertEqual(resumed["external_active_lines"], 1 << 35)
        second = self.until(sim, lambda state: state["IPSR"] == 108)
        self.assertEqual(second["EXCEPTION_DEPTH"], 1)
        final = self.until(sim, lambda state: state["SLEEPING"])
        self.assertEqual((final["RAM0"], final["RAM1"], final["RAM2"], final["MSP"]),
                         (1, 1, 1, 0x20000400))

    def test_primask_wakes_wfi_without_entering_high_irq(self):
        sim = self.build({239: 128}, mask=".hword 0xB672; // CPSID i")
        self.until(sim, lambda state: state["SLEEPING"])
        self.pulse(sim, 239)
        state = self.until(sim, lambda state: state["R4"] == 42)
        self.assertEqual((state["IPSR"], state["PRIMASK"], state["PENDING_EXCEPTION"]), (0, 1, 255))

    def test_held_high_line_repends_on_exception_return(self):
        sim = self.build({239: 128})
        self.until(sim, lambda state: state["SLEEPING"])
        sim.set_input("IRQ_LINES", 1 << 239)
        self.until(sim, lambda state: state["RAM0"] == 2)
        sim.set_input("IRQ_LINES", 0)
        returned = self.until(sim, lambda state: state["SLEEPING"])
        self.assertEqual((returned["RAM0"], returned["MSP"], returned["external_active_lines"]),
                         (2, 0x20000400, 0))

    def test_basepri_masks_the_group_and_allows_a_higher_priority_irq(self):
        mask = """
            MOVS r1, #144;
            .hword 0xF381, 0x8811; // MSR BASEPRI, r1
            MOVS r3, #8;
        delay:
            SUBS r3, #1;
            BNE delay;
            MOVS r1, #0;
            .hword 0xF381, 0x8811;
        """
        sim = self.build({35: 128, 239: 64}, group=5, mask=mask)
        self.until(sim, lambda state: state["BASEPRI"] == 144)
        self.pulse(sim, 35, 239)
        entered = self.until(sim, lambda state: state["IPSR"] == 255)
        self.assertEqual((entered["BASEPRI"], entered["RAM0"]), (144, 0))
        self.until(sim, lambda state: state["IPSR"] == 0 and state["RAM1"] == 1)
        unmasked = self.until(sim, lambda state: state["BASEPRI"] == 0)
        self.assertEqual(unmasked["RAM0"], 0)
        self.until(sim, lambda state: state["IPSR"] == 51)
        final = self.until(sim, lambda state: state["SLEEPING"])
        self.assertEqual((final["RAM0"], final["RAM1"], final["MSP"]), (1, 1, 0x20000400))

    def test_sevonpend_tracks_independent_disabled_high_irqs(self):
        sim = self.build({139: 128, 239: 128}, enabled=False, wait="WFE")
        self.until(sim, lambda state: state["SLEEPING"])
        self.pulse(sim, 139)
        self.until(sim, lambda state: state["R4"] == 42)
        asleep = self.until(sim, lambda state: state["SLEEPING"])
        retired = asleep["RETIRED"]
        self.assertEqual(asleep["external_pending_lines"], 1 << 139)
        self.pulse(sim, 239)
        # The second pending bit produces an event even with the first held.
        state = self.until(sim, lambda state: state["RETIRED"] >= retired + 2)
        self.assertEqual((state["IPSR"], state["external_pending_lines"]),
                         (0, (1 << 139) | (1 << 239)))

