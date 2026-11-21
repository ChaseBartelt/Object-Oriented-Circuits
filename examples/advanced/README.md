# Advanced OOC test circuits

Open any `.ckt` file in VS Code and use **OOC: Run Circuit / Open Schematic** (the play button). Each file includes its controls, probe names, expected behavior, and model assumptions in the opening comments.

[OCTANT](octant/README.md) combines an eight-channel electrical scanner with an FPGA alert controller and six-digit telemetry display. The [OOC processor](octant/octant_cpu.ckt) runs firmware that reads, logs, and acknowledges the scanner's alerts. You can single-step its registers in Logic simulation.

## Five FPGA designs

Each file is an independent `HYBRID01` design for the DE10-Lite's 50 MHz clock. Open **Logic simulation**, select **DE10-Lite switches**, and press **Run**. This profile inserts the board's switch synchronizers automatically. Most timed designs remain still until enabled with the switches below.

| File | What it exercises | Start here |
| --- | --- | --- |
| [01_dual_voice_synth.ckt](fpga/01_dual_voice_synth.ckt) | Two reusable DDS voices; four notes; solo, XOR and AND mixing; tremolo; mute | All switches down plays A3 digitally on `AUDIO_OUT`. `SW1:SW0` selects voice A's note; `SW3:SW2` selects B's. |
| [02_pwm_engine.ckt](fpga/02_pwm_engine.ckt) | Two 24.414 kHz PWM channels; four duty settings; complementary output; breathing ramp | Raise `SW8` and `SW9` to enable the channels. `SW4` enables breathing; `SW7` pauses it. |
| [03_reaction_timer.ckt](fpga/03_reaction_timer.ckt) | State machine; false-start detection; 1-second wait; saturating 10 ms counter | With `SW0/SW1` low, hold `SW9` high for 20 ms, then lower it. Raise `SW0`; when LED9 lights, raise `SW1`. |
| [04_switch_alu.ckt](fpga/04_switch_alu.ckt) | Registered 4-bit add/subtract/XOR/compare; carry/borrow, zero and comparison flags | `SW3:SW0` is A, `SW7:SW4` is B, `SW9:SW8` chooses the operation. |
| [05_pattern_sequencer.ckt](fpga/05_pattern_sequencer.ckt) | Chase, bounce and Gray-code patterns; pause, reverse, speed and reset | Raise `SW0` to run. `SW1` doubles speed; `SW2` reverses; `SW3` selects bounce; `SW4` selects Gray code. |

Use **Step** to inspect individual clock cycles and select an internal signal in **Probe** to see its history. A physical switch passes through two synchronizer stages; a user register consuming that switch responds on the third rising edge. Synchronization does not debounce switches or make multiple switches change atomically.

For hardware, use **FPGA... → Build from OOC** on the file you want. These examples have been checked with the OOC compiler and native simulator; they have not been synthesized in Quartus or tested on a board. DDS notes have finite 32-bit frequency resolution. `AUDIO_OUT` is a digital waveform, not a headphone driver. PWM complement mode has no dead time and is not a power-stage controller.

## Five analog schematics

Leave analysis on **Auto**, then use **Compile / Solve** and **Results**. The schematic remains available for inspecting and arranging the circuit. AC values below are RMS phasors for the supplied 1 Vrms input.

| File | Circuit | Expected check |
| --- | --- | --- |
| [01_instrumentation_bridge.ckt](schematics/01_instrumentation_bridge.ckt) | Resistive bridge, bundled TL072 front end and difference amplifier | Bridge difference: 24.752475 mV; gain: 110; `out`: 2.722772277 V DC. |
| [02_state_variable_filter.ckt](schematics/02_state_variable_filter.ckt) | Feedback filter with high-pass, band-pass, low-pass and notch outputs | At 1591.549 Hz: `hp = +j`, `bp = -1`, `lp = -j`, `notch ≈ 0` V. |
| [03_active_crossover.ckt](schematics/03_active_crossover.ckt) | Buffered two-pole paths, complementary three-band split and reconstruction | At 1 kHz: `low + mid + high = reconstructed = 1` V. Change `f` to compare bands. |
| [04_impedance_equalizer.ckt](schematics/04_impedance_equalizer.ckt) | Complex-Z quadrature stage, inverse equalizer, dry/wet mix and null detector | `quadrature = -2j`, `equalized = 1`, `wetMix = -1+j`, `null ≈ 0` V. |
| [05_transistor_multivibrator.ckt](schematics/05_transistor_multivibrator.ckt) | Asymmetric four-BJT oscillator, emitter-follower outputs and RC monitor | In Results: probe `c1`, step **5 us**, initial capacitor **C1**, initial voltage **1 V**; run for at least **10 ms**. Also inspect `c2`, `out1`, `out2`, and `monitor`. |

The op amps, including the bundled TL072 channels, are ideal models without rail clipping, bandwidth, offset or slew limits. The transistor circuit uses generic nonlinear BJTs, so its period differs from the simple RC estimate. The state-variable filter is intended for AC analysis; its ideal integrators do not provide a DC startup path. The Z equalizer is theoretical AC-only: it cannot be run as a transient or exported to SPICE/LTspice.
