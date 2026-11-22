"""Run CMSIS and FreeRTOS through fault recovery, NMI, and grouped IRQs."""

from collections import Counter
from struct import unpack_from
import unittest

from tests.exporters.test_cortex_m7_freertos_blocking import RAM_BASE, ROOT, firmware_simulation


IMAGE = ROOT / "tests" / "fixtures" / "digital" / "cmsis_5_9_0_freertos_v11_1_0_arm_cm7.bin"
NMI, MEMMANAGE, BUSFAULT, USAGEFAULT, SVC, PENDSV, SYSTICK, IRQ0, IRQ1 = 2, 4, 5, 6, 11, 14, 15, 16, 17
# Faulting PCs come from the reproducible link map. All three instructions are 16-bit.
FAULT_CASES = {
    USAGEFAULT: (0x460, 0xDE2A, 0x10000, "USAGEFAULT_ACTIVE"),
    MEMMANAGE: (0x464, 0x6800, 0x82, "MEMMANAGE_ACTIVE"),
    BUSFAULT: (0x468, 0x6800, 0x8200, "BUSFAULT_ACTIVE"),
}
FAULT_ADDRESSES = {
    MEMMANAGE: ("MMFAR", 0x20000040),
    BUSFAULT: ("BFAR", 0x40000000),
}


class CortexM7CmsisTest(unittest.TestCase):
    def test_cmsis_tasks_recover_from_faults_and_resume_after_grouped_interrupts(self):
        image = IMAGE.read_bytes()
        vectors = unpack_from("<26I", image)
        self.assertEqual(vectors[:2], (0x20001000, 0x3C1))
        self.assertEqual(image[104:960], bytes(856))
        for pc, opcode, _, _ in FAULT_CASES.values():
            self.assertEqual(unpack_from("<H", image, pc)[0], opcode)
        self.assertEqual(len({vectors[exception] for exception in FAULT_CASES}), 3)
        self.assertEqual(len({vectors[NMI], vectors[IRQ0], vectors[IRQ1]}), 3)
        sim = firmware_simulation(image)
        sim.step()
        boot = sim.step()
        self.assertEqual((boot["MSP"], boot["PC"]), (vectors[0], vectors[1] & ~1))

        def step():
            before = sim.values
            after = sim.step()
            self.assertEqual(after["FAULT_CODE"], 0, f"PC={after['PC']:#x} IR={after['IR']:#x}")
            self.assertIn(after["RAM2"], (0, 1, 2, 3, 4))
            self.assertNotEqual(after["IPSR"], 3, f"Unexpected HardFault after PC={before['PC']:#x}")
            return before, after

        def until(predicate, limit=1600):
            for _ in range(limit):
                before, after = step()
                if predicate(after):
                    return before, after
            self.fail(f"Timed out at cycle {sim.cycles}, PC={after['PC']:#x}, "
                      f"IPSR={after['IPSR']}, RAM2={after['RAM2']}, "
                      f"IRQ counts={(after['RAM3'], after['RAM4'])}")

        def pulse(*inputs):
            for name in inputs:
                sim.set_input(name, 1)
            step()
            for name in inputs:
                sim.set_input(name, 0)

        def check_entry(before, after, exception, interrupted, depth):
            self.assertEqual((before["IPSR"], after["IPSR"], after["EXCEPTION_DEPTH"]),
                             (interrupted, exception, depth))
            self.assertEqual(after["PC"], vectors[exception] & ~1)
            self.assertEqual((before["BUS_READ"], before["BUS_ADDR"], before["BUS_RDATA"]),
                             (1, exception * 4, vectors[exception]))

        def enter_irq(exception, interrupted=0, depth=1):
            before, after = until(lambda state: state["IPSR"] in (NMI, IRQ0, IRQ1)
                                  and state["IPSR"] != interrupted)
            check_entry(before, after, exception, interrupted, depth)
            return after

        def return_to(exception, restored, depth):
            before, after = until(lambda state: state["IPSR"] != exception)
            self.assertEqual((before["IPSR"], after["IPSR"], after["EXCEPTION_DEPTH"]),
                             (exception, restored, depth))
            return after

        def thread_ready(state):
            return state["IPSR"] == 0 and state["PENDING_EXCEPTION"] == 0

        entries = Counter()
        dispatched_stacks = set()
        result_addresses = set(range(RAM_BASE, RAM_BASE + 44, 4))
        zeroed_results = set()
        fault_at = {case[0]: exception for exception, case in FAULT_CASES.items()}
        fault_stacks = {}
        returned_faults = []
        post_fault_counters = None
        boot_checked_data = False
        for _ in range(10000):
            before, after = step()
            if (not boot_checked_data and before["BUS_WRITE"] and before["BUS_WDATA"] == 0
                    and before["BUS_ADDR"] in result_addresses):
                zeroed_results.add(before["BUS_ADDR"])
            boot_checked_data |= after["RAM2"] == 1
            previous, active = before["IPSR"], after["IPSR"]

            fault = fault_at.get(before["PC"])
            if not previous and fault is not None and fault not in fault_stacks:
                self.assertEqual(before["SP"], before["PSP"])
                self.assertGreater(before["RAM0"], 0)
                self.assertGreater(before["RAM1"], 0)
                fault_stacks[fault] = before["PSP"]
                if fault in FAULT_ADDRESSES:
                    _, address = FAULT_ADDRESSES[fault]
                    self.assertEqual((before["BUS_READ"], before["BUS_ADDR"]), (1, address))
                    self.assertEqual(before["DATA_ALLOWED"], int(fault == BUSFAULT))
            if active and active != previous and after["PC"] == (vectors[active] & ~1):
                self.assertEqual((before["BUS_READ"], before["BUS_ADDR"], before["BUS_RDATA"]),
                                 (1, active * 4, vectors[active]))
                entries[active] += 1
                if active in FAULT_CASES:
                    _, _, status, active_signal = FAULT_CASES[active]
                    self.assertEqual((after[active_signal], after["CFSR"], after["HFSR"]), (1, status, 0))
                    self.assertEqual(after["PSP"], fault_stacks[active] - 32)
                    self.assertEqual(after["SP"], after["MSP"])
                    if active in FAULT_ADDRESSES:
                        address_register, address = FAULT_ADDRESSES[active]
                        self.assertEqual(after[address_register], address)

            if previous in FAULT_CASES and not active:
                pc, _, _, active_signal = FAULT_CASES[previous]
                self.assertEqual((after["PC"], after["PSP"], after["CFSR"], after["HFSR"], after[active_signal]),
                                 (pc + 2, fault_stacks[previous], 0, 0, 0))
                returned_faults.append(previous)
                if len(returned_faults) == len(FAULT_CASES):
                    post_fault_counters = after["RAM0"], after["RAM1"]
            if previous in (SVC, PENDSV) and not active:
                dispatched_stacks.add(after["PSP"])
            if (thread_ready(after) and after["RAM2"] == 2 and post_fault_counters
                    and after["RAM0"] > post_fault_counters[0] and after["RAM1"] > post_fault_counters[1]):
                break
        else:
            self.fail(f"Firmware did not finish the fault checks. Cycle {sim.cycles}, "
                      f"PC={after['PC']:#x}, IPSR={after['IPSR']}, RAM2={after['RAM2']}")

        self.assertTrue(boot_checked_data)
        self.assertEqual(zeroed_results, result_addresses)
        self.assertEqual(returned_faults, [USAGEFAULT, MEMMANAGE, BUSFAULT])
        for exception in (*FAULT_CASES, SVC):
            self.assertEqual(entries[exception], 1)
        self.assertGreater(entries[PENDSV], 0)
        self.assertGreater(entries[SYSTICK], 0)
        self.assertEqual(len(dispatched_stacks), 2)
        self.assertEqual((after["RAM5"], after["RAM6"], after["RAM7"], after["RAM8"], after["RAM9"]),
                         (0x10000, 0x82, 0x20000040, 0x8200, 0x40000000))

        # IRQ1's group wins over IRQ0's better subpriority. NMI preempts both.
        self.assertEqual((after["IRQ0_ENABLED"], after["IRQ1_ENABLED"], after["PRIGROUP"]), (1, 1, 5))
        self.assertEqual((after["IRQ0_PRIORITY"], after["IRQ1_PRIORITY"]), (0xC0, 0xA0))
        pulse("IRQ0")
        enter_irq(IRQ0)
        pulse("IRQ1")
        irq = enter_irq(IRQ1, IRQ0, 2)
        pulse("NMI")
        nmi = enter_irq(NMI, IRQ1, 3)
        self.assertEqual((nmi["NMI_ACTIVE"], nmi["IRQ0_ACTIVE"], nmi["IRQ1_ACTIVE"]), (1, 1, 1))
        resumed_irq = return_to(NMI, IRQ1, 2)
        self.assertEqual((resumed_irq["NMI_ACTIVE"], resumed_irq["MSP"], resumed_irq["RAM10"]),
                         (0, irq["MSP"], 1))
        return_to(IRQ1, IRQ0, 1)
        after = return_to(IRQ0, 0, 0)
        self.assertEqual((after["RAM3"], after["RAM4"]), (1, 1))

        # CMSIS moves both IRQs into group 2. The better subpriority is
        # pending through IRQ0 and runs after IRQ0 returns.
        _, grouped = until(lambda state: thread_ready(state) and state["RAM2"] == 3)
        self.assertEqual((grouped["PRIGROUP"], grouped["IRQ0_PRIORITY"], grouped["IRQ1_PRIORITY"]),
                         (5, 0xA0, 0x80))
        pulse("IRQ0")
        enter_irq(IRQ0)
        pulse("IRQ1")
        deferred = return_to(IRQ0, 0, 0)
        self.assertEqual((deferred["IRQ1_PENDING"], deferred["RAM3"], deferred["RAM4"]), (1, 2, 1))
        enter_irq(IRQ1)
        return_to(IRQ1, 0, 0)

        # Under simultaneous requests, IRQ1's subpriority beats IRQ0's
        # lower exception number.
        until(thread_ready)
        pulse("IRQ0", "IRQ1")
        first = enter_irq(IRQ1)
        self.assertEqual((first["IRQ0_PENDING"], first["IRQ0_ACTIVE"]), (1, 0))
        return_to(IRQ1, 0, 0)
        enter_irq(IRQ0)
        resumed = return_to(IRQ0, 0, 0)
        self.assertEqual((resumed["RAM3"], resumed["RAM4"], resumed["RAM10"]), (3, 3, 1))
        _, final = until(lambda state: state["RAM2"] == 4
                         and state["RAM0"] > resumed["RAM0"] and state["RAM1"] > resumed["RAM1"])
        self.assertEqual((final["PRIGROUP"], final["CFSR"], final["HFSR"]), (0, 0, 0))
