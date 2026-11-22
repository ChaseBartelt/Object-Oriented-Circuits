"""Generated ROM banks retain linked bytes, address bounds and viewer identities."""

import unittest
from pathlib import Path
from runpy import run_path
from tempfile import TemporaryDirectory

from circuitlab.math.digital import DigitalSimulation
from examples.advanced.CortexM7.firmware.build_firmware import banked_rom


class CortexM7GeneratedRomTest(unittest.TestCase):
    def test_standalone_writer_keeps_grouped_ports_and_behavior(self):
        root = Path(__file__).resolve().parents[2]
        renderer = run_path(str(root / 'examples/advanced/CortexM7/firmware/stm32h750/render_target.py'))
        source = '''chip Copy(a, y) {
    a[8] = input;
    y[8] = output;
    y = a;
}
'''
        with TemporaryDirectory(dir=root) as directory:
            path = Path(directory) / 'standalone.ckt'
            renderer['write_circuit'](path, source, standalone=True)
            generated = path.read_text(encoding='utf-8')
            self.assertIn('input {', generated)
            self.assertIn('output {', generated)
            sim = DigitalSimulation(generated)
            sim.set_input('a', 173)
            self.assertEqual(sim.values['y'], 173)

    def test_generated_banks_cover_a_partial_image_and_remain_individually_visible(self):
        base = 0x08000000
        image = bytes((index * 37 + 9) % 256 for index in range(17 * 256 + 3))
        source = banked_rom(image, base=base) + """
main ROM_TEST {
    address[32] = input;
    word[32], valid = output;
    M7FirmwareRom(address, word, valid);
}
"""
        sim = DigitalSimulation(source)
        banks = [instance for instance in sim.instances if instance.name == 'M7FirmwareRomBank']
        self.assertEqual(len(banks), 18)
        self.assertEqual(len({instance.pins for instance in banks}), 18)
        for offset in range(-4, len(image) + 8):
            sim.set_input('address', base + offset)
            valid = 0 <= offset < len(image) and offset % 4 == 0
            expected = int.from_bytes(image[offset:offset + 4], 'little') if valid else 0
            self.assertEqual((sim.values['word'], sim.values['valid']), (expected, int(valid)), offset)
        for address in (0, base - 256, base + 8192, 0xFFFFFFFF):
            sim.set_input('address', address)
            self.assertEqual((sim.values['word'], sim.values['valid']), (0, 0))
