"""A chip-level bias specification must be drawn once across its channels."""

import unittest

from circuitlab.exporters.spice import export_spice
from circuitlab.language.compiler import compile_circuit
from circuitlab.math.linear import NodalBackend
from circuitlab.math.nonlinear import TimeDomain
from circuitlab.model.graph import CircuitError


CHIP = """chip Dual(a,b,oa,ob,p,n) {
    op1(a,oa,oa,p,n);
    op2(b,ob,ob,p,n);
    analog { supply.current.total=0.8mA; }
}
"""
MAIN = """Vsignal(1V) between(input,0);
Vpower(5V) between(pos,0);
main {Dual(input,input,out1,out2,pos,0);}
"""


class ChipSupplyTotalTest(unittest.TestCase):
    def test_each_backend_draws_the_total_once_and_balances_power(self):
        circuit = compile_circuit(CHIP+MAIN)
        for network in (NodalBackend().solve(circuit,ac=False),TimeDomain(circuit).operating_point()):
            self.assertAlmostEqual(network.branch_current[network.names.index("Vpower")],-.0008)
            self.assertAlmostEqual(sum(network.branch_power),0)
        currents = [p.opamp.supply_current for p in circuit.components if p.kind == "O"]
        self.assertEqual(currents,[.0004,.0004])

    def test_explicit_channel_current_is_retained_and_remainder_is_allocated(self):
        source = CHIP.replace("    analog {", "    analog op1 {supply.current=0.1mA;}\n    analog {")+MAIN
        circuit = compile_circuit(source)
        currents = [p.opamp.supply_current for p in circuit.components if p.kind == "O"]
        self.assertAlmostEqual(currents[0],.0001)
        self.assertAlmostEqual(currents[1],.0007)

    def test_chip_override_changes_the_total_without_changing_connections(self):
        patch = "@override\nchip Dual {analog {supply.current.total=1mA;}}\n"
        original = compile_circuit(CHIP+MAIN)
        changed = compile_circuit(CHIP+MAIN+patch)
        self.assertEqual(original.instances,changed.instances)
        self.assertEqual([p.terminal_nets for p in original.components],[p.terminal_nets for p in changed.components])
        self.assertEqual([p.opamp.supply_current for p in changed.components if p.kind == "O"],[.0005,.0005])

    def test_repeated_instances_have_separate_totals(self):
        source = CHIP+MAIN.replace("pos,0);}","pos,0); Dual(input,input,out3,out4,pos,0);}")
        circuit = compile_circuit(source)
        network = NodalBackend().solve(circuit,ac=False)
        self.assertAlmostEqual(network.branch_current[network.names.index("Vpower")],-.0016)

    def test_inconsistent_or_unpowered_totals_are_diagnostics(self):
        cases = (CHIP.replace("0.8mA","-1mA")+MAIN,
                 CHIP.replace("    analog {","    analog op1 {supply.current=1mA;}\n    analog {")+MAIN,
                 CHIP.replace("op1(a,oa,oa,p,n)","op1(a,oa,oa)")+MAIN)
        for source in cases:
            with self.subTest(source=source),self.assertRaises(CircuitError):
                compile_circuit(source)

    def test_shared_draw_is_preserved_by_spice_export(self):
        netlist = export_spice(CHIP+MAIN)
        self.assertEqual(netlist.count("Iquiescent vplus vminus DC 0.0004"),1)
        self.assertEqual(sum(line.startswith("X_op") and line.endswith(" EC_OpAmp1")
                             for line in netlist.splitlines()),2)

    def test_non_amplifier_total_is_explicitly_incomplete_in_a_preview(self):
        source = "chip Passive(a,b){R1(1k) between(a,b); analog{supply.current.total=1mA;}}\nmain{Passive(input,0);}"
        with self.assertRaisesRegex(CircuitError,"supply-load model"):
            compile_circuit(source)
        preview = compile_circuit(source,render_only=True)
        self.assertTrue(preview.model_issues)
        with self.assertRaisesRegex(CircuitError,"supply-load model"):
            preview.require_models()
