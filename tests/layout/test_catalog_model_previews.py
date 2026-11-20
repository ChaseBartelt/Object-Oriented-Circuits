"""Bundled behavioral models must remain usable in the layout catalog."""

import unittest

from circuitlab.layout.checks import preserves_nets
from circuitlab.layout.devices import DeviceLayouts, device_preview, library_devices
from circuitlab.layout.routing import fast_layout
from circuitlab.math.report import component_labels


class CatalogModelPreviewTest(unittest.TestCase):
    def test_every_declared_device_compiles_and_routes_its_actual_preview(self):
        # Preview compilation is read-only and does not access persistence
        # settings. Enumerate declarations so adding a chip extends coverage
        # without depending on a hardcoded catalog size or a hand-picked list.
        layouts = DeviceLayouts(None)
        devices = library_devices()
        self.assertTrue(devices)
        self.assertEqual(len(layouts.devices), len(devices))
        for device in devices:
            with self.subTest(device=device.name, source=device.file):
                circuit, root, view, boundary = layouts.preview(device.key)
                instance = circuit.instances[0]
                self.assertEqual(instance.name, device.name)
                self.assertEqual(tuple(pin for pin, _ in instance.pins), device.pins)
                self.assertEqual(tuple(net for _, net in instance.pins), device.arguments)
                if root is None:
                    self.assertFalse(instance.model_available)
                    self.assertIsNone(view)
                    continue
                self.assertTrue(instance.model_available)
                self.assertIsNotNone(view)
                drawing = fast_layout(view, component_labels(view), boundary=boundary, shorten=False)
                self.assertTrue(preserves_nets(view, drawing))

    def test_reported_failures_preserve_original_package_pin_order(self):
        expected = {
            "lm311": ("emitter", "plus", "minus", "vminus", "balance", "strobe", "collector", "vplus"),
            "lm339": ("out1", "out2", "vplus", "minus2", "plus2", "minus1", "plus1", "minus3",
                      "plus3", "minus4", "plus4", "vminus", "out4", "out3"),
            "lm358": ("out1", "minus1", "plus1", "vminus", "plus2", "minus2", "out2", "vplus"),
            "lm393": ("out1", "minus1", "plus1", "vminus", "plus2", "minus2", "out2", "vplus"),
        }
        devices = {device.key: device for device in library_devices()}
        for key, pins in expected.items():
            with self.subTest(device=key):
                self.assertEqual(devices[key].pins, pins)
                circuit, root, view, _ = device_preview(devices[key])
                self.assertIsNotNone(root)
                self.assertIsNotNone(view)
                self.assertEqual(tuple(pin for pin, _ in circuit.instances[0].pins), pins)
