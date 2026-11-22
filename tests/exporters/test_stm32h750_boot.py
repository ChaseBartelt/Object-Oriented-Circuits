"""Step a linked STM32H750 ELF through ST startup in the logic viewer."""

import os
from pathlib import Path
import runpy
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from PySide6.QtWidgets import QApplication

from circuitlab.viewer.digital import DigitalSimulator
from circuitlab.viewer.window import prepare_digital_preview


ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = ROOT / "examples" / "advanced" / "CortexM7" / "firmware" / "stm32h750"
ELF = ROOT / "tests" / "fixtures" / "digital" / "stm32h750_stock_boot.elf"
HAL_ELF = ELF.with_name("stm32h750_hal_blink.elf")
FLASH_BASE = 0x08000000
STACK_TOP = 0x20020000
DATA_VALUE = 0x12345678
SIGNATURE = 0x44414953
RCC_CR = 0x58024400
RCC_CFGR = 0x58024410
RCC_AHB4ENR = 0x580244E0
GPIOC_MODER = 0x58020800
GPIOC_BSRR = 0x58020818
PC7 = 1 << 7


class Stm32h750BootTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        cls.app = QApplication.instance() or QApplication([])

    def open_circuit(self, source, path=None):
        session, layout, sim, error = prepare_digital_preview(source, "auto", "auto", path)
        self.assertIsNone(error)
        self.assertFalse(session.issues)
        self.assertIsNotNone(layout)
        self.assertIsNotNone(sim)
        widget = DigitalSimulator()
        self.addCleanup(widget.close)
        widget.set_prepared_source(source, sim, error, design=session.digital_design)
        self.assertTrue(widget.step_button.isEnabled())
        return widget, sim

    def open_firmware(self, elf, name):
        with TemporaryDirectory(dir=ROOT) as temporary:
            circuit = Path(temporary) / name
            generated = subprocess.run(
                [sys.executable, str(FIRMWARE / "build_firmware.py"), "--elf", str(elf),
                 "--circuit-output", str(circuit)],
                cwd=ROOT, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr)
            return self.open_circuit(circuit.read_text(encoding="utf-8"), circuit)

    def step(self, widget, sim):
        cycle = sim.cycles
        widget.step_button.click()
        self.assertIs(widget.simulation, sim, widget.status.text())
        self.assertEqual(sim.cycles, cycle + 1)
        state = sim.values
        self.assertEqual((state["FAULT"], state["IPSR"]), (0, 0),
                         f"PC={state['PC']:#x}, IR={state['IR']:#x}, "
                         f"CFSR={state['CFSR']:#x}, BFAR={state['BFAR']:#x}")
        return state

    def test_stock_elf_boots_and_updates_signature_in_viewer(self):
        builder = FIRMWARE / "build_firmware.py"
        image = runpy.run_path(str(builder))["load_elf"](ELF)
        self.assertEqual(image.stack_top, STACK_TOP)
        reset = image.symbols["Reset_Handler"] & ~1
        system_init = image.symbols["SystemInit"] & ~1
        main = image.symbols["main"] & ~1
        self.assertTrue(all(address >= FLASH_BASE for address in (reset, system_init, main)))
        data_store = image.symbols["data_value"], DATA_VALUE
        bss_address = image.symbols["bss_value"]
        signature_store = image.symbols["boot_signature"], SIGNATURE
        widget, sim = self.open_firmware(ELF, "DaisySeed_STM32H750_Boot.ckt")

        state = sim.values
        self.assertEqual((state["BUS_READ"], state["BUS_ADDR"], state["BUS_RDATA"]),
                         (1, FLASH_BASE, STACK_TOP))
        state = self.step(widget, sim)
        self.assertEqual((state["MSP"], state["BUS_READ"], state["BUS_ADDR"], state["BUS_RDATA"]),
                         (STACK_TOP, 1, FLASH_BASE + 4, reset | 1))
        state = self.step(widget, sim)
        self.assertEqual((state["PC"], state["BOOTED"]), (reset, 1))

        visited, writes = set(), set()
        for _ in range(400):
            visited.add(state["PC"])
            if state["AT_MAIN"]:
                break
            if state["BUS_WRITE"]:
                writes.add((state["BUS_ADDR"], state["BUS_WDATA"]))
            state = self.step(widget, sim)
        else:
            self.fail(f"Stock startup did not reach main. PC={state['PC']:#x}")
        self.assertIn(system_init, visited)
        self.assertIn(0x58024400, {address for address, _ in writes})  # RCC_CR
        self.assertIn(data_store, writes)
        self.assertIn((bss_address, 0), writes)
        self.assertEqual((state["PC"], state["MAIN_ADDRESS"]), (main, main))
        self.assertEqual(state["DATA_VALUE"], DATA_VALUE)
        self.assertEqual(state["BSS_VALUE"], 0)
        self.assertEqual(state["BOOT_SIGNATURE"], 0)

        for _ in range(50):
            if state["BUS_WRITE"] and (state["BUS_ADDR"], state["BUS_WDATA"]) == signature_store:
                break
            state = self.step(widget, sim)
        else:
            self.fail(f"main did not write its SRAM signature. PC={state['PC']:#x}")
        self.assertEqual(state["BOOT_SIGNATURE"], 0)
        state = self.step(widget, sim)
        self.assertEqual(state["BOOT_SIGNATURE"], SIGNATURE)
        self.assertIn(f"0x{SIGNATURE:X}", widget.output_labels["BOOT_SIGNATURE"].text())
        self.assertEqual(state["BSS_VALUE"], 0xA5)

        widget.reset_button.click()
        self.assertEqual(sim.cycles, 0)
        self.assertEqual(sim.values["BUS_ADDR"], FLASH_BASE)
        self.assertEqual(sim.values["BOOT_SIGNATURE"], 0)

    def test_hal_configures_pll_and_blinks_gpio_in_the_viewer(self):
        widget, sim = self.open_firmware(HAL_ELF, "DaisySeed_STM32H750_HAL_Blink.ckt")
        self.assertEqual((sim.values["SYSCLK_PLL"], sim.values["BOARD_LED"]), (0, 0))
        read_values = {RCC_CR: set(), RCC_CFGR: set()}
        writes = set()
        visited_systick = False
        edges = []
        for _ in range(15000):
            state = sim.values
            address = state["BUS_ADDR"]
            self.assertEqual(state["FAULT"], 0, f"Fault at PC={state['PC']:#x}")
            self.assertIn(state["IPSR"], (0, 15), f"Unexpected exception at PC={state['PC']:#x}")
            visited_systick |= state["IPSR"] == 15
            if state["BUS_READ"] and address in read_values:
                read_values[address].add(state["BUS_RDATA"])
            if state["BUS_WRITE"]:
                value = state["BUS_WDATA"]
                if address in (RCC_AHB4ENR, GPIOC_MODER):
                    writes.add((address, value))
                if address == GPIOC_BSRR and value in (PC7, PC7 << 16):
                    next_led = int(value == PC7)
                    if state["BOARD_LED"] != next_led:
                        self.assertEqual(state["SYSCLK_PLL"], 1)
                        self.assertEqual(state["CLOCK_STATUS"], 1)
                        self.assertEqual(state["CLOCK_HZ"], 400_000_000)
                        tick = state["HAL_TICK"]
                        self.assertEqual(self.step(widget, sim)["BOARD_LED"], next_led)
                        self.assertTrue(widget.output_labels["BOARD_LED"].text().endswith(f"  {next_led}"))
                        edges.append((next_led, tick))
                        if len(edges) == 3:
                            break
                        continue
            sim.step()
        else:
            self.fail(f"HAL did not blink three times. PC={state['PC']:#x}, "
                      f"status={state['CLOCK_STATUS']}, tick={state['HAL_TICK']}, edges={edges}")

        self.assertTrue(any(value & (1 << 17) for value in read_values[RCC_CR]))  # HSERDY
        self.assertTrue(any(value & (1 << 25) for value in read_values[RCC_CR]))  # PLL1RDY
        self.assertTrue(any(value >> 3 & 7 == 3 for value in read_values[RCC_CFGR]))  # SWS_PLL1
        self.assertTrue(any(address == RCC_AHB4ENR and value & 4 for address, value in writes))
        self.assertTrue(any(address == GPIOC_MODER and value >> 14 & 3 == 1 for address, value in writes))
        self.assertTrue(visited_systick)
        self.assertEqual([edge for edge, _ in edges], [1, 0, 1])
        self.assertTrue(all(second > first for (_, first), (_, second) in zip(edges, edges[1:])))
        self.assertEqual(sim.values["LED_TRANSITIONS"], 2)
        self.assertEqual((sim.values["CFSR"], sim.values["HFSR"]), (0, 0))

        widget.reset_button.click()
        self.assertEqual(sim.cycles, 0)
        self.assertEqual(sim.values["BOARD_LED"], 0)
