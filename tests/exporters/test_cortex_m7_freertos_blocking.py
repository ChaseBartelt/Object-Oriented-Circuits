"""Exercise timed and interrupt-driven task wakeups in the linked FreeRTOS image."""

from collections import Counter
from pathlib import Path
import runpy
from struct import pack
import unittest

from circuitlab.math.digital import DigitalSimulation
from examples.advanced.CortexM7.firmware.build_firmware import read_model_source


ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "examples" / "advanced" / "CortexM7" / "CortexM7_Firmware.ckt"
IMAGE = ROOT / "tests" / "fixtures" / "digital" / "freertos_v11_1_0_arm_cm7_r0p1_blocking.bin"
RAM_BASE = 0x20000000
MSP_TOP = RAM_BASE + 0x1000
ICSR = 0xE000ED04
PENDSVSET = 1 << 28
HARDFAULT, PENDSV, SYSTICK, IRQ0 = 3, 14, 15, 16

# Addresses from the fixture's link map.
TASK_DELAY, SEND_FROM_ISR, QUEUE_RECEIVE = 0x3D8, 0xCF8, 0xD7C
IDLE_STACK = (0x20000058, 0x20000158)
DELAY_STACK = (0x200001A0, 0x200002A0)
RECEIVE_STACK = (0x200002A0, 0x200003A0)


def on_stack(state, stack):
    return stack[0] <= state["PSP"] < stack[1]


def firmware_simulation(image):
    start, begin, remainder = read_model_source(FIRMWARE).partition("// BEGIN LINKED FIRMWARE")
    _, end, rest = remainder.partition("// END LINKED FIRMWARE")
    if not (begin and end):
        raise AssertionError("Missing linked firmware region.")
    builder = runpy.run_path(str(FIRMWARE.parent / "firmware" / "build_firmware.py"))
    rom = builder["rom_body"](image, rtos=True)
    return DigitalSimulation(start + begin + "\n" + rom + end + rest)


def thumb_image(code, *, unused, hardfault=0, systick=0, irq0=0):
    vectors = [0] * 17
    vectors[0:2] = [MSP_TOP, 0x45]
    vectors[HARDFAULT] = hardfault
    vectors[11] = vectors[PENDSV] = unused
    vectors[SYSTICK] = systick or unused
    vectors[IRQ0] = irq0
    return pack("<17I", *vectors) + code


def interrupt_image(priority):
    reset = pack("<14H",
                 0x480A, 0x2101, 0x6001,               # Enable IRQ0 in NVIC ISER0.
                 0x480A, 0x2100 | priority, 0x7001,    # Set IRQ0 priority.
                 0x4809, 0x2180, 0x7001,               # Set SysTick priority.
                 0x4809, 0x2104, 0x0609, 0x6001,       # Set ICSR.PENDSTSET.
                 0xE7FE)                              # Spin in Thread mode.
    systick = pack("<8H",
                   0x4A07, 0x2355, 0x6053, 0xBF00,    # RAM1 = 0x55 before the preemption point.
                   0x2356, 0x6053, 0x4770, 0xBF00)    # RAM1 = 0x56 before exception return.
    constants = pack("<5I", 0xE000E100, 0xE000E400, 0xE000ED23, ICSR, RAM_BASE)
    irq0 = pack("<8H2I",
                0x4A03, 0x2399, 0x6093,               # RAM2 = 0x99.
                0x4803, 0x6801, 0x60D1,               # RAM3 = ICSR.
                0x4770, 0xBF00, RAM_BASE, ICSR)
    return thumb_image(reset + systick + constants + irq0, unused=0x6D, systick=0x61, irq0=0x85)


def hardfault_image(address, instruction):
    reset = pack("<6H",
                 0x2000 | address, instruction,       # Set R0 and execute the faulting instruction.
                 0x4A08, 0x2355, 0x6013,               # RAM0 = 0x55 after recovery.
                 0xE7FE)
    handler = pack("<12H4I",
                   0x4807, 0x6801, 0x4A05, 0x6051,    # RAM1 = HFSR.
                   0x4806, 0x6801, 0x6091,            # RAM2 = CFSR.
                   0x9B06, 0x3302, 0x9306,            # Advance the stacked PC past the instruction.
                   0x4770, 0xBF00, 0, RAM_BASE, 0xE000ED2C, 0xE000ED28)
    return thumb_image(reset + handler, unused=0x65, hardfault=0x51)


class CortexM7FreeRtosBlockingTest(unittest.TestCase):
    def test_delay_and_queue_receiver_block_and_wake(self):
        image = IMAGE.read_bytes()
        sim = firmware_simulation(image)
        irq_vector = int.from_bytes(image[4 * IRQ0:4 * IRQ0 + 4], "little")
        calls = Counter()
        ticks = ticks_at_delay = 0
        requested = taken = yielded = resumed = received = 0
        idle_since_delay = pulse = False

        for _ in range(16000):
            before = sim.values
            after = sim.step()
            if pulse:
                sim.set_input("IRQ0", 0)
                pulse = False
            self.assertEqual((after["FAULT_CODE"], after["RAM4"]), (0, 0),
                             f"Cycle {sim.cycles}, PC {after['PC']:#x}, IR {after['IR']:#x}")

            previous, active = before["IPSR"], after["IPSR"]
            if active:
                self.assertEqual(after["SP"], after["MSP"])
            if previous != active and active == SYSTICK:
                ticks += 1
            if not active and on_stack(after, IDLE_STACK):
                idle_since_delay = True
            if after["RETIRED"] != before["RETIRED"]:
                retired = after["LAST_PC"]
                if retired in (TASK_DELAY, SEND_FROM_ISR, QUEUE_RECEIVE):
                    calls[retired, previous] += 1
                if retired == TASK_DELAY:
                    ticks_at_delay = ticks
                    idle_since_delay = False
            if after["RAM0"] != before["RAM0"] and after["RAM0"]:
                self.assertEqual(after["RAM0"], before["RAM0"] + 1)
                self.assertEqual(previous, 0)
                self.assertTrue(on_stack(before, DELAY_STACK))
                self.assertTrue(idle_since_delay)
                self.assertGreaterEqual(ticks - ticks_at_delay, 2)

            if previous != active and active == IRQ0:
                taken += 1
                self.assertEqual(taken, requested)
                self.assertEqual((before["BUS_READ"], before["BUS_ADDR"], before["BUS_RDATA"]),
                                 (1, before["VECTOR_BASE"] + 4 * IRQ0, irq_vector))
                self.assertEqual((after["PC"], after["IRQ0_ACTIVE"], after["IRQ0_PENDING"]),
                                 (irq_vector & ~1, 1, 0))
            if (previous == IRQ0 and before["BUS_WRITE"] and before["BUS_ADDR"] == ICSR
                    and before["BUS_WDATA"] & PENDSVSET):
                yielded += 1
                self.assertEqual(yielded, taken)
                self.assertEqual((active, after["PENDSV_PENDING"]), (IRQ0, 1))
            if previous == PENDSV and not active and on_stack(after, RECEIVE_STACK):
                resumed += 1
                self.assertEqual(resumed, yielded)
            if after["RAM1"] != before["RAM1"] and after["RAM1"]:
                received += 1
                self.assertEqual((after["RAM1"], after["RAM2"]), (1000 + received, received))
                self.assertEqual((previous, before["BASEPRI"], resumed), (0, 0, received))
                self.assertEqual((before["BUS_WRITE"], before["BUS_ADDR"]), (1, RAM_BASE + 4))
                self.assertTrue(on_stack(before, RECEIVE_STACK))

            # Wait until the receiver has entered its queue call and yielded to Idle.
            blocked = (not active and on_stack(after, IDLE_STACK) and after["RAM3"] == 2 * received + 1
                       and calls[QUEUE_RECEIVE, 0] == received + 1)
            if blocked and requested == received:
                if received == 2:
                    break
                if after["RAM0"] >= requested + 1 and after["RETIRED"] != before["RETIRED"]:
                    self.assertEqual((after["IRQ0_ENABLED"], after["IRQ0_PRIORITY"], after["IRQ0_PENDING"],
                                      after["PRIMASK"], after["BASEPRI"], after["FAULTMASK"]), (1, 0x80, 0, 0, 0, 0))
                    sim.set_input("IRQ0", 1)
                    requested += 1
                    pulse = True
        else:
            self.fail(f"Tasks did not finish two blocked queue waits. Cycle {sim.cycles}, PC {after['PC']:#x}, "
                      f"RAM0–RAM4 {[after[f'RAM{i}'] for i in range(5)]}.")

        self.assertEqual((requested, taken, yielded, resumed, received), (2, 2, 2, 2, 2))
        self.assertEqual(calls[SEND_FROM_ISR, IRQ0], 2)
        self.assertGreaterEqual(calls[TASK_DELAY, 0], 2)
        self.assertEqual((after["IRQ0_PENDING"], after["IRQ0_ACTIVE"]), (0, 0))
        self.assertEqual((after["CPACR"], after["FPCCR"]), (0x00F00000, 0xC0000000))


class CortexM7ExceptionRoutingTest(unittest.TestCase):
    def checked_steps(self, sim, limit):
        for _ in range(limit):
            before = sim.values
            after = sim.step()
            self.assertEqual(after["FAULT_CODE"], 0,
                             f"Cycle {sim.cycles}, PC {after['PC']:#x}, IR {after['IR']:#x}")
            yield before, after

    def test_irq0_preempts_systick_only_at_higher_priority(self):
        for priority in (0x40, 0x80):
            with self.subTest(priority=priority):
                preempts = priority < 0x80
                sim = firmware_simulation(interrupt_image(priority))
                for _, after in self.checked_steps(sim, 80):
                    if after["IPSR"] == SYSTICK and after["RAM1"] == 0x55:
                        break
                else:
                    self.fail("SysTick did not start.")

                self.assertEqual((after["MSP"], after["IRQ0_PRIORITY"]), (MSP_TOP - 32, priority))
                sim.set_input("IRQ0", 1)
                sim.step()
                sim.set_input("IRQ0", 0)
                origin = SYSTICK if preempts else 0
                resume = None
                transitions = []
                stacked_status = None
                for before, after in self.checked_steps(sim, 100):
                    previous, active = before["IPSR"], after["IPSR"]
                    if before["BUS_WRITE"] and before["BUS_ADDR"] == before["MSP"] - 4:
                        stacked_status = before["BUS_WDATA"]
                    if previous != active:
                        transitions.append((previous, active))
                    if active == IRQ0 and previous != IRQ0:
                        self.assertEqual(previous, origin)
                        self.assertEqual((before["BUS_READ"], before["BUS_ADDR"], after["PC"]), (1, 64, 0x84))
                        link = 0xFFFFFFF1 if preempts else 0xFFFFFFF9
                        self.assertEqual((after["LR"], after["MSP"], after["SP"]),
                                         (link, before["MSP"] - 32, before["MSP"] - 32))
                        self.assertIsNotNone(stacked_status)
                        self.assertEqual(stacked_status & 0x010001FF, 0x01000000 | origin)
                        resume = tuple(before[name] for name in ("PC", "MSP", "LR", "R3"))
                    if previous == IRQ0 and active != IRQ0:
                        self.assertEqual(active, origin)
                        self.assertEqual(tuple(after[name] for name in ("PC", "MSP", "LR", "R3")), resume)
                    if not active and after["RAM1"] == 0x56 and after["RAM2"] == 0x99:
                        break
                else:
                    self.fail("The handlers did not finish.")

                self.assertEqual(transitions, [(15, 16), (16, 15), (15, 0)] if preempts
                                 else [(15, 0), (0, 16), (16, 0)])
                self.assertEqual(after["RAM3"] & 0x9FF, IRQ0 if preempts else 0x800 | IRQ0)
                self.assertEqual((after["MSP"], after["IRQ0_PENDING"]), (MSP_TOP, 0))

    def test_instruction_and_bus_faults_vector_to_hardfault_and_return(self):
        cases = (
            ("Undefined instruction", 0, 0xDE00, 1 << 16, 0),  # UDF #0
            ("Even branch target", 4, 0x4700, 1 << 17, 0),     # BX R0
            ("Write to flash", 4, 0x6001, 0x8200, 4),          # STR R1, [R0]
            ("Unaligned read", 1, 0x6801, 1 << 24, 0),         # LDR R1, [R0]
        )
        for name, address, instruction, cfsr, bfar in cases:
            with self.subTest(fault=name):
                sim = firmware_simulation(hardfault_image(address, instruction))
                entered = returned = False
                for before, after in self.checked_steps(sim, 100):
                    if not before["IPSR"] and after["IPSR"] == HARDFAULT:
                        entered = True
                        self.assertEqual((before["BUS_READ"], before["BUS_ADDR"], after["PC"]), (1, 12, 0x50))
                        self.assertEqual((after["MSP"], after["LR"]), (MSP_TOP - 32, 0xFFFFFFF9))
                    if before["IPSR"] == HARDFAULT and not after["IPSR"]:
                        returned = True
                        self.assertEqual((after["PC"], after["MSP"]), (0x48, MSP_TOP))
                    if after["RAM0"] == 0x55:
                        break
                else:
                    self.fail("The fault handler did not resume the interrupted program.")
                self.assertTrue(entered and returned)
                self.assertEqual((after["RAM1"], after["RAM2"], after["BFAR"]), (1 << 30, cfsr, bfar))
