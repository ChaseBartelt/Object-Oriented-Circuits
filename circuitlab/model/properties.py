"""Canonical property names and component categories for editor tooling.

Category entries describe the proposed vocabulary. MODEL_PROPERTIES lists
only implemented properties, so completion clients need not suggest models
that the compiler/simulator cannot evaluate yet.
"""

from dataclasses import dataclass


COMPONENT_CATEGORIES = {
    "opamp": ("gain", "gbw", "slew", "offset", "common", "output", "supply"),
    "comparator": ("offset", "delay", "response", "common", "output", "supply"),
    "audio": ("gain", "bandwidth", "output", "supply", "distortion"),
    "gate": ("delay", "timing", "input", "output"),
    "flipflop": ("delay", "timing", "clock", "reset"),
    "resistor": ("resistance", "tolerance", "temperature"),
    "capacitor": ("capacitance", "voltage", "leakage"),
}


@dataclass(frozen=True)
class ModelProperty:
    category: str
    unit: str
    description: str
    models: tuple[str, ...] = ("opamp",)


MODEL_PROPERTIES = {
    "gain": ModelProperty("gain", "", "Open-loop DC voltage gain; default ideal (unlimited)."),
    "bandwidth": ModelProperty("gbw", "Hz", "Gain-bandwidth product in Hz, for the dominant-pole model; default ideal."),
    "gbw": ModelProperty("gbw", "Hz", "Gain-bandwidth product in Hz; an alias for bandwidth."),
    "slew": ModelProperty("slew", "V/s", "Maximum output slope, e.g. 2.5V/us; default ideal (unlimited)."),
    "offset": ModelProperty("offset", "V", "Input-referred voltage added to in+; default 0 V.", ("opamp", "comparator")),
    "delay.rise": ModelProperty("delay", "s", "Inertial latency before a comparator collector opens/high; overrides response.time for this direction. Choose a simulation step smaller than the latency.", ("comparator",)),
    "delay.fall": ModelProperty("delay", "s", "Inertial latency before a comparator collector sinks/low; overrides response.time for this direction.", ("comparator",)),
    "response.time": ModelProperty("response", "s", "Comparator latency used for either direction without an explicit delay.rise/delay.fall; default 0 s. Shorter pulses are cancelled.", ("comparator",)),
    "output.low.at4mA": ModelProperty("output", "V", "Comparator low voltage at 4 mA; sets on-resistance to this voltage / 0.004 A. Requires an external collector pull-up.", ("comparator",)),
    "supply.current": ModelProperty("supply", "A", "Constant current from V+ to V-; default 0 A for op amps/comparators, 4 mA for eight-pin audio. Requires both supply pins.", ("opamp", "audio", "comparator")),
    "supply.current.total": ModelProperty("supply", "A", "Shared chip bias current, allocated once across its powered amplifier channels; explicit channel currents are retained.", ("chip",)),
    "common.min": ModelProperty("common", "V", "Minimum input common-mode voltage; a voltage or rail expression such as vminus + 3V. Default unrestricted.", ("opamp", "comparator")),
    "common.max": ModelProperty("common", "V", "Maximum input common-mode voltage; a voltage or rail expression such as vplus. Default unrestricted.", ("opamp", "comparator")),
    "common.rejection": ModelProperty("common", "dB", "Voltage CMRR in dB: 100dB gives a 1e-5 input-referred common-mode coefficient. Default ideal rejection."),
    "common.impedance": ModelProperty("common", "ohm", "Resistance from each input to the output return, e.g. 1G; default ideal (infinite)."),
    "output.min": ModelProperty("output", "V", "Minimum op-amp output voltage: a constant or rail expression such as vminus + 5mV; default unlimited."),
    "output.max": ModelProperty("output", "V", "Maximum op-amp output voltage: a constant or rail expression such as vplus - 1.5V; default unlimited."),
    "gain.default": ModelProperty("gain", "", "Eight-pin audio amplifier gain with open gain pins; default 20.", ("audio",)),
    "gain.max": ModelProperty("gain", "", "Eight-pin audio amplifier gain with shorted/bypassed gain pins; default 200.", ("audio",)),
    "gain.resistor": ModelProperty("gain", "ohm", "Internal audio gain-network resistance; default 1.35k ohms.", ("audio",)),
    "output.bias": ModelProperty("output", "", "Audio output bias as a fraction of V+ minus V-; default 0.5.", ("audio",)),
    "output.type": ModelProperty("output", "", "Output model: open_collector for comparators (external pull-up), power_amplifier for eight-pin audio.", ("audio", "comparator")),
    "bypass.resistance": ModelProperty("output", "ohm", "Behavioral audio bias feed resistance; default 50k ohms.", ("audio",)),
}
