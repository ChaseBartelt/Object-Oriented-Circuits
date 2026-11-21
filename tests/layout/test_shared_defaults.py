# Isolate settings and published defaults so these tests do not replace personal layouts.
# Selection cases move component bodies and terminal/label attachments as one coherent group.
import json
import math
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QSettings, QRect
from PySide6.QtWidgets import QApplication
from circuitlab.layout.devices import DeviceLayouts
from circuitlab.layout.gates import GateLayouts, default_gate_layout
from circuitlab.layout.geometry import Layout
from circuitlab.layout.editing import move_symbols
from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.routing import fast_layout
from circuitlab.language.compiler import compile_circuit
from circuitlab.math.report import component_labels
from circuitlab.viewer.schematic import SchematicView, point


class SharedDefaultsTest(unittest.TestCase):
    # Qt permits one QApplication per process; reuse it across this test class.
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_publish_load_on_clean_install_and_personal_override(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / 'defaults.json'
            with patch('circuitlab.layout.gates.BUNDLED_LAYOUTS', path):
                store = DeviceLayouts(QSettings(str(Path(tmp)/'author.ini'), QSettings.Format.IniFormat))
                layout = default_gate_layout('nand', 2)
                store.set(('nand', 2), layout)
                self.assertEqual(store.publish(path), 1)
                fresh = DeviceLayouts(QSettings(str(Path(tmp)/'fresh.ini'), QSettings.Format.IniFormat))
                self.assertEqual(fresh.get(('nand', 2)).positions, layout.positions)
                fresh.set(('nand', 2), None)
                self.assertIsNone(fresh.get(('nand', 2)))
                self.assertEqual(len(json.loads(path.read_text())['layouts']), 1)

    def test_box_selects_wire_ends_and_moves_them_with_parts(self):
        circuit = compile_circuit('R1(1k) between(a,b);', render_only=True)
        part = circuit.components[0]
        drawing = Layout(symbols=[(part,(0,0),(4,0),'+')], positions={'R1':(0,0)},
            boundary={'a':(-2,0),'b':(6,0)}, wires={'a':[((-2,0),(0,0))], 'b':[((4,0),(6,0))]})
        view = SchematicView()
        self.addCleanup(view.deleteLater)
        view.resize(800,400); view.show(); view.draw(circuit, layout=drawing)
        view.select_box(view.viewport().rect())
        self.assertEqual(view.selected_symbols, {'R1',('boundary','a'),('boundary','b')})
        moved = move_symbols(circuit, drawing, view.selected_symbols, (1,2))
        self.assertIsNotNone(moved)
        self.assertEqual(moved.boundary, {'a':(-1,2),'b':(7,2)})
        self.assertTrue(preserves_nets(circuit,moved))
        partial = move_symbols(circuit, drawing, {'R1',('boundary','a')}, (0,1))
        self.assertIsNotNone(partial)
        self.assertEqual(partial.boundary['b'], drawing.boundary['b'])
        self.assertTrue(preserves_nets(circuit,partial))
        start = view.mapFromScene(point(drawing.boundary['a']))
        view.start_drag('boundary', 'a', (None,'auto'), start)
        self.assertEqual(view.drag.selection, frozenset(view.selected_symbols))
        end = start + view.mapFromScene(point((0,1))) - view.mapFromScene(point((0,0)))
        view.preview_drag(end)
        self.assertFalse(view.drag.blocked)
        self.assertNotEqual(view.layout.boundary, drawing.boundary)
        outline, = view.selection_items
        self.assertTrue(all(outline.rect().contains(point(pos)) for pos in view.layout.boundary.values()))
        self.assertTrue(preserves_nets(circuit,view.layout))
        view.drag = None
        view.close()

    def test_selection_outline_follows_internal_wires_without_enclosing_fixed_trunks(self):
        circuit = compile_circuit(
            "R1(1k) between(a,mid);\nR2(1k) between(mid,out);\nR3(1k) between(out,b);",
            render_only=True,
        )
        symbols = [(part, (x, 0), (x + 4, 0), "+") for part, x in zip(circuit.components, (0, 8, 26))]
        drawing = Layout(
            symbols=symbols, positions={part.name: a for part, a, _, _ in symbols}, pin_grid=.25,
            wires={
                "mid": [((4, 0), (4, 6)), ((4, 6), (8, 6)), ((8, 6), (8, 0))],
                "out": [((12, 0), (15, 0)), ((15, 0), (15, -3)), ((15, -3), (24, -3)),
                        ((24, -3), (24, 0)), ((24, 0), (26, 0))],
            },
        )
        view = SchematicView()
        self.addCleanup(view.deleteLater)
        view.resize(900, 430)
        view.show()
        view.draw(circuit, layout=drawing)
        key = (None, "auto")
        view.selection_key = key
        view.selected_symbols = {"R1", "R2"}
        view.paint_selection()
        outline, = view.selection_items
        self.assertTrue(outline.rect().contains(point((4, 6))))
        self.assertTrue(outline.rect().contains(point((15, 0))))
        self.assertFalse(outline.rect().contains(point((24, -3))))

        start = view.mapFromScene(point((2, 0)))
        view.start_drag("component", "R1", key, start, control=True)
        view.preview_drag(view.mapFromScene(point((2, 2))))
        self.assertFalse(view.drag.blocked)
        self.assertIsNot(view.layout, drawing)
        outline, = view.selection_items
        for a, b in view.layout.wires["mid"]:
            self.assertTrue(outline.rect().contains(point(a)))
            self.assertTrue(outline.rect().contains(point(b)))
        self.assertFalse(outline.rect().contains(point((24, -3))))
        self.assertTrue(preserves_nets(circuit, view.layout))
        view.drag = None
        view.close()

    def test_move_ground_and_split_connector_ends_with_or_without_component(self):
        circuit = compile_circuit('R1(1k) between(a,0);', render_only=True)
        part = circuit.components[0]
        drawing = Layout(symbols=[(part,(0,0),(4,0),'+')], positions={'R1':(0,0)},
            grounds=[(6,0)], connectors=[('a',(-2,0),'a',True,False)],
            wires={'a':[((-2,0),(0,0))], '0':[((4,0),(6,0))]})
        ends = {('ground',(6,0)), ('connector',0)}
        for selected in (ends, ends | {'R1'}):
            moved = move_symbols(circuit, drawing, selected, (0,1))
            self.assertIsNotNone(moved)
            self.assertEqual(moved.grounds, [(6,1)])
            self.assertEqual(moved.connectors[0][1], (-2,1))
            self.assertTrue(preserves_nets(circuit,moved))

    def test_initial_shortening_never_increases_wire_length(self):
        for name in ('NE555','SN74HC00','LM358'):
            store = DeviceLayouts(QSettings('unused', 'layout-test'))
            _,_,circuit,boundary = store.preview(name.casefold())
            labels = component_labels(circuit)
            before = fast_layout(circuit,labels,boundary=boundary,shorten=False)
            after = fast_layout(circuit,labels,boundary=boundary)
            length = lambda layout: sum(math.dist(a,b) for edges in layout.wires.values() for a,b in edges)
            self.assertLessEqual(length(after),length(before)+1e-8)
            self.assertTrue(preserves_nets(circuit,after))
