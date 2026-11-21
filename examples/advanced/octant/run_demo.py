"""Sample OCTANT's electrical trip lines into its FPGA alert controller."""

from dataclasses import replace
from pathlib import Path

from circuitlab.exporters.clocked import SEVEN_SEGMENTS
from circuitlab.language.compiler import compile_circuit
from circuitlab.language.syntax import SWITCH_ON_RESISTANCE
from circuitlab.math.digital import DigitalSimulation
from circuitlab.math.nonlinear import TimeDomain


PROJECT = Path(__file__).resolve().parent
CHANNELS = 8
LOGIC_THRESHOLD = 2.5
ACK_TIME = 0.020
END_TIME = 0.028


def show_telemetry(controller):
    panel = DigitalSimulation((PROJECT / "digital_blocks.ckt").read_text(encoding="utf-8"))
    for name in panel.inputs:
        if name != "PAGE":
            panel.set_input(name, controller.values[name])
    glyphs = {code & 127: f"{digit:X}" for digit, code in enumerate(SEVEN_SEGMENTS)}
    glyphs[127] = "-"
    print("\nTelemetry at the end of the run (left to right)")
    for page, label in enumerate(("active / first / pending / stable", "last event / event count")):
        panel.set_input("PAGE", page)
        digits = [panel.values[f"DIGIT{i}"] for i in reversed(range(6))]
        display = "".join(glyphs[code & 127] + ("" if code & 128 else ".") for code in digits)
        print(f"  {display}   {label}")


def main():
    controller = DigitalSimulation((PROJECT / "octant_controller.ckt").read_text(encoding="utf-8"))
    step = 1 / controller.frequency
    ack_sample = round(ACK_TIME / step)

    path = PROJECT / "octant.ckt"
    circuit = compile_circuit(path.read_text(encoding="utf-8"), path=path)
    # Use automatic scan for this run without editing the saved switch position.
    parts = [
        replace(part, closed=True, value=SWITCH_ON_RESISTANCE) if part.name == "Sscan" else part
        for part in circuit.components
    ]
    analog = TimeDomain(replace(circuit, components=parts), step)
    index = {node: i for i, node in enumerate(analog.nodes)}
    trip_pins = [index[f"trip{channel}"] for channel in range(CHANNELS)]
    select_pins = [index[f"select{bit}"] for bit in range(3)]

    controller.set_input("RESET", 1)
    controller.step()
    controller.set_input("RESET", 0)
    previous = None
    print("Time (ms)   Scan   Monitor (V)   Average (V)   Trip   Pending   First   Events")
    for sample in range(1, round(END_TIME / step) + 1):
        voltage = analog.advance()
        trip = [int(voltage[pin] >= LOGIC_THRESHOLD) for pin in trip_pins]
        mask = sum(bit << i for i, bit in enumerate(trip))
        for channel, bit in enumerate(trip):
            name = f"TRIP{channel}"
            if controller.values[name] != bit:
                controller.set_input(name, bit)
        if sample == ack_sample:
            controller.set_input("ACK", (1 << CHANNELS) - 1)
        values = controller.step()
        if sample == ack_sample:
            controller.set_input("ACK", 0)

        scan = sum(int(voltage[pin] >= LOGIC_THRESHOLD) << i for i, pin in enumerate(select_pins))
        state = scan, mask, values["PENDING"], values["EVENT_COUNT"]
        if state != previous:
            first = str(values["FIRST"]) if values["VALID"] else "-"
            print(
                f"{sample * step * 1e3:9.2f}   {scan:4d}   "
                f"{voltage[index['monitor']]:11.3f}   {voltage[index['average']]:11.3f}   "
                f"  {mask:02X}        {values['PENDING']:02X}   {first:>5}   {values['EVENT_COUNT']:6d}"
            )
            previous = state
    show_telemetry(controller)


if __name__ == "__main__":
    main()
