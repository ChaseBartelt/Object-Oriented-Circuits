"""Verify stock libDaisy AudioPassthru from reset through circular DMA playback."""

import argparse
from pathlib import Path
import runpy
import sys


ROOT = Path(__file__).resolve().parents[6]
EXAMPLE = ROOT / "examples/advanced/CortexM7"
ELF = ROOT / "tests/fixtures/digital/libdaisy_v8_0_0_audio_passthru.elf"
ENTRY = EXAMPLE / "DaisySeed_AudioPassthru.ckt"
BUFFER_FRAMES = 96  # Two 48-frame libDaisy DMA halves.
VERIFY_FRAMES = BUFFER_FRAMES + 14


def verify(circuit, max_cycles):
    sys.path.insert(0, str(ROOT))
    from circuitlab.viewer.window import prepare_digital_preview

    load_elf = runpy.run_path(str(EXAMPLE / "firmware/stm32h750/build_firmware.py"))["load_elf"]
    symbols = load_elf(ELF).symbols
    callback = symbols["_Z13AudioCallbackPKPKfPPfj"] & ~1
    vector_handlers = {
        27: symbols["DMA1_Stream0_IRQHandler"] & ~1,
        28: symbols["DMA1_Stream1_IRQHandler"] & ~1,
    }
    source = circuit.read_text(encoding="utf-8")
    session, layout, sim, error = prepare_digital_preview(source, "auto", "auto", circuit)
    if error or session.issues or layout is None or sim is None:
        raise RuntimeError(error or str(session.issues) or "Could not prepare the viewer.")
    print(f"Loaded {len(sim.signals)} viewer signals. Simulating from reset.", flush=True)

    ready = set()
    seen_vectors = set()
    callback_cycle = None
    recorded, played = [], []
    while sim.cycles < max_cycles:
        values = sim.values
        pc, exception = values["PC"], values["IPSR"]
        if values["FAULT"] or values["CFSR"] or values["HFSR"]:
            raise RuntimeError(
                f"Cycle {sim.cycles}: PC={pc:#010x}, IR={values['IR']:#010x}, "
                f"CFSR={values['CFSR']:#010x}, HFSR={values['HFSR']:#010x}, BFAR={values['BFAR']:#010x}"
            )
        for flag in ("QSPI_READY", "SDRAM_READY"):
            if values[flag] and flag not in ready:
                ready.add(flag)
                print(f"Cycle {sim.cycles}: {flag}", flush=True)
        if vector_handlers.get(exception) == pc and exception not in seen_vectors:
            seen_vectors.add(exception)
            print(f"Cycle {sim.cycles}: DMA vector {exception} entered {pc:#010x}", flush=True)
        if pc == callback and callback_cycle is None:
            callback_cycle = sim.cycles
            print(f"Cycle {sim.cycles}: stock AudioCallback entered {pc:#010x}", flush=True)
        if values["AUDIO_FRAMES"] > len(recorded):
            recorded.append((values["AUDIO_IN_LEFT"], values["AUDIO_IN_RIGHT"]))
            played.append((values["AUDIO_OUT_LEFT"], values["AUDIO_OUT_RIGHT"]))
            if len(recorded) == VERIFY_FRAMES:
                break
        if sim.cycles % 4096 == 0:
            print(f"Cycle {sim.cycles}: PC={pc:#010x}, frames={len(recorded)}", flush=True)
        sim.step()

    if (not values["QSPI_READY"] or not values["SDRAM_READY"]
            or seen_vectors != set(vector_handlers) or callback_cycle is None):
        raise RuntimeError(f"Missing startup or callback events: ready={sorted(ready)}, "
                           f"DMA vectors={sorted(seen_vectors)}, callback cycle={callback_cycle}.")
    if len(played) != VERIFY_FRAMES:
        raise RuntimeError(f"Only {len(played)} audio frames reached in {max_cycles} cycles.")
    if any(left or right for left, right in played[:BUFFER_FRAMES]):
        raise RuntimeError("Playback started before DMA returned to the first buffer half.")
    expected = recorded[:VERIFY_FRAMES - BUFFER_FRAMES]
    has_stereo = (any(left for left, _ in expected) and any(right for _, right in expected)
                  and any(left != right for left, right in expected))
    if played[BUFFER_FRAMES:] != expected or not has_stereo:
        raise RuntimeError(f"Playback did not match the delayed stereo input. "
                           f"Expected {expected}, received {played[BUFFER_FRAMES:]}.")
    print(f"Matched {len(expected)} stereo frames after the {BUFFER_FRAMES}-frame DMA buffer delay.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("circuit", type=Path, nargs="?", default=ENTRY,
                        help="modular viewer circuit for the bundled AudioPassthru ELF")
    parser.add_argument("--max-cycles", type=int, default=200_000,
                        help="stop after this many simulated cycles")
    args = parser.parse_args()
    if args.max_cycles < 1:
        parser.error("--max-cycles must be positive.")
    try:
        verify(args.circuit.resolve(), args.max_cycles)
    except (OSError, RuntimeError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
