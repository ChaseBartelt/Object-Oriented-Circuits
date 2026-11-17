"""Select a chip's diagram signals using compiler/runtime identity mappings."""

import re
from types import SimpleNamespace

from circuitlab.math.digital import DigitalSimulation
from circuitlab.language.pin_banks import expand_pin_banks


def chip_diagram_model(circuit, index, simulation=None):
    """Return display metadata and display-name -> live-signal bindings.

    A stand-alone preview compiles once for structure only; it never runs a
    second clock or presents its initial values as a live simulation.
    """
    instance = circuit.instances[index]
    if simulation is not None and len(circuit.instances) == 1 and not instance.signal_bindings:
        return simulation, {name: name for name in simulation.signals}

    if simulation is None or not instance.signal_bindings:
        pins = ', '.join(pin for pin, _ in instance.pins)
        body = '\n'.join(row if row.endswith(('{', '}')) else row + ';' for row in instance.body)
        model = DigitalSimulation(f'chip {instance.name}({pins}) {{\n{body}\n}}')
        model.values = {}
        return model, {}

    bindings = {local: simulation.source_bindings[global_name]
                for local, global_name in instance.signal_bindings
                if global_name in simulation.source_bindings
                and simulation.source_bindings[global_name] in simulation.signals}
    # Generated tick counters are not source declarations. Include their real
    # dependencies, but never pull a neighbouring chip's private state inside.
    owned = {simulation.source_bindings[name]
             for method in circuit.instances for _, name in method.signal_bindings
             if name in simulation.source_bindings}
    reverse = {live: local for local, live in bindings.items()}
    pending = list(reverse)
    while pending:
        for live in simulation.dependencies.get(pending.pop(), ()):
            if live in reverse or live in owned or live not in simulation.signals:
                continue
            local = live.rsplit('.', 1)[-1]
            while local in bindings:
                local = '_' + local
            bindings[local], reverse[live] = live, local
            pending.append(live)

    def rename(text):
        return re.sub(r'\b[A-Za-z_][\w.]*', lambda match: reverse.get(match[0], match[0]), text)

    ports = {'input': [], 'output': []}
    for row in instance.body:
        if match := re.fullmatch(r'(.+?)\s*=\s*(input|output)', row):
            pins = expand_pin_banks(match[1], 1)
            ports[match[2]].extend(name.partition('[')[0].strip() for name in pins.split(','))
    clocks = tuple(reverse[name] for name in simulation.clocks if name in reverse)
    model = SimpleNamespace(
        name=instance.name,
        signals={name: simulation.signals[live] for name, live in bindings.items()},
        inputs=tuple(name for name in ports['input'] if name in bindings and name not in clocks),
        outputs=tuple(name for name in ports['output'] if name in bindings),
        clocks=clocks, frequency=simulation.frequency, values={},
        dependencies={name: tuple(reverse[read] for read in simulation.dependencies.get(live, ())
                                  if read in reverse) for name, live in bindings.items()},
        signal_kinds={name: simulation.signal_kinds.get(live, 'wire') for name, live in bindings.items()},
        signal_clocks={name: reverse.get(clock, clock) for name, live in bindings.items()
                       if (clock := simulation.signal_clocks.get(live))},
        signal_expressions={name: rename(simulation.signal_expressions.get(live, ''))
                            for name, live in bindings.items()},
    )
    return model, bindings
