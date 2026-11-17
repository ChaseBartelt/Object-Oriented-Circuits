"""Independent shared layouts for bundled chips and their internal circuits."""

import json
from dataclasses import dataclass, replace

from circuitlab.language.compiler import compile_circuit
from circuitlab.language.linked import CHIP_LIBRARY
from circuitlab.layout.gates import GateLayouts, rebind_layout
from circuitlab.layout.hierarchy import METHOD_PREFIX, hood_levels
from circuitlab.layout.persistence import restore_layout
from circuitlab.tooling.source_info import methods


@dataclass(frozen=True)
class LibraryDevice:
    name: str
    kind: str
    file: str
    pins: tuple[str, ...]

    @property
    def key(self):
        return self.name.casefold()

    @property
    def category(self):
        return self.file.split("/")[0]

    @property
    def arguments(self):
        # A ground reference of 0 matches ordinary use and avoids adding an
        # extra implicit global-ground terminal to nested ideal-op-amp blocks.
        return tuple("0" if pin == "gnd" else pin for pin in self.pins)

    @property
    def example(self):
        return f'include("@chips/{self.file}");\nmain {{ {self.name}({", ".join(self.arguments)}); }}'


def library_devices():
    """Read real declarations; include-only alias files are not extra devices."""
    return tuple(
        LibraryDevice(name, kind, path.relative_to(CHIP_LIBRARY).as_posix(),
                      tuple(pin.strip() for pin in pins.split(",") if pin.strip()))
        for path in sorted(CHIP_LIBRARY.rglob("*.ckt"))
        for kind, name, pins in methods(path.read_text(encoding="utf-8-sig")).values()
    )


def device_preview(device):
    """Instantiate distinct signal ports, retaining nested package identities."""
    circuit = compile_circuit(device.example, render_only=True)
    instance = circuit.instances[0]
    # A top-level `circuit` normally flattens. Isolate it for layout editing just
    # as it appears when called inside a chip, without changing its definition.
    if instance.kind == "circuit":
        circuit = replace(circuit, instances=[
            replace(instance, kind="chip"), *circuit.instances[1:],
        ])
    if not instance.model_available:
        return circuit, None, None, ()
    root = METHOD_PREFIX + "0"
    _, view, _, boundary = next(row for row in hood_levels(circuit, {root}) if row[0] == root)
    return circuit, root, view, boundary


class DeviceLayouts(GateLayouts):
    """Keep existing gate keys intact and add one separate key per declaration."""

    def __init__(self, settings):
        super().__init__(settings)
        self.reload_library()

    def reload_library(self):
        """Pick up edited definitions when reopening the layout browser."""
        self.devices = {device.key: device for device in library_devices()}
        self.previews = {}
        self.cache = {key: layout for key, layout in self.cache.items() if not isinstance(key, str)}

    @staticmethod
    def path(key, group="v1"):
        if isinstance(key, str):
            return f"device-layouts/{group}/{key.casefold()}"
        return GateLayouts.path(key, group)

    def preview(self, key):
        if key not in self.previews:
            self.previews[key] = device_preview(self.devices[key])
        return self.previews[key]

    def saved(self, key):
        if not isinstance(key, str):
            return super().saved(key)
        raw = self.raw(key)
        if not raw:
            return None
        try:
            _, _, view, boundary = self.preview(key)
            return restore_layout(json.loads(raw), view, boundary) if view is not None else None
        except (TypeError, ValueError, KeyError):
            return None

    def for_instance(self, instance, view, boundary, *, use_default=True):
        if instance.builtin_gate:
            return super().for_instance(instance, view, boundary, use_default=use_default)
        key = instance.name.casefold()
        if key not in self.devices or not instance.model_available:
            return None
        layout = self.get(key)
        if layout is None:
            return None
        prototype, _, template, _ = self.preview(key)
        try:
            return rebind_layout(
                layout, template, view, instance.pins, boundary,
                template_pins=prototype.instances[0].pins,
            )
        except (KeyError, ValueError):
            # An override or a newer definition can have a different topology.
            # The saved design remains available, but is never forced onto it.
            return None
