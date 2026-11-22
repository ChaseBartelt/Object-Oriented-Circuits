"""Exercise Cortex-M7 hints and the modeled wakeup paths."""

from struct import pack
import unittest

from tests.exporters.test_cortex_m7_freertos_blocking import MSP_TOP, firmware_simulation, thumb_image


WFI_PC = 0x54
AFTER_WFI = 0x58
INTERRUPT_IDLE = 0x5A
IRQ_HANDLER = 0x5C
AFTER_WFE = 0x64
EVENT_IDLE = 0x66


def interrupt_image(*, primask=False, sleep_on_exit=False):
    mask = 0xB672 if primask else 0xBF00  # CPSID I or NOP.
    wait = (0xBF30, 0xBF00) if primask else (0xF3AF, 0x8003)
    code = pack("<18H",
                0x4A06, 0x2100 | (2 if sleep_on_exit else 0), 0x6011, 0x6815,  # Configure and read SCR.
                0x4A05, 0x2101, 0x6011, mask,                                 # Enable IRQ0.
                *wait, 0x202A, 0xE7FE,                                       # WFI, MOVS R0,#42, B .
                0x244D, 0x4770,                                               # IRQ0: MOVS R4,#77, BX LR.
                0xED10, 0xE000, 0xE100, 0xE000)
    return thumb_image(code, unused=INTERRUPT_IDLE | 1, irq0=IRQ_HANDLER | 1)


def event_image(*, on_pending):
    code = pack("<20H",
                0x4A08, 0x2100 | (16 if on_pending else 0), 0x6011, 0x6815,  # Leave IRQ0 disabled.
                0xBF10, 0xF3AF, 0x8001,                                    # YIELD and YIELD.W.
                0x4289, 0xBF18, 0xBF30,                                    # CMP R1,R1; IT NE; WFINE.
                0xF3AF, 0x8004, 0xBF20,                                    # SEV.W; WFE consumes the event.
                0x2301, 0xF3AF, 0x8002,                                    # MOVS R3,#1; WFE.W sleeps.
                0x202A, 0xE7FE, 0xED10, 0xE000)
    return thumb_image(code, unused=EVENT_IDLE | 1)


def preload_image():
    code = pack("<23H",
                0xF04F, 0x4020, 0x2103,  # R0 = unmapped address 0xA0000000; R1 = 3.
                0xF890, 0xF0FF,          # PLD positive immediate
                0xF810, 0xFC04,          # PLD negative immediate
                0xF810, 0xF021,          # PLD shifted register
                0xF89F, 0xF018,          # PLD literal
                0xF990, 0xF002,          # PLI positive immediate
                0xF910, 0xFC04,          # PLI negative immediate
                0xF910, 0xF011,          # PLI shifted register
                0xF99F, 0xF008,          # PLI literal
                0xF8B0, 0xF004,          # Unallocated Armv7-M halfword memory hint.
                0x232A, 0xE7FE)
    return thumb_image(code, unused=0x71)


class CortexM7HintTest(unittest.TestCase):
    def until(self, sim, predicate):
        for _ in range(80):
            state = sim.step()
            self.assertFalse(state["FAULT"], f"PC={state['PC']:#x}, IR={state['IR']:#x}")
            if predicate(state):
                return state
        self.fail(f"Firmware did not reach the expected state from PC={sim.values['PC']:#x}.")

    def assert_held(self, sim):
        before = sim.values
        for _ in range(5):
            after = sim.step()
            self.assertEqual((after["PC"], after["RETIRED"]), (before["PC"], before["RETIRED"]))
            self.assertEqual((after["SLEEPING"], after["BUS_READ"], after["BUS_WRITE"], after["FAULT"]),
                             (1, 0, 0, 0))

    @staticmethod
    def pulse_irq(sim):
        sim.set_input("IRQ0", 1)
        sim.step()
        sim.set_input("IRQ0", 0)

    def test_wfi_takes_an_interrupt_and_resumes_at_the_next_instruction(self):
        sim = firmware_simulation(interrupt_image())
        state = self.until(sim, lambda state: state["SLEEPING"])
        self.assertEqual((state["LAST_PC"], state["PC"], state["R0"]), (WFI_PC, AFTER_WFI, 0))
        self.assert_held(sim)
        self.pulse_irq(sim)
        state = self.until(sim, lambda state: state["IPSR"] == 16)
        self.assertEqual((state["PC"], state["MSP"], state["SLEEPING"]), (IRQ_HANDLER, MSP_TOP - 32, 0))
        state = self.until(sim, lambda state: state["PC"] == INTERRUPT_IDLE)
        self.assertEqual((state["R0"], state["R4"], state["IPSR"], state["MSP"]), (42, 77, 0, MSP_TOP))
        self.assertEqual(state["EVENT_LATCH"], 1)

    def test_primask_blocks_entry_but_can_wake_wfi(self):
        sim = firmware_simulation(interrupt_image(primask=True))
        state = self.until(sim, lambda state: state["SLEEPING"])
        self.assertEqual((state["LAST_PC"], state["PC"], state["PRIMASK"]), (WFI_PC, WFI_PC + 2, 1))
        self.assert_held(sim)
        self.pulse_irq(sim)
        state = self.until(sim, lambda state: state["PC"] == INTERRUPT_IDLE)
        self.assertEqual((state["R0"], state["R4"], state["IPSR"], state["MSP"]), (42, 0, 0, MSP_TOP))
        self.assertEqual((state["IRQ0_PENDING"], state["PRIMASK"], state["SLEEPING"]), (1, 1, 0))

    def test_wfe_consumes_sev_and_only_wakes_for_disabled_irq_with_sevonpend(self):
        for on_pending in (False, True):
            with self.subTest(sev_on_pend=on_pending):
                sim = firmware_simulation(event_image(on_pending=on_pending))
                state = self.until(sim, lambda state: state["LAST_PC"] == 0x56)
                self.assertEqual(state["SLEEPING"], 0)  # Neither YIELD nor skipped WFI sleeps.
                state = self.until(sim, lambda state: state["LAST_PC"] == 0x58)
                self.assertEqual(state["EVENT_LATCH"], 1)
                state = self.until(sim, lambda state: state["LAST_PC"] == 0x5C)
                self.assertEqual((state["EVENT_LATCH"], state["SLEEPING"]), (0, 0))
                state = self.until(sim, lambda state: state["SLEEPING"])
                self.assertEqual((state["PC"], state["R3"], state["R5"]), (AFTER_WFE, 1, 16 if on_pending else 0))
                self.assert_held(sim)
                self.pulse_irq(sim)
                if on_pending:
                    state = self.until(sim, lambda state: state["PC"] == EVENT_IDLE)
                    self.assertEqual((state["R0"], state["IPSR"], state["SLEEPING"]), (42, 0, 0))
                    self.assertEqual((state["IRQ0_PENDING"], state["IRQ0_ENABLED"], state["EVENT_LATCH"]), (1, 0, 0))
                else:
                    self.assert_held(sim)
                    self.assertEqual(sim.values["IRQ0_PENDING"], 1)
                    sim.reset()
                    self.assertEqual((sim.values["SLEEPING"], sim.values["EVENT_LATCH"]), (0, 0))

    def test_exception_return_can_sleep_on_exit(self):
        sim = firmware_simulation(interrupt_image(sleep_on_exit=True))
        self.until(sim, lambda state: state["SLEEPING"])
        self.pulse_irq(sim)
        self.until(sim, lambda state: state["IPSR"] == 16)
        state = self.until(sim, lambda state: state["IPSR"] == 0)
        self.assertEqual((state["PC"], state["R0"], state["R4"], state["R5"]), (AFTER_WFI, 0, 77, 2))
        self.assertEqual((state["MSP"], state["SLEEPING"], state["EVENT_LATCH"]), (MSP_TOP, 1, 1))
        self.assert_held(sim)

    def test_preload_hints_retire_without_bus_access(self):
        sim = firmware_simulation(preload_image())
        self.until(sim, lambda state: state["PC"] == 0x4A)
        for address in range(0x4A, 0x6E, 4):
            with self.subTest(address=address):
                before = sim.values
                self.assertEqual((before["PC"], before["BUS_READ"], before["BUS_WRITE"]), (address, 0, 0))
                after = sim.step()
                self.assertEqual((after["LAST_PC"], after["PC"], after["RETIRED"]),
                                 (address, address + 4, before["RETIRED"] + 1))
                self.assertEqual((after["FAULT"], after["SLEEPING"], after["EVENT_LATCH"]), (0, 0, 0))
        state = self.until(sim, lambda state: state["PC"] == 0x70)
        self.assertEqual(state["R3"], 42)
