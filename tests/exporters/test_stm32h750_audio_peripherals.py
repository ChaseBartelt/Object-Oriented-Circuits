"""Exercise the Daisy Seed SAI1 and DMA1 request path with a shared SRAM bus."""

from pathlib import Path
from runpy import run_path
import unittest

from circuitlab.language.thumb_assembler import assemble_thumb
from circuitlab.math.digital import DigitalSimulation


MODEL = (Path(__file__).resolve().parents[2] / "examples" / "advanced" / "CortexM7"
         / "firmware" / "stm32h750" / "STM32H750_DaisyAudio.ckt")
SAI1 = 0x40015800
DMA1 = 0x40020000
DMAMUX1 = 0x40020800
TX = 0x30000000
RX = 0x30000100
STREAM_WORDS = 4
DMA_CONTROL = (2 << 13) | (2 << 11) | (1 << 10) | (1 << 8) | (1 << 4) | (1 << 3) | 1
SAI_24BIT = 6 << 5
SAI_DMAEN = 1 << 17
SAI_ENABLE = 1 << 16
SAI_FIFO_LEVEL = 7 << 16
FLASH = 0x08000000
DTCM = 0x20000000
STACK = DTCM + 0x400

BOARD_SETUP = (
    (0x580244D8, 1),                  # RCC AHB1 DMA1 clock
    (0x580244E0, 1 << 1),             # RCC AHB4 GPIOB clock
    (0x580244F0, 1 << 22),            # RCC APB2 SAI1 clock
    (0x58020400, 1 << 22),            # PB11 output
    (0x58020418, 1 << 11),            # Release codec reset
    (0xE000E408, 0x40000000),         # DMA1 Stream0 priority (IRQ11)
    (0xE000E40C, 0x00000080),         # DMA1 Stream1 priority (IRQ12)
    (0xE000E100, (1 << 11) | (1 << 12)),
    (TX, 0x100001), (TX + 4, 0xF00002),
    (DMAMUX1, 87), (DMAMUX1 + 4, 88),
    (DMA1 + 0x14, 8), (DMA1 + 0x18, SAI1 + 0x20), (DMA1 + 0x1C, TX),
    (DMA1 + 0x10, DMA_CONTROL | (1 << 6)),
    (DMA1 + 0x2C, 8), (DMA1 + 0x30, SAI1 + 0x40), (DMA1 + 0x34, RX),
    (DMA1 + 0x28, DMA_CONTROL),
    (SAI1 + 0x0C, 63), (SAI1 + 0x10, (3 << 16) | (1 << 8)),
    (SAI1 + 0x2C, 63), (SAI1 + 0x30, (3 << 16) | (1 << 8)),
    (SAI1 + 0x24, SAI_24BIT | SAI_DMAEN | SAI_ENABLE | (1 << 10) | 3),
    (SAI1 + 0x04, SAI_24BIT | SAI_DMAEN),
)

HARNESS = """
main DaisyAudioTest {
    clk, reset, write_enable, sai_enabled, dma_enabled, codec_reset_n, dma_grant = input;
    address[32], size_code[2], write_data[32], dma_read_data[32] = input;
    read_data[32], dma_address[32], dma_write_data[32] = output;
    mapped, dma_request, dma_write, irq0, irq1 = output;
    output_left[32], output_right[32], frame_count[32], input_left[32], input_right[32] = output;
    STM32H750DaisyAudio(clk, reset, address, size_code, write_data, write_enable,
                       sai_enabled, dma_enabled, codec_reset_n, dma_grant, dma_read_data,
                       read_data, mapped, dma_request, dma_write, dma_address, dma_write_data,
                       irq0, irq1, output_left, output_right, frame_count, input_left, input_right);
}
"""

CPU_AUDIO_PROGRAM = f"""
    .word {STACK:#x}, reset + 0x08000001
    .org 0x0C
    .word fault + 0x08000001
    .org 0x6C
    .word tx_handler + 0x08000001, rx_handler + 0x08000001
    .org 0x80
reset:
    LDR r4, control_words
    LDR r5, setup_pointer
    LDR r6, setup_end
configure:
    LDR r0, [r5]
    LDR r1, [r5, #4]
    ADDS r5, #8
    STR r1, [r0]
    CMP r5, r6
    BNE configure
    LDR r0, sai_a_status
    LDR r2, fifo_level
wait_tx:
    LDR r1, [r0]
    .hword 0x4011 // ANDS r1, r2
    BEQ wait_tx
    LDR r0, sai_a_control
    LDR r1, sai_a_enable
    STR r1, [r0]
main_loop:
    LDR r7, [r4, #8]
    ADDS r7, #1
    STR r7, [r4, #8]
    B main_loop

tx_handler:
    LDR r0, dma_flags
    LDR r1, [r0]
    MOVS r2, #48
    .hword 0x4011 // ANDS r1, r2
    STR r1, [r0, #8]
    LDR r0, control_words
    LDR r1, [r0]
    ADDS r1, #1
    STR r1, [r0]
    BX lr

rx_handler:
    LDR r0, dma_flags
    LDR r1, [r0]
    LDR r2, dma_rx_mask
    .hword 0x4011 // ANDS r1, r2
    STR r1, [r0, #8]
    LDR r2, rx_address
    LDR r3, tx_address
    .hword 0x0AC9 // LSRS r1, r1, #11
    BEQ copy_half
    ADDS r2, #16
    ADDS r3, #16
copy_half:
    MOVS r1, #4
copy_word:
    LDR r0, [r2]
    STR r0, [r3]
    ADDS r2, #4
    ADDS r3, #4
    SUBS r1, #1
    BNE copy_word
    LDR r0, control_words
    LDR r1, [r0, #4]
    ADDS r1, #1
    STR r1, [r0, #4]
    BX lr

fault:
    B fault
    .align 2
control_words: .word {DTCM:#x}
setup_pointer: .word setup_pairs + 0x08000000
setup_end: .word after_setup + 0x08000000
sai_a_status: .word {SAI1 + 0x18:#x}
fifo_level: .word {SAI_FIFO_LEVEL:#x}
sai_a_control: .word {SAI1 + 4:#x}
sai_a_enable: .word {SAI_24BIT | SAI_DMAEN | SAI_ENABLE:#x}
dma_flags: .word {DMA1:#x}
dma_rx_mask: .word 0xC00
rx_address: .word {RX:#x}
tx_address: .word {TX:#x}
setup_pairs:
""" + "\n".join(f"    .word {address:#x}, {value:#x}" for address, value in BOARD_SETUP) + "\nafter_setup:\n"


class Stm32h750AudioPeripheralsTest(unittest.TestCase):
    def test_sai1_dma_primes_fills_and_replays_circular_stereo_words(self):
        sim = DigitalSimulation(MODEL.read_text(encoding="utf-8") + HARNESS)
        sim.set_input("size_code", 2)
        memory = dict(zip(range(TX, TX + 16, 4), (0x100001, 0xF00002, 0x200003, 0xE00004)))

        def read(address):
            sim.set_input("address", address)
            self.assertTrue(sim.values["mapped"], f"Unmapped MMIO address {address:#010x}")
            return sim.values["read_data"]

        def write(address, value):
            read(address)
            sim.set_input("write_data", value)
            sim.set_input("write_enable", 1)
            sim.step()
            sim.set_input("write_enable", 0)

        def run_until(predicate, cycles):
            for _ in range(cycles):
                state = sim.values
                if predicate(state):
                    return
                if state["dma_request"]:
                    address = state["dma_address"]
                    if state["dma_write"]:
                        self.assertIn(address, range(RX, RX + 16, 4))
                        memory[address] = state["dma_write_data"]
                    else:
                        self.assertIn(address, range(TX, TX + 16, 4))
                        sim.set_input("dma_read_data", memory[address])
                    sim.set_input("dma_grant", 1)
                sim.step()
                sim.set_input("dma_grant", 0)
            self.fail("Audio bus did not reach the expected state.")

        write(DMAMUX1, 87)
        sim.set_input("dma_enabled", 1)
        self.assertEqual(read(DMAMUX1), 0, "DMA clock must gate writes")
        sim.set_input("sai_enabled", 1)
        for offset, request, peripheral, buffer in ((0x10, 87, SAI1 + 0x20, TX),
                                                      (0x28, 88, SAI1 + 0x40, RX)):
            write(DMAMUX1 + (0 if request == 87 else 4), request)
            write(DMA1 + offset + 4, STREAM_WORDS)
            write(DMA1 + offset + 8, peripheral)
            write(DMA1 + offset + 12, buffer)
            write(DMA1 + offset, DMA_CONTROL | ((1 << 6) if request == 87 else 0))
        for offset in (0x04, 0x24):
            write(SAI1 + offset + 8, 63)
            write(SAI1 + offset + 12, 0xFFFF0180)  # HAL's two 32-bit slots and all-slot enable mask.
        write(SAI1 + 0x24, SAI_24BIT | SAI_DMAEN | SAI_ENABLE | (1 << 10) | 3)
        write(SAI1 + 0x04, SAI_24BIT | SAI_DMAEN)

        self.assertEqual(read(SAI1 + 0x18) & SAI_FIFO_LEVEL, 0)
        self.assertEqual(sim.values["dma_address"], TX)
        sim.step()
        self.assertEqual(sim.values["dma_address"], TX, "A stalled DMA request must retain its address")
        run_until(lambda state: not state["dma_request"], 3)
        self.assertNotEqual(read(SAI1 + 0x18) & SAI_FIFO_LEVEL, 0, "HAL primes TX before setting SAIEN")
        self.assertEqual(read(DMA1 + 0x14), STREAM_WORDS - 1)
        write(SAI1 + 0x04, SAI_24BIT | SAI_DMAEN | SAI_ENABLE)
        for _ in range(40):
            sim.step()
        self.assertEqual(sim.values["frame_count"], 0, "The codec reset holds off serial frames")

        sim.set_input("codec_reset_n", 1)
        run_until(lambda state: state["frame_count"] == 1 and state["irq1"], 80)
        self.assertEqual((sim.values["output_left"], sim.values["output_right"]), (0x100001, 0xF00002))
        self.assertEqual((memory[RX], memory[RX + 4]), (0, 0x400000))
        self.assertEqual(read(DMA1) & 0x430, 0x410)
        write(DMA1 + 0x08, 0x410)
        self.assertFalse(sim.values["irq0"])
        self.assertFalse(sim.values["irq1"])

        run_until(lambda state: state["frame_count"] == 2 and state["irq1"], 80)
        self.assertEqual((sim.values["output_left"], sim.values["output_right"]), (0x200003, 0xE00004))
        self.assertEqual((memory[RX + 8], memory[RX + 12]), (0x200000, 0x200000))
        self.assertEqual(read(DMA1) & 0x820, 0x820)
        self.assertEqual(read(DMA1 + 0x14), STREAM_WORDS)
        self.assertEqual(read(DMA1 + 0x2C), STREAM_WORDS)

        # A CPU callback can rewrite the TX buffer before the ring consumes it again.
        for offset in range(0, 16, 4):
            memory[TX + offset] = memory[RX + offset]
        write(DMA1 + 0x08, 0x820)
        run_until(lambda state: state["frame_count"] == 3, 80)
        self.assertEqual((sim.values["output_left"], sim.values["output_right"]), (0, 0x400000))
        self.assertEqual((sim.values["input_left"], sim.values["input_right"]), (0x400000, 0))

    def test_rendered_cpu_services_dma_irqs_and_writes_the_playback_buffer(self):
        words = assemble_thumb((line, index) for index, line in enumerate(CPU_AUDIO_PROGRAM.splitlines(), 1))
        firmware = b"".join(words.get(address, 0).to_bytes(4, "little")
                            for address in range(0, max(words) + 4, 4))
        render_demo = run_path(str(MODEL.with_name("render_target.py")))["render_demo"]
        source = render_demo(
            firmware, [(DTCM, 12), (TX, RX + 32 - TX)], STACK, FLASH + 0x80,
            {"TX_INTERRUPTS": DTCM, "RX_INTERRUPTS": DTCM + 4, "CPU_WRITES": DTCM + 8}, audio=True,
        )
        sim = DigitalSimulation(source)
        interrupts = set()
        vector_reads = set()
        transmit = []
        for _ in range(950):
            state = sim.values
            self.assertFalse(state["FAULT"] or state["HFSR"] or state["CFSR"],
                             f"Fault at cycle {sim.cycles}, PC={state['PC']:#x}, "
                             f"IR={state['IR']:#x}, address={state['BUS_ADDR']:#x}")
            if state["IPSR"]:
                interrupts.add(state["IPSR"])
            if state["BUS_READ"] and state["BUS_ADDR"] in (FLASH + 0x6C, FLASH + 0x70):
                vector_reads.add(state["BUS_ADDR"])
            if len(transmit) < state["AUDIO_FRAMES"]:
                transmit.append((state["AUDIO_OUT_LEFT"], state["AUDIO_OUT_RIGHT"]))
            if len(transmit) >= 5 and state["IPSR"] == 0:
                break
            sim.step()
        self.assertEqual(interrupts, {27, 28})
        self.assertEqual(vector_reads, {FLASH + 0x6C, FLASH + 0x70})
        self.assertGreaterEqual(state["TX_INTERRUPTS"], 2)
        self.assertGreaterEqual(state["RX_INTERRUPTS"], 2)
        self.assertGreater(state["CPU_WRITES"], 0)
        self.assertGreaterEqual(len(transmit), 5)
        self.assertEqual(transmit[0], (0x100001, 0xF00002))
        self.assertEqual(transmit[4], (0, 0x400000))


if __name__ == "__main__":
    unittest.main()
