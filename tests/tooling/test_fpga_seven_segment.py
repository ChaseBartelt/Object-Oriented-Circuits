"""Board pin locations, polarity and timing for the six DE10-Lite displays."""

import re
import unittest

from circuitlab.math.digital import DigitalSimulation
from circuitlab.model.graph import CircuitError
from circuitlab.tooling import fpga


# Independent transcription of Terasic DE10-Lite manual Table 3-6 (June 2020).
# Within each digit the columns are a,b,c,d,e,f,g,decimal point, in that order.
DISPLAY_PINS = (
    ("C14", "E15", "C15", "C16", "E16", "D17", "C17", "D15"),
    ("C18", "D18", "E18", "B16", "A17", "A18", "B17", "A16"),
    ("B20", "A20", "B19", "A21", "B21", "C22", "B22", "A19"),
    ("F21", "E22", "E21", "C19", "C20", "D19", "E17", "D22"),
    ("F18", "E20", "E19", "J18", "H19", "F19", "F20", "F17"),
    ("J20", "K20", "L18", "N18", "M20", "N19", "N20", "L19"),
)


def display_source(*, clocked=False):
    clock = "MAX10_CLK1_50, " if clocked else ""
    displays = ", ".join(f"HEX{digit}[7:0]" for digit in range(6))
    assignments = " ".join(f"HEX{digit}[7:0] = SW[7:0] ^ {digit * 17};" for digit in range(6))
    return (f"chip HYBRID01({clock}SW[7:0], {displays}) {{"
            f"{clock}SW[7:0] = input; {displays} = output; {assignments}}}")


class SevenSegmentBoardTest(unittest.TestCase):
    def test_all_physical_segment_pins_match_the_manual_without_collisions(self):
        expected = {f"HEX{digit}{bit}": pin for digit, pins in enumerate(DISPLAY_PINS)
                    for bit, pin in enumerate(pins)}
        self.assertEqual(fpga.HEX_PINS, expected)
        self.assertEqual(len(expected), 48)
        self.assertEqual(len(fpga.PINS), len(set(fpga.PINS.values())))
        for name, pin in expected.items():
            self.assertEqual(fpga.PINS[name], pin)
            self.assertEqual(fpga.PORTS[name], "output")

    def test_clockless_display_only_outputs_get_exact_locations_and_timing(self):
        hdl, qsf, sdc = fpga.compile_de10_lite(display_source())
        locations = dict((name, pin) for pin, name in re.findall(
            r"set_location_assignment PIN_(\w+) -to (\w+)", qsf))
        expected = {f"HEX{digit}{bit}": pin for digit, pins in enumerate(DISPLAY_PINS)
                    for bit, pin in enumerate(pins)}
        expected.update({f"SW{bit}": fpga.SWITCH_PINS[f"SW{bit}"] for bit in range(8)})
        self.assertEqual(locations, expected)
        for name in expected:
            self.assertIn(f'IO_STANDARD "3.3-V LVTTL" -to {name}', qsf)
        self.assertNotIn("LEDR", qsf)
        self.assertNotIn("AUDIO_OUT", qsf)
        self.assertNotIn("CURRENT_STRENGTH_NEW", qsf)
        self.assertNotIn("always", hdl)
        self.assertNotIn("create_clock", sdc)
        self.assertIn("set_max_delay 20.000", sdc)
        for name in fpga.HEX_PINS:
            self.assertIn(name, sdc)

    def test_all_segment_patterns_preserve_bit_order_and_active_low_polarity(self):
        for profile in ("ooc", "de10-lite"):
            with self.subTest(profile=profile):
                sim = DigitalSimulation(display_source(), profile)
                self.assertFalse(sim.clocks)
                self.assertEqual(len(sim.outputs), 48)
                for pattern in range(256):
                    for bit in range(8):
                        sim.set_input(f"SW{bit}", (pattern >> bit) & 1)
                    for digit in range(6):
                        value = sum(sim.values[f"HEX{digit}{bit}"] << bit for bit in range(8))
                        self.assertEqual(value, pattern ^ (digit * 17))
                self.assertEqual(sim.cycles, 0)
                # All ones means every physical segment is off, including DP.
                self.assertTrue(all(sim.values[f"HEX0{bit}"] == 1 for bit in range(8)))

    def test_clocked_display_profile_retains_two_stage_switch_synchronization(self):
        source = display_source(clocked=True)
        hdl, qsf, sdc = fpga.compile_de10_lite(source)
        self.assertIn("module ooc_user_HYBRID01", hdl)
        self.assertIn("create_clock", sdc)
        self.assertIn("set_false_path -to [get_ports {HEX07", sdc)
        for bit in range(8):
            self.assertIn(f"ooc_sw{bit}_meta", qsf)
            self.assertIn(f"ooc_sw{bit}_sync", qsf)
        sim = DigitalSimulation(source, "de10-lite")
        self.assertEqual(sim.frequency, 50_000_000)
        sim.set_input("SW0", 1)
        sim.set_input("SW7", 1)
        self.assertEqual(sim.values["HEX00"], 0)
        self.assertEqual(sim.values["HEX07"], 0)
        sim.step()
        self.assertEqual(sim.values["HEX00"], 0)
        sim.step()
        self.assertEqual(sim.values["HEX00"], 1)
        self.assertEqual(sim.values["HEX07"], 1)

    def test_individual_segments_and_decimal_points_need_no_full_digit_declaration(self):
        source = "chip HYBRID01(SW0, HEX00, HEX57) {SW0=input; HEX00,HEX57=output; HEX00=SW0;HEX57=!SW0;}"
        _, qsf, sdc = fpga.compile_de10_lite(source)
        self.assertIn("PIN_C14 -to HEX00", qsf)
        self.assertIn("PIN_L19 -to HEX57", qsf)
        self.assertNotIn("HEX01", qsf)
        self.assertIn("-to [get_ports {HEX00 HEX57}]", sdc)

    def test_nested_chip_can_drive_display_bank_through_main(self):
        source = '''
        chip Invert(a, segments[7:0]) {
            a[8]=input; segments[7:0]=output;
            segments[7:0]=a ^ 255;
        }
        chip Displays(SW[7:0], HEX0[7:0], HEX5[7:0]) {
            SW[7:0]=input; HEX0[7:0],HEX5[7:0]=output;
            input_value[8]=SW[7:0];
            Invert(input_value,HEX0[7:0]);
            HEX5[7:0]=SW[7:0];
        }
        main HYBRID01 {
            SW[7:0]=input; HEX0[7:0],HEX5[7:0]=output;
            Displays(SW[7:0],HEX0[7:0],HEX5[7:0]);
        }
        '''
        _, qsf, _ = fpga.compile_de10_lite(source)
        self.assertIn("PIN_C14 -to HEX00", qsf)
        self.assertIn("PIN_L19 -to HEX57", qsf)
        sim = DigitalSimulation(source, "de10-lite")
        sim.set_input("SW2", 1)
        for digit, expected in ((0, 251), (5, 4)):
            actual = sum(sim.values[f"HEX{digit}{bit}"] << bit for bit in range(8))
            self.assertEqual(actual, expected)

    def test_shifted_inferred_wires_feed_decoders_and_display_enable(self):
        source = '''chip HYBRID01(SW[9:0],HEX0[7:0],HEX1[7:0]) {
            SW[9:0]=input; HEX0[7:0],HEX1[7:0]=output;
            requests[8]=SW[7:0];
            nibble[4]=requests >> 4;
            segments[8]=sevenseg(nibble,SW8);
            HEX0[7:0]=segments;
            HEX1[7:0]=SW9 ? sevenseg(requests) : 255;
        }'''
        # Conventional hexadecimal digits, low means lit and DP defaults off.
        masks = (192, 249, 164, 176, 153, 146, 130, 248,
                 128, 144, 136, 131, 198, 161, 134, 142)
        for profile in ("ooc", "de10-lite"):
            sim = DigitalSimulation(source, profile)
            for name in ("requests", "nibble", "segments"):
                self.assertEqual(sim.signal_kinds[name], "wire")
            for pattern in (0, 1, 10, 15, 16, 164, 255):
                for point in (0, 1):
                    for enabled in (0, 1):
                        with self.subTest(profile=profile, value=pattern, point=point, enabled=enabled):
                            for bit in range(8):
                                sim.set_input(f"SW{bit}", (pattern >> bit) & 1)
                            sim.set_input("SW8", point)
                            sim.set_input("SW9", enabled)
                            expected = (masks[pattern >> 4] & (127 if point else 255),
                                        masks[pattern & 15] if enabled else 255)
                            for digit in range(2):
                                value = sum(sim.values[f"HEX{digit}{bit}"] << bit for bit in range(8))
                                self.assertEqual(value, expected[digit])
            self.assertEqual(sim.cycles, 0)

    def test_unknown_digits_segments_wrong_directions_and_vector_ports_are_rejected(self):
        cases = (
            "chip HYBRID01(SW0,HEX60){SW0=input;HEX60=output;HEX60=SW0;}",
            "chip HYBRID01(SW0,HEX08){SW0=input;HEX08=output;HEX08=SW0;}",
            "chip HYBRID01(SW0,HEX00,LEDR0){SW0,HEX00=input;LEDR0=output;LEDR0=SW0 & HEX00;}",
            "chip HYBRID01(SW0,HEX0){SW0=input;HEX0[8]=output;HEX0=SW0 ? 255:0;}",
        )
        for source in cases:
            with self.subTest(source=source), self.assertRaises(fpga.FpgaError):
                fpga.compile_de10_lite(source)

    def test_declared_decimal_point_must_be_driven_instead_of_left_floating(self):
        source = '''chip HYBRID01(SW0,HEX0[7:0]) {
            SW0=input; HEX0[7:0]=output;
            HEX0[6:0]=SW0 ? 127 : 0;
        }'''
        with self.assertRaisesRegex(CircuitError, 'HEX07.*assignment'):
            fpga.compile_de10_lite(source)
        source = source.replace("HEX0[6:0]=", "HEX07=1; HEX0[6:0]=")
        sim = DigitalSimulation(source, "de10-lite")
        self.assertEqual(sim.values["HEX07"], 1)
        sim.set_input("SW0", 1)
        self.assertTrue(all(sim.values[f"HEX0{bit}"] == 1 for bit in range(8)))


if __name__ == "__main__":
    unittest.main()
