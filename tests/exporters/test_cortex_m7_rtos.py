"""Exercise exception-driven Cortex-M7 task switching with linked firmware."""

from pathlib import Path
import runpy
import unittest

from circuitlab.math.digital import DigitalSimulation
from examples.advanced.CortexM7.firmware.build_firmware import read_model_source


FIRMWARE = (Path(__file__).resolve().parents[2] / "examples" / "advanced"
            / "CortexM7" / "CortexM7_Firmware.ckt")
RAM_BASE = 0x20000000
MSP_TOP = RAM_BASE + 0x1000
TASK_TOPS = (RAM_BASE + 0x800, RAM_BASE + 0xC00)
SVC, PENDSV, SYSTICK = 11, 14, 15
ICSR = 0xE000ED04
PENDSVSET = 1 << 28
WATCHED = (
    "PC", "IR", "MSP", "PSP", "SP", "LR", "IPSR", "FAULT_CODE", "VECTOR_BASE",
    "PENDSV_PENDING", "PENDSV_PRIORITY", "SYSTICK_PRIORITY",
    "BUS_ADDR", "BUS_SIZE", "BUS_READ", "BUS_WRITE", "BUS_RDATA", "BUS_WDATA",
    *(f"R{i}" for i in range(4, 12)), *(f"RAM{i}" for i in range(6)),
)

# Linked with build_firmware.py --rtos so the tests can run without a C toolchain.
RTOS_IMAGE = bytes.fromhex("""
    0010002041000000000000000000000000000000000000000000000000000000
    000000000000000000000000bf0000000000000000000000c700000029010000
    3b483c490122402300f01cf83a4908603a483b490322802300f014f836494860
    34480021016041608160c1603548364901603648bf2141600021816007210160
    00dffee74038036001334360013383600133c360013303610133436101338361
    0133c3610162426200238362c362036343632749013981632649c163704708b0
    1d4800681ee0eff309802038046045608660c760424602614a46426152468261
    5a46c2611c490a6801320a604a68002a04d1886001224a60c86803e0c8600022
    4a608868046845688668c768016988464169894681698a46c1698b46203080f3
    09880e498e4670470d480e4901607047000800200000002010000020000c0020
    0400002020ed00e00000ff8010e000e0690100000000000108000020fdffffff
    04ed00e0000000100c4605462868214600f002f82860f9e781b0084400900098
    01b07047
""")


def task_stack_contains(task, address):
    return TASK_TOPS[task] - 0x100 <= address <= TASK_TOPS[task]


def snapshot(sim):
    return {name: sim.values[name] for name in WATCHED}


class CortexM7RtosTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = read_model_source(FIRMWARE)
        cls.builder = runpy.run_path(str(FIRMWARE.parent / "firmware" / "build_firmware.py"))

    def simulation(self, image=RTOS_IMAGE):
        start, begin, remainder = self.source.partition("// BEGIN LINKED FIRMWARE")
        _, end, rest = remainder.partition("// END LINKED FIRMWARE")
        self.assertTrue(begin and end)
        rom = self.builder["rom_body"](image, rtos=True)
        return DigitalSimulation(start + begin + "\n" + rom + end + rest)

    def boot(self, sim):
        for address in (0, 4):
            self.assertEqual((sim.values["BUS_ADDR"], sim.values["BUS_READ"], sim.values["BUS_SIZE"]),
                             (address, 1, 2))
            vector = sim.values["BUS_RDATA"]
            sim.step()
        self.assertEqual((sim.values["BOOTED"], sim.values["MSP"], sim.values["PC"]),
                         (1, MSP_TOP, vector & ~1))

    def task_trace(self, sim):
        trace = []
        before = snapshot(sim)
        for _ in range(2400):
            sim.step()
            after = snapshot(sim)
            trace.append((before, after))
            self.assertEqual(after["FAULT_CODE"], 0, f"Fault at PC {after['PC']:#x}")
            if after["RAM2"] >= 4 and min(after["RAM0"], after["RAM1"]) > 0 and not after["IPSR"]:
                return trace
            before = after
        self.fail(f"Firmware did not finish four task switches. Last state: {before}")

    def test_rtos_launches_and_switches_independent_psp_tasks(self):
        sim = self.simulation()
        self.boot(sim)
        trace = self.task_trace(sim)
        entries = [(index, before, after) for index, (before, after) in enumerate(trace)
                   if not before["IPSR"] and after["IPSR"]]
        self.assertTrue(entries)
        self.assertEqual(entries[0][2]["IPSR"], SVC)
        for _, before, after in entries:
            vector = before["VECTOR_BASE"] + 4 * after["IPSR"]
            self.assertEqual((before["BUS_READ"], before["BUS_SIZE"], before["BUS_ADDR"]), (1, 2, vector))
            self.assertEqual(after["PC"], before["BUS_RDATA"] & ~1)
        self.assertTrue(all(after["SP"] == after["MSP"] for _, after in trace if after["IPSR"]))
        self.assertTrue(all(not before["IPSR"] or not after["IPSR"] or before["IPSR"] == after["IPSR"]
                            for before, after in trace))

        svc_index, _, svc_active = entries[0]
        self.assertEqual((svc_active["MSP"], svc_active["LR"]), (MSP_TOP - 32, 0xFFFFFFF9))
        svc_pc = next(before["PC"] for before, _ in trace[:svc_index] if before["IR"] & 0xFF00 == 0xDF00)
        self.assertTrue(any(before["BUS_WRITE"] and before["BUS_ADDR"] == MSP_TOP - 8
                            and before["BUS_WDATA"] == svc_pc + 2 for before, _ in trace[:svc_index]))
        svc_exit = next(index for index, (before, after) in enumerate(trace)
                        if before["IPSR"] == SVC and not after["IPSR"])
        reads = [before for before, _ in trace[svc_index + 1:svc_exit + 1]
                 if before["BUS_READ"] and TASK_TOPS[0] - 32 <= before["BUS_ADDR"] < TASK_TOPS[0]]
        self.assertEqual([(read["BUS_ADDR"], read["BUS_SIZE"]) for read in reads],
                         [(address, 2) for address in range(TASK_TOPS[0] - 32, TASK_TOPS[0], 4)])
        self.assertEqual(reads[0]["LR"], 0xFFFFFFFD)
        launched = trace[svc_exit][1]
        self.assertEqual((launched["RAM3"], launched["PSP"], launched["SP"]), (0, TASK_TOPS[0], TASK_TOPS[0]))
        self.assertEqual(launched["PC"], reads[6]["BUS_RDATA"])

        delayed_pends = [index for index, (before, after) in enumerate(trace)
                         if before["IPSR"] == SYSTICK and before["BUS_WRITE"] and before["BUS_ADDR"] == ICSR
                         and before["BUS_WDATA"] & PENDSVSET and after["PENDSV_PENDING"] and after["IPSR"] == SYSTICK]
        switches = [index for index, _, after in entries if after["IPSR"] == PENDSV]
        self.assertGreaterEqual(len(switches), 4)
        self.assertEqual(len(switches), len(delayed_pends))
        for request, entry in zip(delayed_pends, switches):
            self.assertLess(request, entry)
            self.assertGreater(trace[entry][1]["PENDSV_PRIORITY"], trace[entry][1]["SYSTICK_PRIORITY"])
            self.assertTrue(any(before["IPSR"] == SYSTICK and not after["IPSR"]
                                for before, after in trace[request + 1:entry]))

        returns = [index for index, (before, after) in enumerate(trace)
                   if before["IPSR"] == PENDSV and not after["IPSR"]]
        restored = [trace[index][1] for index in returns]
        self.assertEqual([state["RAM3"] for state in restored[:4]], [1, 0, 1, 0])
        for entry, finish in zip(switches, returns, strict=True):
            self.check_saved_context(trace[entry][1], trace[finish][1], trace[entry + 1:finish + 1])
        for state in (launched, *restored):
            task = state["RAM3"]
            self.assertEqual(state["SP"], state["PSP"])
            self.assertEqual(state["MSP"], MSP_TOP)
            self.assertTrue(task_stack_contains(task, state["PSP"]))
            marker = (0x40 if task == 0 else 0x80) + 4
            self.assertEqual([state[f"R{i}"] for i in range(8, 12)], list(range(marker, marker + 4)))
            for index in (0, 1):
                self.assertTrue(task_stack_contains(index, state[f"RAM{4 + index}"]))

        writes = ([], [])
        for before, after in trace:
            address = before["BUS_ADDR"]
            if not before["BUS_WRITE"] or address not in (RAM_BASE, RAM_BASE + 4) or not before["BUS_WDATA"]:
                continue
            task = (address - RAM_BASE) // 4
            self.assertEqual((before["IPSR"], before["RAM3"], before["BUS_SIZE"]), (0, task, 2))
            self.assertTrue(task_stack_contains(task, before["PSP"]))
            self.assertEqual(after[f"RAM{task}"], before[f"RAM{task}"] + (1, 3)[task])
            writes[task].append(after[f"RAM{task}"])
        self.assertTrue(all(len(counter) > 1 for counter in writes))

    def check_saved_context(self, outgoing, incoming, handler):
        """Observe the eight software-saved registers crossing SRAM on each switch."""
        save = incoming[f"RAM{4 + outgoing['RAM3']}"]
        load = incoming[f"RAM{4 + incoming['RAM3']}"]
        saved = [(before["BUS_ADDR"], before["BUS_SIZE"], before["BUS_WDATA"]) for before, _ in handler
                 if before["BUS_WRITE"] and save <= before["BUS_ADDR"] < save + 32]
        loaded = [(before["BUS_ADDR"], before["BUS_SIZE"], before["BUS_RDATA"]) for before, _ in handler
                  if before["BUS_READ"] and load <= before["BUS_ADDR"] < load + 32]
        self.assertEqual(saved, [(save + 4 * i, 2, outgoing[f"R{i + 4}"]) for i in range(8)])
        self.assertEqual(loaded, [(load + 4 * i, 2, incoming[f"R{i + 4}"]) for i in range(8)])
        hardware_pc = load + 32 + 6 * 4
        resumed = [before["BUS_RDATA"] for before, _ in handler
                   if before["BUS_READ"] and before["BUS_ADDR"] == hardware_pc]
        self.assertEqual(resumed, [incoming["PC"]])

    def test_masked_svc_faults_without_running_a_task(self):
        source = bytearray(RTOS_IMAGE)
        entry = int.from_bytes(source[4:8], "little") & ~1
        source[entry:entry + 4] = bytes.fromhex("72b600df")  # CPSID i; SVC #0
        sim = self.simulation(source)
        self.boot(sim)
        sim.step()
        for _ in range(4):
            final = sim.step()
            if final["FAULT"]:
                break
        self.assertEqual((final["PRIMASK"], final["FAULT_CODE"], final["IPSR"], final["PC"]),
                         (1, 7, 0, entry + 2))
        self.assertEqual((final["RAM0"], final["RAM1"], final["RAM2"]), (0, 0, 0))
