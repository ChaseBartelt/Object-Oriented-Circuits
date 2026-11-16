# Compact circuit lab

Ten small, editable demonstrations of the current language. Open a numbered
`.ckt` file in VS Code and press its OOC play button. Start with **07** for a
ringing percussion waveform, **01** for phase cancellation, or **08** for a DAC
generated from a few lines. These are new examples; existing circuits are untouched.

| File | What it does | Try this |
| --- | --- | --- |
| [01 Quadrature notch](01_quadrature_notch.ckt) | Two all-pass cells create phase-shifted copies; blending dry and wet creates a notch near 995 Hz. | In AC Results inspect `phase90`, `phase180`, `notch`. Change `f`; tune both R/C pairs together. |
| [02 Octave resonators](02_octave_resonators.ckt) | Three buffered LC bands near 503, 1007 and 2013 Hz feed an averaging mixer. | Probe `band0`, `band1`, `band2`, `out`. Lower the loss resistors for sharper peaks. |
| [03 Pulse envelope](03_pulse_envelope.ckt) | A transistor resets a capacitor; it rises exponentially between pulses. | Results: `envelope`, **20 us** step, Run. Inspect about **24 ms**; `RCharge` controls rise time. |
| [04 Schmitt clock bursts](04_schmitt_oscillator.ckt) | An RC loop around a CD40106B channel makes a clock; a periodic clamp resets it. | Results: `out` or `timing`, **20 us** step, about **8 ms**. Increase R or C to slow it. The reset repeats every **10 ms**. |
| [05 Majority vote](05_majority_vote.ckt) | Four NAND calls vote high when at least two of three inputs are high. | Change `VA`, `VB`, `VC` to 0 or 5; solve `vote`. Default is high. |
| [06 Enabled mux](06_enabled_mux_digital.ckt) | A one-bit combinational multiplexer with enable and complementary output. | Use **OOC: Check Circuit**; export through the viewer's **File > Export Verilog**. This is the digital example. |
| [07 Metal ping](07_metal_ping.ckt) | Short pulses strike two damped modes near 3.39 and 7.34 kHz, then mix them. | Results: `out`, **2 us** step, about **4 ms**. Change the pulse period for rhythm and the [mode components](patches/metal_modes.ckt) for timbre. |
| [08 Code DAC](08_code_dac.ckt) | Compile-time loops generate a binary-weighted DAC around a bundled TL072. | Change `word` to 0..63, solve `out`. Default 45 gives **3.515625 V**. |
| [09 Passive wah](09_passive_wah.ckt) | A series input feeds a damped parallel LC tank with resonance near 1.59 kHz. | AC Results: `tone`. Change `f`, or vary the tank C to move the band. |
| [10 Slow gate](10_slow_gate.ckt) | Override the built-in inverter with a capacitive output for asymmetric edges. | Results: `out`, **10 us** step, about **12 ms**. Change `CNode` or the pull-up to reshape the edge. |

For time plots, keep analysis on **Auto/DC** and use **Results > Run**, not the
separate first-order **Transient** analysis option. Enter the numeric microsecond
step in the viewer's Time step field. No initial capacitor override is required.
Compile / Solve shows the DC starting point; the waveform appears when you run.
The viewer plots one selected node at a time. These examples do not play audio.

## Shortcuts to borrow

- **Typed component arguments:** `phase(R(16k), C(10n), input, output)` and
  `mode(L1(22m), C1(100n), R1(150), hit, bell0)` reuse a topology without repeated wiring.
- **Generated names and values:** `for`, `while`, `do while`, `if / else if`,
  integer arithmetic and `${...}` emit components in 02, 07 and 08.
  They run at compilation; they do not inspect live voltages or implement clocked logic.
- **Built-in devices:** `op1(plus, minus, out)`, `transQ1(base, collector, emitter, npn)`
  and gate calls avoid verbose device blocks. 05 omits gate ground; its three-input
  NAND uses grouped inputs to make the pin order unambiguous.
- **Short library includes:** `include("TL072");`, `include("CD40106B");`.
- **Source shorthand:** `VCC = 12 : wire = positive;` in 08.
  `pulse(low, high, delay, rise, fall, high_time, period)` drives 03, 07 and 10.
- **Cross-file patching:** 07 and `patches/metal_modes.ckt` share only the names
  declared by `connector(hit, bell0, bell1);`. Open **07**, not its helper, to run the patch.
- **Implicit wiring:** 09 uses `series` and `parallel` definitions with values alone.
- **Custom gate implementation:** 10 uses `@Override`; its `not(...)` call stays compact.
- **Digital pins and logic:** 06 groups `input`/`output` declarations and uses `!`,
  `&` and `|`. It exports combinational Verilog, not an analog electrical simulation.

For the DAC, `bits` controls the generated width (try 1..12); valid words are
0 through `2^bits - 1`. The branches clamp an out-of-range word to that range.
The ideal output is `5 * word / 2^bits` volts. The default six-bit circuit has
78.125 mV steps and a maximum of 4.921875 V.

## Verification and model limits

All nine electrical entry files were compiled, numerically exercised, and exported
to SPICE. Export was checked locally; no external SPICE simulator was run.

- 01 and 02 match independent complex transfer calculations at five frequencies
  each (maximum absolute difference below `5.4e-15`). At its exact center,
  01's ideal cancellation falls below `1e-12 V`; this is not real-world rejection.
- 03 reaches roughly **0.015..3.87 V** during a 24 ms native run.
- 04 produces approximately **2.38 kHz**, **0.095..4.67 V**, in an 8 ms native run.
  Its startup clamp allows the DC solve to converge and releases by 110 us;
  it resets again every 10 ms, giving repeated clock bursts on longer runs.
  The library expands all six Schmitt channels (131 parts including the clamp),
  so it is slower than the other examples. Its discrete model does not predict
  manufacturer timing accurately. For SPICE waveform comparison set a maximum
  timestep of 20 us; the automatically chosen step is coarser.
- 05 passes all **8** input combinations; nominal LOW is about **0.232 V**, HIGH about **5 V**.
- 06 compiles and exports Verilog; evaluating its emitted one-bit expressions
  passes all **16** input combinations. No HDL simulator or FPGA hardware was used.
- 07's 4 ms native run gives about **-0.722..0.339 V** at `out`; the first strike
  decays below **4 mV** late in its two-millisecond interval.
- 08 matches the ideal DAC equation for codes 0, 1, 31, 32, 45 and 63, including
  out-of-range clamp checks.
- 09 matches its independent complex impedance calculation at five frequencies
  (maximum difference below `3.4e-16`); default `tone` is about **0.909 V RMS**.
- 10 repeats both rising and falling logic transitions in a 12 ms native run,
  spanning about **0.027..5.00 V**. The RC rising edge is slower than the transistor reset.

Op amps, including the TL072 wrapper, are ideal: supply limits, noise, slew rate
and bandwidth are omitted. BJTs and logic gates use the project's approximate
native models. Passive parts have no tolerance or parasitics. The circuits are
language/simulation examples, not tested physical builds.
