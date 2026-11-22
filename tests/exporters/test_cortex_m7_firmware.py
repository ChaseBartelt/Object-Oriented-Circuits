"""Run inline and linked Cortex-M7 firmware through the OOC Thumb model."""

from pathlib import Path
import runpy
import unittest

from circuitlab.exporters.verilog import export_verilog
from circuitlab.math.digital import DigitalSimulation
from examples.advanced.CortexM7.firmware.build_firmware import read_model_source


FIRMWARE = (Path(__file__).resolve().parents[2] / "examples" / "advanced"
            / "CortexM7" / "CortexM7_Firmware.ckt")
RAM_BASE = 0x20000000
STACK_TOP = 0x20000400
RESULT = 13
SYSTICK_VECTOR = 0x3C
SYSTICK_RELOAD = 31
# Original linked image from startup.S, firmware.c, and arithmetic.c.
BOOT_IMAGE = (
    STACK_TOP, 9, 0xF802F000, 0xD4D4E7FE,
    0x2004B580, 0xF806F000, 0x60084901, 0xBF00BD80,
    RAM_BASE, 0xF000B580, 0x3002F802, 0x3007BD80,
    0xD4D44770,
)
# Linked from startup_systick.S and systick.c with build_firmware.py --systick.
# Keep the simulator checks independent of an installed ARM compiler.
SYSTICK_IMAGE = (
    0x20000400, 0x00000041, 0x00000000, 0x00000000,
    0x00000000, 0x00000000, 0x00000000, 0x00000000,
    0x00000000, 0x00000000, 0x00000000, 0x00000000,
    0x00000000, 0x00000000, 0x00000000, 0x00000071,
    0xF802F000, 0xD4D4E7FE, 0x21004805, 0x6001221F,
    0x60024804, 0x60014804, 0x21074804, 0x47706001,
    0x20000004, 0xE000E014, 0xE000E018, 0xE000E010,
    0x68014802, 0x60013101, 0xBF004770, 0x20000004,
)
# Linked from startup_large.S and large.c with build_firmware.py --large.
LARGE_IMAGE = bytes.fromhex("""
    00100020090000000a480b490b4a914204d003680b6004300431f8e70849094a
    0023914202d00b600431fae700f00cf8fee70000b00100000800002014000020
    140000201c000020f8b50c480c4c00260c4f006821680d18b6f5807f08d0b859
    00f016f861680544043601316160f3e705480649056060680860f8bd08000020
    14000020b0000000000000200400002080b500f007f802490968084480bd00bf
    1000002001490968401a70470c000020b2040000c1040000d4040000e2040000
    f50400000505000014050000220500002e0500003a0500003f05000047050000
    4c0500004d0500004f0500004a050000440500003f0500003705000029050000
    1d0500000d050000fe040000ed040000de040000cd040000ba040000aa040000
    9804000089040000790400006d04000061040000560400004c04000043040000
    3e0400003d0400003b0400003e040000450400004a0400005404000061040000
    6f0400007f04000090040000a2040000b3040000c4040000d4040000e7040000
    f80400000505000017050000230500002f050000390500004105000046050000
    4d050000500500004e0500004b05000078563412e803000003000000
""")


def thumb_bl(instruction):
    return (instruction >> 16) & 0xD000 == 0xD000 and instruction & 0xF800 == 0xF000


def bl_target(address, instruction):
    """Decode the two little-endian halfwords using the Thumb BL immediate layout."""
    first, second = instruction & 0xFFFF, instruction >> 16
    sign = (first >> 10) & 1
    i1 = 1 ^ sign ^ ((second >> 13) & 1)
    i2 = 1 ^ sign ^ ((second >> 11) & 1)
    immediate = (sign << 24) | (i1 << 23) | (i2 << 22) | ((first & 0x3FF) << 12) | ((second & 0x7FF) << 1)
    if sign:
        immediate -= 1 << 25
    return (address + 4 + immediate) & 0xFFFFFFFF


def retired_instructions(trace):
    samples = []
    for before, after in trace:
        samples.append(before)
        if after["RETIRED"] != before["RETIRED"]:
            yield samples, after
            samples = []


class CortexM7FirmwareTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = read_model_source(FIRMWARE)

    def step_to(self, sim, finish):
        transitions = []
        for _ in range(512):
            before = sim.values
            after = sim.step()
            transitions.append((before, after))
            if finish(after):
                return transitions
            if after["FAULT"]:
                break
        self.fail(f"Firmware did not reach the expected state after {len(transitions)} cycles. "
                  f"PC={sim.values['PC']:#x}, fault={sim.values['FAULT_CODE']}")

    def boot(self, sim, stack_top=STACK_TOP):
        for address in (0, 4):
            self.assertEqual((sim.values["BUS_ADDR"], sim.values["BUS_READ"], sim.values["BUS_SIZE"]),
                             (address, 1, 2))
            vector = sim.values["BUS_RDATA"]
            sim.step()
        self.assertEqual(vector & 1, 1)
        self.assertEqual((sim.values["BOOTED"], sim.values["FAULT"], sim.values["MSP"], sim.values["PC"]),
                         (1, 0, stack_top, vector & ~1))

    def finish_firmware(self, sim):
        return self.step_to(sim, lambda values: values["RAM0"] == RESULT
                            and values["LAST_IR"] == 0xE7FE and values["PC"] == values["LAST_PC"])

    def test_typed_image_boots_and_can_restart_after_the_result(self):
        self.assertTrue(export_verilog(self.source).startswith("module CORTEX_M7_FIRMWARE ("))
        sim = DigitalSimulation(self.source)
        self.assertEqual(sim.clocks, ("CLK",))
        self.assertEqual(sim.inputs, ("RESET", "IRQ0", "IRQ1", "NMI", "IRQ_LINES"))
        self.assertEqual(sim.signals["IRQ_LINES"], 240)
        for name, width in (("IR", 32), ("LAST_IR", 32), ("MSP", 32), ("LR", 32),
                            ("RETIRED", 32), ("BUS_SIZE", 2)):
            with self.subTest(port=name):
                self.assertEqual(sim.signals[name], width)
        self.boot(sim)
        self.finish_firmware(sim)

        sim.set_input("RESET", 1)
        sim.step()
        self.assertEqual((sim.values["BOOTED"], sim.values["FAULT"]), (0, 0))
        sim.set_input("RESET", 0)
        self.boot(sim)
        self.finish_firmware(sim)
        self.assertEqual(sim.values["RAM0"], RESULT)

    def test_compiled_nested_calls_save_and_restore_the_return_addresses(self):
        sim = DigitalSimulation(self.source)
        self.boot(sim)
        trace = self.finish_firmware(sim)
        retired = list(retired_instructions(trace))
        calls = [after for _, after in retired if thumb_bl(after["LAST_IR"])]
        self.assertGreaterEqual(len(calls), 3)
        for call in calls:
            with self.subTest(call_site=hex(call["LAST_PC"])):
                self.assertEqual(call["LR"], (call["LAST_PC"] + 4) | 1)
                self.assertEqual(call["PC"], bl_target(call["LAST_PC"], call["LAST_IR"]))

        returns = [(samples[-1], after) for samples, after in retired if after["LAST_IR"] == 0x4770]
        self.assertTrue(returns)
        for before, after in returns:
            self.assertEqual(after["PC"], before["LR"] & ~1)
        pushes = [(samples, after) for samples, after in retired if after["LAST_IR"] & 0xFE00 == 0xB400]
        pops = [(samples, after) for samples, after in retired if after["LAST_IR"] & 0xFE00 == 0xBC00]
        self.assertTrue(pushes)
        self.assertTrue(pops)
        stacked, popped = set(), set()
        for samples, after in pushes:
            count = (after["LAST_IR"] & 0x1FF).bit_count()
            start, end = after["MSP"], samples[0]["MSP"]
            transfers = [sample for sample in samples if sample["BUS_WRITE"]]
            self.assertEqual(end - start, 4 * count)
            self.assertEqual([(sample["BUS_ADDR"], sample["BUS_SIZE"]) for sample in transfers],
                             [(address, 2) for address in range(start, end, 4)])
            stacked.update((sample["BUS_ADDR"], sample["BUS_WDATA"]) for sample in transfers)
        for samples, after in pops:
            count = (after["LAST_IR"] & 0x1FF).bit_count()
            start, end = samples[0]["MSP"], after["MSP"]
            transfers = [sample for sample in samples if sample["BUS_READ"]]
            self.assertEqual(end - start, 4 * count)
            self.assertEqual([(sample["BUS_ADDR"], sample["BUS_SIZE"]) for sample in transfers],
                             [(address, 2) for address in range(start, end, 4)])
            popped.update((sample["BUS_ADDR"], sample["BUS_RDATA"]) for sample in transfers)
            if after["LAST_IR"] & 0x100:
                self.assertEqual(after["PC"], transfers[-1]["BUS_RDATA"] & ~1)
        self.assertTrue(stacked.issubset(popped))
        self.assertEqual(sim.values["MSP"], STACK_TOP)
        self.assertEqual(sim.values["RAM0"], RESULT)
        self.assertTrue(any(before["BUS_WRITE"] and before["BUS_ADDR"] == RAM_BASE
                            and before["BUS_SIZE"] == 2 and before["BUS_WDATA"] == RESULT for before, _ in trace))

    def rom_sections(self):
        start, begin, source = self.source.partition("// BEGIN LINKED FIRMWARE")
        rom, end, rest = source.partition("// END LINKED FIRMWARE")
        self.assertTrue(begin and end)
        return start + begin, rom, end + rest

    def test_inline_rom_matches_the_original_linked_image(self):
        _, rom, _ = self.rom_sections()
        sim = DigitalSimulation(rom)
        for index, word in enumerate(BOOT_IMAGE):
            with self.subTest(address=hex(index * 4)):
                sim.set_input("address", index * 4)
                self.assertEqual((sim.values["word"], sim.values["valid"]), (word, 1))
        for address in (2, 50, len(BOOT_IMAGE) * 4):
            with self.subTest(address=hex(address)):
                sim.set_input("address", address)
                self.assertEqual((sim.values["word"], sim.values["valid"]), (0, 0))

    def sim_with_boot_word(self, address, update):
        words = list(BOOT_IMAGE)
        index = address // 4
        words[index] = update(words[index])
        return self.sim_with_rom(words)

    def sim_with_boot_halfword(self, address, value):
        shift = 16 if address & 2 else 0
        return self.sim_with_boot_word(address, lambda word: (word & ~(0xFFFF << shift)) | (value << shift))

    def sim_with_rom(self, words):
        start, _, rest = self.rom_sections()
        cases = "\n".join(f"case {index * 4}: word = {word}; break;" for index, word in enumerate(words))
        rom = f"""
chip M7FirmwareRom(address, word, valid) {{
    address[32] = input;
    word[32] = output;
    valid = output;
    valid = address < {4 * len(words)} && address[1:0] == 0;
    switch (address) {{
        {cases}
        default: word = 0;
    }}
}}
"""
        return DigitalSimulation(start + rom + rest)

    def test_byte_and_halfword_access_selects_lanes_without_changing_other_bytes(self):
        sim = self.sim_with_rom([
            STACK_TOP, 9,
            0x20AA4904,  # LDR r1, RAM_BASE; MOVS r0, #0xAA
            0x784A7048,  # STRB r0, [r1, #1]; LDRB r2, [r1, #1]
            0x80484803,  # LDR r0, 0xFCBB literal; STRH r0, [r1, #2]
            0x680C884B,  # LDRH r3, [r1, #2]; LDR r4, [r1]
            0xBF00E7FE,  # B .; padding
            RAM_BASE, 0xFCBB,
        ])
        self.boot(sim)
        trace = self.step_to(sim, lambda values: values["LAST_IR"] == 0xE7FE)
        final = trace[-1][1]
        self.assertEqual((final["RAM0"], final["RAM1"]), (0xFCBBAA00, 0))
        self.assertEqual((final["R2"], final["R3"], final["R4"]), (0xAA, 0xFCBB, 0xFCBBAA00))
        accesses = [(before["BUS_ADDR"], before["BUS_SIZE"], before["BUS_WRITE"]) for before, _ in trace
                    if (before["BUS_READ"] or before["BUS_WRITE"]) and before["BUS_ADDR"] >= RAM_BASE]
        self.assertEqual(accesses, [
            (RAM_BASE + 1, 0, 1), (RAM_BASE + 1, 0, 0),
            (RAM_BASE + 2, 1, 1), (RAM_BASE + 2, 1, 0), (RAM_BASE, 2, 0),
        ])

    def test_wide_fetch_faults_if_the_second_halfword_is_outside_flash(self):
        # The first BL halfword is at address 10, the last mapped halfword.
        sim = self.sim_with_rom([STACK_TOP, 11, 0xF000BF00])
        self.boot(sim)
        self.assertEqual((sim.values["PC"], sim.values["WIDE"]), (10, 1))
        final = self.step_to(sim, lambda values: values["FAULT"])[-1][1]
        self.assertEqual((final["FAULT_CODE"], final["RETIRED"], final["PC"], final["LR"]),
                         (2, 0, 10, 0xFFFFFFFF))

    def test_invalid_bl_second_halfword_never_retires_or_changes_lr(self):
        original = DigitalSimulation(self.source)
        self.boot(original)
        entry = original.values["PC"]
        self.assertTrue(thumb_bl(original.values["IR"]))
        # Use an unsupported wide branch encoding. A zero second halfword
        # would make this a valid AND modified-immediate instruction.
        sim = self.sim_with_boot_halfword(entry + 2, 0xC000)
        self.boot(sim)
        final = self.step_to(sim, lambda values: values["FAULT"])[-1][1]
        self.assertEqual((final["FAULT_CODE"], final["RETIRED"], final["PC"], final["LR"]),
                         (3, 0, entry, 0xFFFFFFFF))

    def test_bx_to_an_even_target_faults_before_retirement(self):
        original = DigitalSimulation(self.source)
        self.boot(original)
        entry = original.values["PC"]
        sim = self.sim_with_boot_halfword(entry, 0x4700)  # BX R0, initially zero
        self.boot(sim)
        final = self.step_to(sim, lambda values: values["FAULT"])[-1][1]
        self.assertEqual((final["FAULT_CODE"], final["RETIRED"], final["PC"], final["MSP"]),
                         (5, 0, entry, STACK_TOP))

    def test_push_below_sram_faults_without_moving_sp_or_writing_ram(self):
        sim = self.sim_with_boot_word(0, lambda _: RAM_BASE)
        self.boot(sim, stack_top=RAM_BASE)
        final = self.step_to(sim, lambda values: values["FAULT"])[-1][1]
        self.assertEqual((final["FAULT_CODE"], final["MSP"], final["RAM0"], final["RAM1"]),
                         (4, RAM_BASE, 0, 0))

    def test_systick_c_handler_stacks_and_restores_thread_state_twice(self):
        for initial_msp in (STACK_TOP, STACK_TOP - 4):
            with self.subTest(initial_msp=hex(initial_msp)):
                self.check_systick_frames(initial_msp)

    def check_systick_frames(self, initial_msp):
        words = (initial_msp, *SYSTICK_IMAGE[1:])
        vector = words[SYSTICK_VECTOR // 4]
        idle = (words[1] & ~1) + 4
        sim = self.sim_with_rom(words)
        self.boot(sim, stack_top=initial_msp)
        frame_base = (initial_msp & ~7) - 32
        addresses = range(frame_base, frame_base + 32, 4)
        saved_names = ("R0", "R1", "R2", "R3", "R12", "LR", "PC")
        restored_names = (*saved_names, "MSP", "NZCV")
        last_interrupt = None
        for hit in (1, 2):
            entering = self.step_to(sim, lambda values: values["IRQ_ACTIVE"])
            self.assertTrue(any(before["IRQ_PENDING"] for before, _ in entering))
            if last_interrupt is not None:
                self.assertEqual(sim.cycles - last_interrupt, SYSTICK_RELOAD + 1)
            last_interrupt = sim.cycles
            active = entering[-1][1]
            self.assertEqual((active["PC"], active["MSP"], active["LR"], active["RAM1"]),
                             (vector & ~1, frame_base, 0xFFFFFFF9, hit - 1))

            writes = [before for before, _ in entering
                      if before["BUS_WRITE"] and before["BUS_ADDR"] in addresses]
            self.assertEqual(len(writes), 8)
            saved = writes[0]
            self.assertEqual(saved["PC"], idle)
            status = (saved["NZCV"] << 28) | (1 << 24) | (int(bool(initial_msp & 4)) << 9)
            expected = [saved[name] for name in saved_names] + [status]
            self.assertEqual(
                [(sample["BUS_ADDR"], sample["BUS_SIZE"], sample["BUS_WDATA"]) for sample in writes],
                [(address, 2, word) for address, word in zip(addresses, expected)],
            )
            self.assertEqual(self.systick_vector_reads(entering), [(2, vector)])

            returning = self.step_to(sim, lambda values: not values["IRQ_ACTIVE"])
            self.assertTrue(any(after["R0"] != saved["R0"] for _, after in returning))
            self.assertEqual(
                [(before["BUS_ADDR"], before["BUS_SIZE"], before["BUS_RDATA"]) for before, _ in returning
                 if before["BUS_READ"] and before["BUS_ADDR"] in addresses],
                [(address, 2, word) for address, word in zip(addresses, expected)],
            )
            resumed = returning[-1][1]
            self.assertEqual([resumed[name] for name in restored_names],
                             [saved[name] for name in restored_names])
            self.assertEqual((resumed["RAM1"], resumed["FAULT_CODE"]), (hit, 0))

    @staticmethod
    def systick_vector_reads(trace):
        return [(before["BUS_SIZE"], before["BUS_RDATA"]) for before, _ in trace
                if before["BUS_READ"] and before["BUS_ADDR"] == SYSTICK_VECTOR]

    def test_systick_even_vector_faults_before_the_handler_runs(self):
        words = list(SYSTICK_IMAGE)
        words[SYSTICK_VECTOR // 4] &= ~1
        sim = self.sim_with_rom(words)
        self.boot(sim)
        trace = self.step_to(sim, lambda values: values["FAULT"])
        self.assertEqual(self.systick_vector_reads(trace), [(2, words[SYSTICK_VECTOR // 4])])
        final = trace[-1][1]
        self.assertEqual((final["FAULT_CODE"], final["IRQ_ACTIVE"], final["RAM1"]), (6, 0, 0))

    def test_larger_linked_c_initializes_sram_and_processes_all_samples(self):
        build = runpy.run_path(str(FIRMWARE.parent / "firmware" / "build_firmware.py"))
        start, _, rest = self.rom_sections()
        sim = DigitalSimulation(start + "\n" + build["rom_body"](LARGE_IMAGE, large=True) + rest)
        stack_top = RAM_BASE + 4096
        self.boot(sim, stack_top=stack_top)

        startup = self.step_to(sim, lambda values: thumb_bl(values["LAST_IR"]))
        initializers = (0x12345678, 1000, 3)
        flash = [(before["BUS_ADDR"], before["BUS_SIZE"], before["BUS_RDATA"]) for before, _ in startup
                 if before["BUS_READ"] and 0x1B0 <= before["BUS_ADDR"] < 0x1BC]
        self.assertEqual(flash, [(0x1B0 + i * 4, 2, word) for i, word in enumerate(initializers)])
        stores = [(before["BUS_ADDR"], before["BUS_SIZE"], before["BUS_WDATA"]) for before, _ in startup
                  if before["BUS_WRITE"]]
        self.assertEqual(stores, [(RAM_BASE + 8 + i * 4, 2, word)
                                  for i, word in enumerate((*initializers, 0, 0))])

        sim.advance(2400)
        values = sim.values
        self.assertEqual(values["FAULT_CODE"], 0)
        self.assertEqual((values["RAM0"], values["RAM1"], values["MSP"]), (0x123493EB, 64, stack_top))
        self.assertEqual((values["PC"], values["LAST_PC"], values["LAST_IR"]), (0x30, 0x30, 0xE7FE))

    def sim_with_systick_startup(self, code, *, handler=None):
        entry = len(SYSTICK_IMAGE) * 4
        words = [SYSTICK_IMAGE[0], entry | 1, *SYSTICK_IMAGE[2:], *code]
        if handler is not None:
            words[SYSTICK_VECTOR // 4] = handler | 1
        return self.sim_with_rom(words)

    def test_primask_latches_systick_until_cpsie_releases_it(self):
        sim = self.sim_with_systick_startup([
            0xF7FFB672, 0x2663FFE1,  # CPSID i; BL 0x48; MOVS r6, #99
            0x3C012428, 0xF3EFD1FD,  # MOVS r4, #40; SUBS r4, #1; BNE 0x8A; MRS r5, PRIMASK
            0xB6628510, 0x8610F3EF,  # finish MRS; CPSIE i; MRS r6, PRIMASK
            0xBF00E7FE,              # B .
        ])
        self.boot(sim)
        trace = self.step_to(sim, lambda values: values["IRQ_ACTIVE"])
        waiting = [before for before, _ in trace if before["IRQ_PENDING"] and before["PRIMASK"]]
        self.assertGreaterEqual(len(waiting), SYSTICK_RELOAD + 1)
        self.assertTrue(all(before["RAM1"] == 0 and not before["IRQ_ACTIVE"] for before in waiting))
        self.assertFalse(any(before["BUS_WRITE"] and STACK_TOP - 32 <= before["BUS_ADDR"] < STACK_TOP
                             for before in waiting))
        active = trace[-1][1]
        self.assertEqual((active["PRIMASK"], active["R5"], active["R6"]), (0, 1, 99))
        self.assertEqual(self.systick_vector_reads(trace), [(2, SYSTICK_IMAGE[SYSTICK_VECTOR // 4])])

        returned = self.step_to(sim, lambda values: not values["IRQ_ACTIVE"])[-1][1]
        self.assertEqual((returned["RAM1"], returned["PC"]), (1, 0x94))
        observed = self.step_to(sim, lambda values: values["LAST_PC"] == 0x94)[-1][1]
        self.assertEqual((observed["R5"], observed["R6"]), (1, 0))

    def test_shpr3_configures_systick_priority_and_basepri_masks_equal_priority(self):
        priority_address = 0xE000ED20
        sim = self.sim_with_systick_startup([
            0x490A4809, 0x68056001,              # Load literals; STR/LDR SHPR3 word
            0x70C12183, 0x248078C6,              # STRB/LDRB SysTick priority byte
            0x8811F384, 0xFFD8F7FF,              # MSR BASEPRI, #0x80 via r4; BL 0x48
            0x3C012428, 0x2490D1FD,              # Wait 40 iterations; MOVS r4, #0x90
            0x8811F384, 0xBF00E7FE,              # MSR BASEPRI, r4; B .
            priority_address, 0x9F000000,
        ])
        self.boot(sim)
        trace = self.step_to(sim, lambda values: values["IRQ_ACTIVE"])
        accesses = [
            (before["BUS_ADDR"], before["BUS_SIZE"],
             before["BUS_WDATA"] if before["BUS_WRITE"] else before["BUS_RDATA"])
            for before, _ in trace
            if (before["BUS_READ"] or before["BUS_WRITE"])
            and before["BUS_ADDR"] in (priority_address, priority_address + 3)
        ]
        self.assertEqual(accesses, [
            (priority_address, 2, 0x9F000000), (priority_address, 2, 0x90000000),
            (priority_address + 3, 0, 0x83), (priority_address + 3, 0, 0x80),
        ])
        waiting = [before for before, _ in trace if before["IRQ_PENDING"] and before["BASEPRI"] == 0x80]
        self.assertGreaterEqual(len(waiting), SYSTICK_RELOAD + 1)
        self.assertTrue(all(before["SYSTICK_PRIORITY"] == 0x80 and before["RAM1"] == 0
                            and not before["IRQ_ACTIVE"] and not before["BUS_WRITE"] for before in waiting))
        active = trace[-1][1]
        self.assertEqual((active["BASEPRI"], active["SYSTICK_PRIORITY"]), (0x90, 0x80))
        self.assertEqual((active["R5"], active["R6"]), (0x90000000, 0x80))
        returned = self.step_to(sim, lambda values: not values["IRQ_ACTIVE"])[-1][1]
        self.assertEqual((returned["RAM1"], returned["PC"], returned["BASEPRI"]), (1, 0xA4, 0x90))

    def test_systick_stacks_and_returns_using_thread_psp(self):
        thread_sp = RAM_BASE + 0x300
        sim = self.sim_with_systick_startup([
            0xF3804804, 0x20028809,  # LDR r0, literal; MSR PSP, r0; MOVS r0, #2
            0x8814F380, 0xFFDCF7FF,  # MSR CONTROL, r0; BL 0x48
            0xBF00E7FE, thread_sp,   # B .; the PSP literal
        ])
        self.boot(sim)
        entering = self.step_to(sim, lambda values: values["IRQ_ACTIVE"])
        active = entering[-1][1]
        frame_base = thread_sp - 32
        self.assertEqual((active["MSP"], active["PSP"], active["SP"], active["LR"]),
                         (STACK_TOP, frame_base, STACK_TOP, 0xFFFFFFFD))
        self.assertEqual((active["CONTROL"], active["IPSR"], active["PRIVILEGED"]), (0, 15, 1))
        stacked = [(before["BUS_ADDR"], before["BUS_WDATA"]) for before, _ in entering
                   if before["BUS_WRITE"] and RAM_BASE <= before["BUS_ADDR"] < STACK_TOP
                   and before["BUS_ADDR"] != RAM_BASE + 4]
        self.assertEqual([address for address, _ in stacked], list(range(frame_base, thread_sp, 4)))
        self.assertEqual(stacked[6][1], 0x90)

        returning = self.step_to(sim, lambda values: not values["IRQ_ACTIVE"])
        restored = [(before["BUS_ADDR"], before["BUS_RDATA"]) for before, _ in returning
                    if before["BUS_READ"] and frame_base <= before["BUS_ADDR"] < thread_sp]
        self.assertEqual(restored, stacked)
        resumed = returning[-1][1]
        self.assertEqual((resumed["MSP"], resumed["PSP"], resumed["SP"], resumed["PC"]),
                         (STACK_TOP, thread_sp, thread_sp, 0x90))
        self.assertEqual((resumed["CONTROL"], resumed["IPSR"], resumed["RAM1"]), (2, 0, 1))

    def test_system_register_widths_priority_updates_and_unprivileged_access(self):
        instructions = [
            0x20FF, 0xF380, 0x8810, 0xF3EF, 0x8110,  # MSR/MRS PRIMASK
            0x2083, 0xF380, 0x8811, 0xF3EF, 0x8211,  # MSR/MRS BASEPRI
            0x2043, 0xF380, 0x8812,                  # BASEPRI_MAX strengthens the mask
            0x20C0, 0xF380, 0x8812,                  # A weaker priority has no effect
            0x2000, 0xF380, 0x8812,                  # Zero also has no effect
            0xF3EF, 0x8311,                          # MRS r3, BASEPRI
            0x2001, 0xF380, 0x8813, 0xF3EF, 0x8413,  # MSR/MRS FAULTMASK
            0xB661, 0xF3EF, 0x8513,                  # CPSIE f; MRS r5, FAULTMASK
            0x2663, 0xF3EF, 0x8605,                  # MRS r6, IPSR in Thread mode
            0xF3EF, 0x8708,                          # MRS r7, MSP
            0x2001, 0xF380, 0x8814,                  # Drop privilege through CONTROL
            0xB662, 0x2000, 0xF380, 0x8811,          # Unprivileged CPSIE i and MSR BASEPRI
            0x2063, 0xF3EF, 0x8010, 0xE7FE, 0xBF00,  # Unprivileged MRS r0, PRIMASK; B .
        ]
        words = [low | (high << 16) for low, high in zip(instructions[::2], instructions[1::2], strict=True)]
        sim = self.sim_with_rom([STACK_TOP, 9, *words])
        self.boot(sim)
        final = self.step_to(sim, lambda values: values["LAST_IR"] == 0xE7FE)[-1][1]
        self.assertEqual([final[f"R{i}"] for i in range(8)], [0, 1, 0x80, 0x40, 1, 0, 0, STACK_TOP])
        self.assertEqual((final["PRIMASK"], final["BASEPRI"], final["FAULTMASK"]), (1, 0x40, 0))
        self.assertEqual((final["CONTROL"], final["PRIVILEGED"], final["IPSR"]), (1, 0, 0))

    def test_qadd_and_qsub_saturate_and_keep_q_sticky(self):
        signed_max, signed_min = 0x7FFFFFFF, 0x80000000
        nzcv, q = 0x50000000, 1 << 27
        instructions = [
            0x2101,                  # MOVS r1, #1
            0x4810,                  # LDR r0, signed_max
            0x4C11,                  # LDR r4, nzcv
            0xF384, 0x8800,          # MSR APSR_nzcvq, r4
            0xFA81, 0xF280,          # QADD r2, r0, r1
            0xF3EF, 0x8300,          # MRS r3, APSR
            0xFA81, 0xF4A1,          # QSUB r4, r1, r1
            0xF3EF, 0x8500,          # MRS r5, APSR
            0x480B,                  # LDR r0, signed_min
            0xFA81, 0xF6A0,          # QSUB r6, r0, r1
            0xFA80, 0xF780,          # QADD r7, r0, r0
            0xF3EF, 0x8800,          # MRS r8, APSR
            0xF240, 0x092A,          # MOVW r9, #42
            0xF389, 0x8800,          # MSR APSR_nzcvq, r9
            0xBF08,                  # IT EQ
            0xFA80, 0xF980,          # QADDEQ r9, r0, r0
            0xF3EF, 0x8A00,          # MRS r10, APSR
            0xFA80, 0xFBA1,          # QSUB r11, r1, r0
            0xF3EF, 0x8C00,          # MRS r12, APSR
            0xE7FE,                  # B .
        ]
        words = [low | (high << 16) for low, high in zip(instructions[::2], instructions[1::2], strict=True)]
        sim = self.sim_with_rom([STACK_TOP, 9, *words, signed_max, signed_min, nzcv])
        self.boot(sim)
        final = self.step_to(sim, lambda values: values["LAST_IR"] == 0xE7FE)[-1][1]
        self.assertEqual([final[f"R{i}"] for i in (2, 4, 6, 7, 11)],
                         [signed_max, 0, signed_min, signed_min, signed_max])
        self.assertEqual([final[f"R{i}"] for i in (3, 5, 8)], [nzcv | q] * 3)
        self.assertEqual((final["R9"], final["R10"], final["R12"], final["NZCV"]), (42, 0, q, 0))

    def test_systick_preserves_combined_apsr_fields_across_exception_return(self):
        thread_apsr = 0xB80A0000
        sim = self.sim_with_systick_startup([
            0xFFE2F7FF,                       # BL 0x48 to configure SysTick
            0xF3804802, 0xF3EF8C00,            # Load literal; MSR APSR_nzcvqg, r0
            0xE7FC8600, thread_apsr,           # MRS r6, APSR; B 0x8A; literal
            0x23632000, 0x8C00F380,            # Handler shim: zero APSR, set r3 sentinel
            0x8300F3EF, 0xBF00E7E6,            # MRS r3, APSR; B 0x70 to the C handler
        ], handler=0x94)
        self.boot(sim)
        entering = self.step_to(sim, lambda values: values["IRQ_ACTIVE"])
        statuses = [before["BUS_WDATA"] for before, _ in entering
                    if before["BUS_WRITE"] and before["BUS_ADDR"] == STACK_TOP - 4]
        self.assertEqual(statuses, [thread_apsr | (1 << 24)])
        active = entering[-1][1]
        self.assertEqual((active["PC"], active["R6"], active["NZCV"]), (0x94, thread_apsr, 0xB))

        returning = self.step_to(sim, lambda values: not values["IRQ_ACTIVE"])
        cleared = [after["R3"] for before, after in returning
                   if before["RETIRED"] != after["RETIRED"] and after["LAST_PC"] == 0x9C]
        self.assertEqual(cleared, [0])
        resumed = returning[-1][1]
        self.assertEqual((resumed["NZCV"], resumed["RAM1"]), (0xB, 1))
        observed = self.step_to(sim, lambda values: values["LAST_PC"] == 0x8A)[-1][1]
        self.assertEqual(observed["R6"], thread_apsr)
