"""Exercise FPGA preparation and verification without invoking hardware tools."""
# Prepare board projects and verify artifacts using fixtures rather than running a programmer.
# Exit success alone must not authorize a stale image, wrong device chain, or changed source.

import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from circuitlab.tooling import fpga
from circuitlab.exporters.verilog import export_verilog


ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / "tests/fixtures/digital/HYBRID01.ckt").read_text(encoding="utf-8")
GOOD_SUMMARY = "\n".join(
    f"Type : Slow Model {kind} 'MAX10_CLK1_50'\nSlack : {slack}\nTNS : 0.000\n"
    for kind, slack in (("Setup", "14.706"), ("Hold", "0.148"), ("Minimum Pulse Width", "9.325")))
GOOD_TIMING = "\n".join(f"; {label} ; 0 ; 0 ;" for label in fpga.UNCONSTRAINED)
GOOD_FLOW = f"; Flow Status ; Successful - today ;\n; Device ; {fpga.DEVICE} ;\n"
DIRECT_SOURCE = '''chip HYBRID01(MAX10_CLK1_50, SW0, LEDR0, AUDIO_OUT) {
    MAX10_CLK1_50, SW0 = input;
    LEDR0, AUDIO_OUT = output;
    LEDR0 = SW0;
    AUDIO_OUT = !SW0;
}'''
CLOCKLESS_SOURCE = DIRECT_SOURCE.replace('MAX10_CLK1_50, ', '')
LEGACY_SOURCE = '''chip HYBRID01(MAX10_CLK1_50, SW0, LEDR0, AUDIO_OUT) {
    MAX10_CLK1_50, SW0 = input;
    LEDR0, AUDIO_OUT = output;
    sw_meta;
    sw_sync;
    clock(MAX10_CLK1_50, 50MHz) {
        sw_meta = SW0;
        sw_sync = sw_meta;
    }
    LEDR0 = sw_sync;
    AUDIO_OUT = !sw_sync;
}'''
# DE10-Lite manual, slide-switch and LED pin tables. Keep expected physical
# locations independent of the backend's board profile dictionary.
SWITCH_PINS = ('C10', 'C11', 'D12', 'C12', 'A12', 'B12', 'A13', 'A14', 'B14', 'F15')
LED_PINS = ('A8', 'A9', 'A10', 'B10', 'D13', 'C13', 'E14', 'D14', 'A11', 'B11')


def switch_led_source(indices=range(10), *, audio=False):
    indices = tuple(indices)
    inputs = ['MAX10_CLK1_50', *(f'SW{i}' for i in indices)]
    outputs = [*(f'LEDR{i}' for i in indices), *(['AUDIO_OUT'] if audio else [])]
    assignments = [f'LEDR{i} = SW{i};' for i in indices]
    if audio:
        assignments.append(f'AUDIO_OUT = SW{indices[0]};')
    return (f'chip HYBRID01({", ".join(inputs + outputs)}) {{\n'
            f'{", ".join(inputs)} = input;\n{", ".join(outputs)} = output;\n'
            + '\n'.join(assignments) + '\n}')


class FpgaBackendTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = fpga.prepare_project(SOURCE, self.root)

    def built(self, summary=GOOD_SUMMARY, timing=GOOD_TIMING, flow=GOOD_FLOW):
        out = self.project.directory / "build/output_files"
        out.mkdir(exist_ok=True)
        for name, data in {"HYBRID01.sof": b"sample bitstream", "HYBRID01.sta.summary": summary.encode(),
                           "HYBRID01.sta.rpt": timing.encode(), "HYBRID01.flow.rpt": flow.encode()}.items():
            path = out / name
            path.write_bytes(data)
            stamp = json.loads((self.project.directory / "ooc-build.json").read_text())["input_mtime_ns"] + 1_000_000
            os.utime(path, ns=(stamp, stamp))
        return out

    def test_current_source_is_compiled_and_builds_are_isolated(self):
        edited = SOURCE.replace("440Hz", "220Hz")
        other = fpga.prepare_project(edited, self.root)
        self.assertNotEqual(self.project.directory, other.directory)
        self.assertNotEqual((self.project.directory / "build/HYBRID01.v").read_text(),
                            (other.directory / "build/HYBRID01.v").read_text())
        self.assertTrue(other.matches_source(edited))
        self.assertFalse(other.matches_source(SOURCE))
        self.assertFalse((other.directory / "build/output_files").exists())

    def test_packaged_constraints_match_verified_board_project(self):
        actual = tuple(fpga.canonical_constraints((ROOT / "tests/fixtures/fpga_board" / name).read_text())
                       for name in ('HYBRID01.qsf', 'HYBRID01.sdc'))
        # Independent board constraints archived from commit 890a7698f9ee;
        # the optional root FPGA project was removed. Accept a matching profile,
        # never a mixed legacy/auto pair, without regenerating this reference.
        profiles = ((fpga.QSF, fpga.SDC), fpga.compile_de10_lite(DIRECT_SOURCE)[1:],
                    fpga.compile_de10_lite(switch_led_source())[1:])
        self.assertIn(actual, [tuple(fpga.canonical_constraints(text) for text in pair) for pair in profiles])

    def test_profile_rejects_other_top_ports_or_incorrect_clock(self):
        for source in (SOURCE.replace("HYBRID01", "OtherTop"), SOURCE.replace("LEDR0", "LED0"),
                       SOURCE.replace("50MHz", "25MHz"), SOURCE.replace(", 50MHz", ""),
                       SOURCE + "\nchip Extra(a, y) { a = input; y = output; y = a; }\n"):
            with self.subTest(source=source[:40]), self.assertRaises(ValueError):
                fpga.prepare_project(source, self.root)

    def test_verified_artifacts_are_fingerprinted_and_can_be_rechecked(self):
        out = self.built()
        self.assertEqual(fpga.verify_build(self.project), out / "HYBRID01.sof")
        self.assertEqual(fpga.verify_build(self.project), out / "HYBRID01.sof")
        fingerprints = json.loads((self.project.directory / "ooc-verified.json").read_text())
        self.assertIn("HYBRID01.sof", fingerprints)
        self.assertEqual(len(fingerprints), 4)

    def test_missing_or_old_bitstream_is_rejected(self):
        with self.assertRaisesRegex(fpga.FpgaError, "fresh"):
            fpga.verify_build(self.project)
        out = self.built()
        os.utime(out / "HYBRID01.sof", ns=(1, 1))
        with self.assertRaisesRegex(fpga.FpgaError, "fresh"):
            fpga.verify_build(self.project)

    def test_negative_nonfinite_or_incomplete_timing_is_rejected(self):
        for summary in (GOOD_SUMMARY.replace("14.706", "-0.001"), GOOD_SUMMARY.replace("14.706", "nan"),
                        GOOD_SUMMARY.replace("14.706", "inf"), GOOD_SUMMARY.replace("Slack : 0.148", "missing"),
                        GOOD_SUMMARY.replace("Hold", "Recovery"), ""):
            with self.subTest(summary=summary):
                self.built(summary=summary)
                with self.assertRaises(fpga.FpgaError):
                    fpga.verify_build(self.project)

    def test_clockless_build_verifies_propagation_timing_without_pulse_width(self):
        self.project = fpga.prepare_project(CLOCKLESS_SOURCE, self.root)
        summary = "\n".join(f"Type : Slow Model {kind} 'n/a'\nSlack : {slack}\n"
                            for kind, slack in (("Setup", "8.970"), ("Hold", "3.213")))
        out = self.built(summary=summary)
        self.assertEqual(fpga.verify_build(self.project), out / "HYBRID01.sof")
        # Clockless still requires complete, finite, nonnegative path timing.
        for invalid in (summary.replace("8.970", "-0.001"), summary.replace("8.970", "nan"),
                        summary.replace("Hold", "Other"), ""):
            with self.subTest(summary=invalid), self.assertRaises(fpga.FpgaError):
                self.built(summary=invalid)
                fpga.verify_build(self.project)

    def test_clockless_build_still_rejects_unconstrained_paths(self):
        self.project = fpga.prepare_project(CLOCKLESS_SOURCE, self.root)
        self.built(timing=GOOD_TIMING.replace("; Unconstrained Input Port Paths ; 0 ; 0 ;",
                                             "; Unconstrained Input Port Paths ; 1 ; 0 ;"))
        with self.assertRaisesRegex(fpga.FpgaError, "unconstrained"):
            fpga.verify_build(self.project)

    def test_failed_flow_wrong_device_or_unconstrained_paths_are_rejected(self):
        for flow, timing in ((GOOD_FLOW.replace("Successful", "Failed"), GOOD_TIMING),
                             (GOOD_FLOW.replace(fpga.DEVICE, "10M25DAF484C7G"), GOOD_TIMING),
                             (GOOD_FLOW, GOOD_TIMING.replace("; 0 ; 0 ;", "; 1 ; 0 ;", 1)),
                             (GOOD_FLOW, GOOD_TIMING + "\n; Unconstrained Clocks ; 0 ; 1 ;"),
                             (GOOD_FLOW, ""), (GOOD_FLOW + "Critical Warning (1): bad", GOOD_TIMING)):
            self.built(flow=flow, timing=timing)
            with self.subTest(flow=flow, timing=timing), self.assertRaises(fpga.FpgaError):
                fpga.verify_build(self.project)

    def test_changed_generated_hdl_or_constraints_are_rejected(self):
        self.built()
        for filename in fpga.INPUTS:
            path = self.project.directory / filename
            original = path.read_text()
            path.write_text(original + "\nchanged\n")
            with self.subTest(filename=filename), self.assertRaisesRegex(fpga.FpgaError, "project changed"):
                fpga.verify_build(self.project)
            path.write_text(original)

    def test_quartus_generated_safe_fields_are_accepted_but_pin_setting_changes_are_not(self):
        self.built()
        path = self.project.directory / "HYBRID01.qsf"
        path.write_text(path.read_text() + '\nset_global_assignment -name LAST_QUARTUS_VERSION "25.1std.0 Lite Edition"\n'
                        'set_global_assignment -name RESERVE_ALL_UNUSED_PINS_NO_OUTPUT_GND "AS INPUT TRI-STATED"\n')
        fpga.verify_build(self.project)
        path.write_text(path.read_text().replace('NO_OUTPUT_GND "AS INPUT TRI-STATED"', 'NO_OUTPUT_GND "AS OUTPUT DRIVING GROUND"'))
        with self.assertRaises(fpga.FpgaError):
            fpga.verify_build(self.project)

    def test_modifying_verified_bitstream_is_rejected(self):
        out = self.built()
        fpga.verify_build(self.project)
        (out / "HYBRID01.sof").write_bytes(b"replacement bitstream")
        with self.assertRaises(fpga.FpgaError):
            fpga.verify_build(self.project)

    def test_program_argv_requires_current_source_and_uses_sof_on_device_one(self):
        out = self.built()
        bins = self.root / "Quartus folder/bin64"
        cable = "USB-Blaster [USB-0]"
        argv = fpga.program_command(self.project, bins, cable, source=SOURCE)
        self.assertEqual(argv, [str(bins / "quartus_pgm.exe"), "-m", "JTAG", "-c", cable,
                                "-o", f"P;{out / 'HYBRID01.sof'}@1"])
        with self.assertRaisesRegex(fpga.FpgaError, "source changed"):
            fpga.program_command(self.project, bins, cable, source=SOURCE + "# edit\n")
        self.assertEqual(fpga.build_command(self.project, bins), [str(bins / "quartus_sh.exe"), "--flow", "compile", "HYBRID01"])
        self.assertEqual(fpga.cable_list_command(bins), [str(bins / "quartus_pgm.exe"), "-l"])
        self.assertEqual(fpga.chain_command(bins), [str(bins / "jtagconfig.exe"), "--enum"])

    def test_discover_explicit_and_path_quartus(self):
        bins = self.root / "bin64"
        bins.mkdir()
        for name in ("quartus_sh.exe", "quartus_pgm.exe", "jtagconfig.exe"):
            (bins / name).touch()
        self.assertEqual(fpga.discover_quartus(str(bins)), bins.resolve())
        self.assertEqual(fpga.discover_quartus(str(bins / "quartus_sh.exe")), bins.resolve())
        with patch.object(fpga.shutil, "which", return_value=str(bins / "quartus_sh.exe")):
            self.assertEqual(fpga.discover_quartus(), bins.resolve())
        with self.assertRaises(fpga.FpgaError):
            fpga.discover_quartus(str(self.root / "missing"))

    def test_cable_output_rejects_reported_errors_even_with_valid_cables(self):
        output = "Info: header\n1) USB-Blaster [USB-0]\n2) USB-BlasterII [USB-1]\n"
        self.assertEqual(fpga.parse_cables(output), ["USB-Blaster [USB-0]", "USB-BlasterII [USB-1]"])
        for failure in ("Error when scanning hardware - Server error", "Error (213009): Unable to access JTAG hardware",
                        "No JTAG hardware available", "Operation failed"):
            with self.subTest(failure=failure), self.assertRaises(fpga.FpgaError):
                fpga.parse_cables(output + failure)
        with self.assertRaises(fpga.FpgaError):
            fpga.parse_cables("Info: no cables listed\n")

    def test_chain_requires_selected_single_matching_device(self):
        good = "1) USB-Blaster [USB-0]\n  031050DD   10M50DA\n"
        self.assertIsNone(fpga.verify_chain(good, "USB-Blaster [USB-0]"))
        self.assertIsNone(fpga.verify_chain(good + "2) Other cable\n  031020DD   10M02\n", "USB-Blaster [USB-0]"))
        for output, cable in ((good, "Missing"), (good.replace("031050DD", "031020DD"), "USB-Blaster [USB-0]"),
                              (good.replace("10M50DA", "Different"), "USB-Blaster [USB-0]"),
                              (good + "  031050DD  10M50DA\n", "USB-Blaster [USB-0]"),
                              (good + "  !Error\n", "USB-Blaster [USB-0]")):
            with self.subTest(output=output, cable=cable), self.assertRaises(fpga.FpgaError):
                fpga.verify_chain(output, cable)

    def test_program_success_requires_actual_operation_confirmation(self):
        success = "Info (209011): Successfully performed operation(s)"
        self.assertTrue(fpga.programmer_success(success))
        for output in ("Info: Quartus Programmer was successful. 0 errors", success + "\nOperation failed",
                       success + "\nError (1): failure"):
            with self.subTest(output=output), self.assertRaises(fpga.FpgaError):
                fpga.programmer_success(output)


class AutomaticSwitchSyncTest(unittest.TestCase):
    @staticmethod
    def module(hdl, name):
        match = re.search(rf'\bmodule\s+{name}\s*\(.*?\bendmodule\b', hdl, re.S)
        if not match:
            raise AssertionError(f'Module {name} is missing')
        return match[0]

    def assert_wrapper(self, source):
        hdl, qsf, sdc = fpga.compile_de10_lite(source)
        top = self.module(hdl, 'HYBRID01')
        self.assertEqual(len(re.findall(r'\bmodule\s+', hdl)), 2)
        self.assertRegex(top, r'\booc_sw0_meta\s*<=\s*SW0\s*;')
        self.assertRegex(top, r'\booc_sw0_sync\s*<=\s*ooc_sw0_meta\s*;')
        self.assertRegex(top, r'\balways\s*@\s*\(\s*posedge\s+MAX10_CLK1_50\s*\)')
        self.assertRegex(top, r'\booc_user_HYBRID01\s+ooc_logic\s*\(')
        self.assertRegex(top, r'\.SW0\s*\(\s*ooc_sw0_sync\s*\)')
        self.assertRegex(top, r'\.MAX10_CLK1_50\s*\(\s*MAX10_CLK1_50\s*\)')
        # The only physical SW0 read in top-level assignments enters stage one.
        raw_reads = re.findall(r'\b([A-Za-z_][A-Za-z0-9_]*)\s*(?:<=|=)\s*[^;]*\bSW0\b[^;]*;', top)
        self.assertEqual(raw_reads, ['ooc_sw0_meta'])
        self.assertIn('PRESERVE_REGISTER ON -to ooc_sw0_meta', qsf)
        self.assertIn('PRESERVE_REGISTER ON -to ooc_sw0_sync', qsf)
        self.assertIn('get_registers {ooc_sw0_meta}', sdc)
        self.assertIn('-period 20.000', sdc)
        self.assertIn('set_false_path -from [get_ports {SW0}] -to $first_stage', sdc)
        self.assertNotIn('set_false_path -from [get_ports {SW0}] -to [all_registers]', sdc)
        return hdl, qsf, sdc

    def test_direct_switch_output_without_clock_block_gets_two_stages(self):
        from tests.exporters.test_clocked import EmittedClock
        hdl, _, _ = self.assert_wrapper(DIRECT_SOURCE)
        sim = EmittedClock(self.module(hdl, 'HYBRID01'))
        self.assertEqual(sim.state['ooc_sw0_meta'], 0)
        self.assertEqual(sim.state['ooc_sw0_sync'], 0)
        samples = [sim.tick(SW0=signal) for signal in (1, 1, 0, 0)]
        self.assertEqual([state['ooc_sw0_meta'] for state in samples], [1, 1, 0, 0])
        self.assertEqual([state['ooc_sw0_sync'] for state in samples], [0, 1, 1, 0])

    def test_plain_verilog_export_is_not_changed_by_board_synchronization(self):
        before = export_verilog(DIRECT_SOURCE)
        hdl, _, _ = self.assert_wrapper(DIRECT_SOURCE)
        self.assertEqual(export_verilog(DIRECT_SOURCE), before)
        self.assertNotIn('ooc_sw0_meta', before)
        inner = self.module(hdl, 'ooc_user_HYBRID01')
        self.assertEqual(inner, before.strip().replace('module HYBRID01 (', 'module ooc_user_HYBRID01 (', 1))
        self.assertIn('assign LEDR0 = SW0;', inner)
        self.assertIn('assign AUDIO_OUT = !SW0;', inner)

    def test_exact_legacy_two_stage_pattern_is_preserved_without_extra_latency(self):
        hdl, qsf, sdc = fpga.compile_de10_lite(LEGACY_SOURCE)
        self.assertEqual(hdl.strip(), export_verilog(LEGACY_SOURCE).strip())
        self.assertEqual(qsf, fpga.QSF)
        self.assertEqual(sdc, fpga.SDC)
        self.assertEqual(len(re.findall(r'\bmodule\s+', hdl)), 1)
        self.assertNotIn('ooc_sw0_meta', hdl)

    def test_explicit_legacy_initial_values_are_preserved(self):
        from tests.exporters.test_clocked import EmittedClock
        for meta, sync in ((0, 1), (1, 0), (1, 1)):
            source = LEGACY_SOURCE.replace('sw_meta;', f'sw_meta = {meta};', 1).replace('sw_sync;', f'sw_sync = {sync};', 1)
            with self.subTest(meta=meta, sync=sync):
                hdl, qsf, sdc = fpga.compile_de10_lite(source)
                self.assertEqual(hdl.strip(), export_verilog(source).strip())
                self.assertEqual((qsf, sdc), (fpga.QSF, fpga.SDC))
                sim = EmittedClock(hdl)
                self.assertEqual(sim.state, {'sw_meta': meta, 'sw_sync': sync})
                self.assertEqual(sim.tick(SW0=0), {'sw_meta': 0, 'sw_sync': meta})

    def test_custom_manual_names_are_isolated_behind_auto_sync(self):
        source = LEGACY_SOURCE.replace('sw_meta', 'stage_one').replace('sw_sync', 'stage_two')
        self.assert_wrapper(source)

    def test_incomplete_or_unsafe_legacy_sync_patterns_are_rejected(self):
        for source in (
            LEGACY_SOURCE.replace('sw_meta', 'stage_one'),
            LEGACY_SOURCE.replace('sw_meta = SW0;', 'if (sw_sync) { sw_meta = SW0; }'),
            LEGACY_SOURCE.replace('LEDR0 = sw_sync;', 'LEDR0 = SW0;'),
            LEGACY_SOURCE.replace('LEDR0 = sw_sync;', 'LEDR0 = sw_meta;'),
            LEGACY_SOURCE.replace('sw_meta = SW0;', 'sw_meta = SW0; sw_meta = 0;'),
        ):
            with self.subTest(source=source), self.assertRaises(fpga.FpgaError):
                fpga.compile_de10_lite(source)

    def test_generated_names_and_sw0_substrings_do_not_corrupt_user_module(self):
        source = DIRECT_SOURCE.replace('LEDR0 = SW0;', '''
            SW0x;
            ooc_sw0_meta;
            ooc_sw0_sync;
            ooc_user_HYBRID01;
            ooc_logic;
            LEDR0 = SW0 ^ SW0x ^ ooc_sw0_meta ^ ooc_sw0_sync ^ ooc_user_HYBRID01 ^ ooc_logic;''')
        hdl, _, _ = self.assert_wrapper(source)
        expected = export_verilog(source).strip().replace('module HYBRID01 (', 'module ooc_user_HYBRID01 (', 1)
        self.assertEqual(self.module(hdl, 'ooc_user_HYBRID01'), expected)
        # A similarly named state is not a raw switch read or an alias for it.
        legacy = LEGACY_SOURCE.replace('sw_meta;', 'SW0x; sw_meta;', 1).replace('LEDR0 = sw_sync;', 'LEDR0 = sw_sync ^ SW0x;')
        self.assertEqual(fpga.compile_de10_lite(legacy)[0].strip(), export_verilog(legacy).strip())

    def test_automatic_mode_still_rejects_wrong_clock_or_unannotated_clock(self):
        for header in ('clock(MAX10_CLK1_50)', 'clock(MAX10_CLK1_50,25MHz)', 'clock(SW0,50MHz)'):
            source = DIRECT_SOURCE.replace('LEDR0 = SW0;', f'flag; {header} {{ flag=!flag; }} LEDR0=flag;')
            with self.subTest(header=header), self.assertRaises(fpga.FpgaError):
                fpga.compile_de10_lite(source)

    def test_prepared_auto_project_uses_generated_constraints_and_hdl(self):
        with tempfile.TemporaryDirectory() as directory:
            project = fpga.prepare_project(DIRECT_SOURCE, directory)
            hdl, qsf, sdc = fpga.compile_de10_lite(DIRECT_SOURCE)
            for name, expected in (('build/HYBRID01.v', hdl), ('HYBRID01.qsf', qsf), ('HYBRID01.sdc', sdc)):
                self.assertEqual((project.directory / name).read_text().strip(), expected.strip())


class TenSwitchProfileTest(unittest.TestCase):
    @staticmethod
    def locations(qsf):
        return dict((port, pin) for pin, port in re.findall(
            r'^set_location_assignment PIN_([A-Z0-9]+) -to ([A-Za-z0-9_]+)$', qsf, re.M))

    def test_all_ten_pairs_have_independent_board_locations_and_electrical_constraints(self):
        hdl, qsf, sdc = fpga.compile_de10_lite(switch_led_source())
        expected = {'MAX10_CLK1_50': 'P11'}
        expected.update({f'SW{i}': pin for i, pin in enumerate(SWITCH_PINS)})
        expected.update({f'LEDR{i}': pin for i, pin in enumerate(LED_PINS)})
        self.assertEqual(self.locations(qsf), expected)
        self.assertEqual(len(set(expected.values())), 21)
        standards = re.findall(r'^set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to (\w+)$', qsf, re.M)
        self.assertCountEqual(standards, expected)
        strengths = dict((port, value) for value, port in re.findall(
            r'^set_instance_assignment -name CURRENT_STRENGTH_NEW "(\d+MA)" -to (\w+)$', qsf, re.M))
        self.assertEqual(strengths, {f'LEDR{i}': '8MA' for i in range(10)})
        self.assertNotIn('AUDIO_OUT', hdl + qsf + sdc)
        self.assertIn('-period 20.000', sdc)
        top = AutomaticSwitchSyncTest.module(hdl, 'HYBRID01')
        for index in range(10):
            self.assertIn(f'ooc_sw{index}_meta <= SW{index};', top)
            self.assertIn(f'ooc_sw{index}_sync <= ooc_sw{index}_meta;', top)
            self.assertRegex(top, rf'\.SW{index}\s*\(ooc_sw{index}_sync\)')
            self.assertIn(f'PRESERVE_REGISTER ON -to ooc_sw{index}_meta', qsf)
            self.assertIn(f'PRESERVE_REGISTER ON -to ooc_sw{index}_sync', qsf)
        # Every asynchronous input has its own first-stage-only exception.
        self.assertEqual(len(re.findall(r'set_false_path -from \[get_ports \{SW\d\}\]', sdc)), 10)
        for index in range(10):
            pattern = (rf'set (\w+) \[get_registers \{{ooc_sw{index}_meta\}}\]'
                       rf'.*?set_false_path -from \[get_ports \{{SW{index}\}}\] -to \$\1\b')
            self.assertRegex(sdc.replace('\n', ' '), pattern)
        self.assertNotRegex(sdc, r'set_false_path[^\n]*(?:_sync|all_registers|all_clocks)')

    def test_all_1024_switch_patterns_reach_corresponding_led_after_two_edges(self):
        from tests.exporters.test_clocked import EmittedClock
        source = switch_led_source()
        original = export_verilog(source)
        hdl, _, _ = fpga.compile_de10_lite(source)
        self.assertEqual(export_verilog(source), original)
        top = AutomaticSwitchSyncTest.module(hdl, 'HYBRID01')
        inner = AutomaticSwitchSyncTest.module(hdl, 'ooc_user_HYBRID01')
        self.assertEqual(inner, original.strip().replace('module HYBRID01 (', 'module ooc_user_HYBRID01 (', 1))
        wiring = re.findall(r'assign LEDR(\d+) = SW(\d+);', inner)
        self.assertCountEqual(wiring, [(str(i), str(i)) for i in range(10)])
        sim = EmittedClock(top)
        self.assertEqual(len(sim.state), 20)
        previous = 0
        for pattern in range(1024):
            inputs = {f'SW{i}': (pattern >> i) & 1 for i in range(10)}
            first = sim.tick(**inputs)
            self.assertEqual(sum(first[f'ooc_sw{i}_sync'] << i for i in range(10)), previous)
            second = sim.tick(**inputs)
            actual_leds = sum(second[f'ooc_sw{switch}_sync'] << int(led) for led, switch in wiring)
            self.assertEqual(actual_leds, pattern)
            previous = pattern

    def test_sw9_subset_and_optional_audio_do_not_assign_unused_board_pins(self):
        for audio in (False, True):
            with self.subTest(audio=audio):
                hdl, qsf, sdc = fpga.compile_de10_lite(switch_led_source([9], audio=audio))
                expected = {'MAX10_CLK1_50': 'P11', 'SW9': 'F15', 'LEDR9': 'B11'}
                if audio:
                    expected['AUDIO_OUT'] = 'V10'
                    self.assertIn('CURRENT_STRENGTH_NEW "4MA" -to AUDIO_OUT', qsf)
                self.assertEqual(self.locations(qsf), expected)
                self.assertNotRegex(hdl + qsf + sdc, r'\bSW0\b|\bLEDR0\b|\booc_sw0_')
                self.assertIn('ooc_sw9_meta', hdl + sdc)

    def test_unknown_ports_wrong_directions_or_missing_required_roles_are_rejected(self):
        for source in (
            switch_led_source([10]),
            switch_led_source([9]).replace('LEDR9', 'LEDR10'),
            'chip HYBRID01(MAX10_CLK1_50,LEDR0) { MAX10_CLK1_50=input; LEDR0=output; LEDR0=0; }',
            'chip HYBRID01(MAX10_CLK1_50,SW0) { MAX10_CLK1_50,SW0=input; }',
            'chip HYBRID01(MAX10_CLK1_50,SW0,LEDR0,AUDIO_OUT) { MAX10_CLK1_50,SW0,LEDR0=input; AUDIO_OUT=output; AUDIO_OUT=LEDR0; }',
            'chip HYBRID01(MAX10_CLK1_50,SW0,SW1,LEDR0) { MAX10_CLK1_50,SW0=input; SW1,LEDR0=output; SW1=SW0; LEDR0=SW0; }',
            'chip HYBRID01(MAX10_CLK1_50,SW0,LEDR0) { SW0=input; MAX10_CLK1_50,LEDR0=output; MAX10_CLK1_50=0; LEDR0=SW0; }',
        ):
            with self.subTest(source=source), self.assertRaises(fpga.FpgaError):
                fpga.compile_de10_lite(source)

    def test_mixed_legacy_sw0_and_extra_switches_are_explicitly_rejected(self):
        extra_switches = ', '.join(f'SW{i}' for i in range(1, 10))
        extra_leds = ', '.join(f'LEDR{i}' for i in range(1, 10))
        source = LEGACY_SOURCE.replace('AUDIO_OUT) {', f'AUDIO_OUT, {extra_switches}, {extra_leds}) {{')
        source = source.replace('sw_meta;', f'{extra_switches} = input; {extra_leds} = output; sw_meta;', 1)
        source = source.replace('AUDIO_OUT = !sw_sync;', 'AUDIO_OUT = !sw_sync;\n' + '\n'.join(f'LEDR{i}=SW{i};' for i in range(1, 10)))
        with self.assertRaises(fpga.FpgaError):
            fpga.compile_de10_lite(source)

    def test_manual_sw0_with_extra_outputs_retains_original_two_stages(self):
        extra_leds = ', '.join(f'LEDR{i}' for i in range(1, 10))
        source = LEGACY_SOURCE.replace('AUDIO_OUT) {', f'AUDIO_OUT, {extra_leds}) {{')
        source = source.replace('sw_meta;', f'{extra_leds} = output; sw_meta;', 1)
        source = source.replace('AUDIO_OUT = !sw_sync;', 'AUDIO_OUT = !sw_sync;\n' + '\n'.join(f'LEDR{i}=sw_sync;' for i in range(1, 10)))
        hdl, qsf, sdc = fpga.compile_de10_lite(source)
        self.assertEqual(hdl.strip(), export_verilog(source).strip())
        self.assertNotIn('ooc_sw0_meta', hdl)
        expected = {'MAX10_CLK1_50': 'P11', 'SW0': 'C10', 'AUDIO_OUT': 'V10'}
        expected.update({f'LEDR{i}': pin for i, pin in enumerate(LED_PINS)})
        self.assertEqual(self.locations(qsf), expected)
        self.assertIn('get_registers {sw_meta}', sdc)


class ClocklessProfileTest(unittest.TestCase):
    def test_clockless_profile_has_direct_logic_and_no_clock_or_registers(self):
        hdl, qsf, sdc = fpga.compile_de10_lite(CLOCKLESS_SOURCE)
        self.assertEqual(hdl.strip(), export_verilog(CLOCKLESS_SOURCE).strip())
        self.assertNotRegex(hdl, r'\b(reg|always|initial)\b')
        self.assertNotIn('MAX10_CLK1_50', hdl + qsf + sdc)
        self.assertNotIn('create_clock', sdc)
        self.assertNotIn('set_false_path', sdc)
        self.assertNotIn('SYNCHRONIZER', qsf)
        self.assertIn('set_max_delay 20.000 -from [get_ports {SW0}] -to [get_ports {LEDR0 AUDIO_OUT}]', sdc)
        self.assertIn('set_min_delay 0.000', sdc)
        self.assertEqual(TenSwitchProfileTest.locations(qsf), {'SW0': 'C10', 'LEDR0': 'A8', 'AUDIO_OUT': 'V10'})

    def test_clockless_profile_rejects_storage_and_switch_driven_clock(self):
        for source in (
            CLOCKLESS_SOURCE.replace('LEDR0 = SW0;', 'held; LEDR0 = held;'),
            CLOCKLESS_SOURCE.replace('LEDR0 = SW0;', 'held; clock(SW0,50MHz) { held = !held; } LEDR0 = held;'),
        ):
            with self.subTest(source=source), self.assertRaises(fpga.FpgaError):
                fpga.compile_de10_lite(source)

    def test_clockless_assembly_remains_combinational(self):
        source = '''chip Invert(a, y) { a = input; y = output; y = !a; }
        main HYBRID01 { SW9 = input; LEDR9 = output; Invert(SW9, LEDR9); }'''
        hdl, qsf, sdc = fpga.compile_de10_lite(source)
        self.assertNotIn('MAX10_CLK1_50', hdl + qsf + sdc)
        self.assertNotIn('posedge', hdl)
        self.assertEqual(TenSwitchProfileTest.locations(qsf), {'SW9': 'F15', 'LEDR9': 'B11'})


if __name__ == "__main__":
    unittest.main()
