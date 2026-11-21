"""Clock OCTANT's OOC CPU with trip signals from the electrical simulator."""

from dataclasses import replace
from pathlib import Path

from circuitlab.language.compiler import compile_circuit
from circuitlab.language.syntax import SWITCH_ON_RESISTANCE
from circuitlab.math.digital import DigitalSimulation
from circuitlab.math.nonlinear import TimeDomain


PROJECT = Path(__file__).resolve().parent
LOGIC_THRESHOLD = 2.5
DEMO_DURATION = 0.0285


def electrical_scan(step):
    """Run the existing schematic with automatic scan selected for this session."""
    source = PROJECT / "octant.ckt"
    circuit = compile_circuit(source.read_text(encoding="utf-8"), path=source)
    parts = [
        replace(part, closed=True, value=SWITCH_ON_RESISTANCE) if part.name == "Sscan" else part
        for part in circuit.components
    ]
    return TimeDomain(replace(circuit, components=parts), step)


def logic_mask(voltage, pins):
    return sum(int(voltage[pin].real >= LOGIC_THRESHOLD) << bit for bit, pin in enumerate(pins))


def main():
    source = PROJECT / "octant_cpu.ckt"
    cpu = DigitalSimulation(source.read_text(encoding="utf-8"))
    step = 1 / cpu.frequency
    analog = electrical_scan(step)
    nodes = {name: index for index, name in enumerate(analog.nodes)}
    trip_pins = tuple(nodes[f"trip{i}"] for i in range(8))
    select_pins = tuple(nodes[f"select{i}"] for i in range(3))

    cpu.set_input("RESET", 1)
    values = cpu.step()
    cpu.set_input("RESET", 0)
    previous = None
    print("Time (ms)   Scan    Trip   Pending   ACK   Events   Batches     Last PC     Last IR")
    for cycle in range(1, round(DEMO_DURATION / step) + 1):
        voltage = analog.advance()
        trips = logic_mask(voltage, trip_pins)
        for channel in range(len(trip_pins)):
            pin = f"TRIP{channel}"
            level = (trips >> channel) & 1
            if cpu.values[pin] != level:
                cpu.set_input(pin, level)
        # An ACK belongs to the store being retired on this edge.
        ack = cpu.values["ACK"]
        values = cpu.step()

        scan = logic_mask(voltage, select_pins)
        pending = values["PENDING"]
        events, batches = values["EVENT_COUNT"], values["R4"]
        state = scan, trips, pending, ack, events, batches
        stopping = values["HALTED"] or values["FAULT"]
        if state != previous or stopping:
            print(
                f"{cycle * step * 1e3:9.2f}   {scan:4d}      {trips:02X}        "
                f"{pending:02X}    {ack:02X}   {events:6d}   {batches:7d}   "
                f"{values['LAST_PC']:08X}    {values['LAST_IR']:08X}"
            )
            previous = state
        if stopping:
            break

    print(
        f"\nFirmware logged {values['R4']} batches. "
        f"Latest acknowledgment {values['LAST_ACK']:02X}, last recorded hardware count {values['R5']}."
    )
    registers = "  ".join(f"R{i}={values[f'R{i}']:08X}" for i in range(8))
    print(f"Registers  {registers}")
    log = "  ".join(
        f"[{slot}] {values[f'LOG_MASK{slot}']:02X}/{values[f'LOG_COUNT{slot}']}" for slot in range(4)
    )
    print(f"RAM log (mask/count)  {log}")
    if values["FAULT"]:
        raise SystemExit(f"CPU fault at PC {values['PC']:08X}, instruction {values['IR']:08X}.")
    print("CPU halted." if values["HALTED"] else "CPU polling its memory-mapped event input.")


if __name__ == "__main__":
    main()
