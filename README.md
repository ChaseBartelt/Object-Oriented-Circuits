# Object-Oriented Circuits

OOC is a .ckt circuit language with a compiler, schematic viewer and simulation tools. Write in VS Code, save to refresh the viewer, and keep connections in source. Electrical analysis and digital FPGA compilation are separate workflows.

## Quick start

Installed Windows releases bundle Python, dependencies, the viewer, chip library and VS Code extension. VS Code itself is installed separately.

For source development, use Python 3.10 or newer from the checkout root:

    py -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -e .
    .\.venv\Scripts\python.exe ide/vscode/package_extension.py ide/vscode/dist/ooc-language.vsix

Install the generated VSIX from ide/vscode/dist using **Extensions: Install from VSIX**. Select the checkout's Python interpreter if needed. Open your own .ckt file and use **OOC: Run Circuit / Open Schematic** or the editor play button. Save source to update the same viewer. Restart the viewer after changing Python implementation files.

Headless commands and the optional desktop editor:

    .\.venv\Scripts\python.exe -m circuitlab.tooling.cli check path\to\circuit.ckt
    .\.venv\Scripts\python.exe -m circuitlab.tooling.cli solve path\to\circuit.ckt
    .\.venv\Scripts\python.exe -m circuitlab path\to\circuit.ckt
    .\.venv\Scripts\python.exe -m circuitlab --editor

On macOS/Linux use python3 and .venv/bin/python. Solve is for electrical designs; digital designs use Check, Logic simulation or Verilog export. Test designs are in examples/advanced. Keep your own circuits outside the application source tree.

## Diodes

Use `D1` for a standard diode or a typed name such as `D.rect1`,
`D.Tun1`, `D.Schot1`, `D.Zener1` or `D.LED1`. These are ordinary
components in `main`, `circuit`, `chip`, series/parallel groups and included
files. The wires in `between(anode, cathode)` are ordinary node names;
**the first wire is the anode and the second is the cathode** (the symbol's bar).

```ooc
V1(12V) between(supply, 0);
main {
    R1(1k) between(supply, regulated);
    D.Zener1(5.1V) between(0, regulated);
    R2(1k) between(supply, light);
    D.LED1(2V) between(light, 0);
}
```

Omit parentheses to use generic room-temperature defaults: `D.rect1 between(signal, 0);`.
Parentheses are needed only when specifying values or parameters. Empty `()` remains accepted for compatibility. A bare voltage sets
Zener/avalanche breakdown, or the other diodes' forward drop **at 1 mA in the PN branch** (photo/tunnel currents are added separately).
It is not a fixed voltage drop. Optional named parameters are `is` (A), `n`,
`rs` (ohms), `bv` (V), `ibv` (A), `cj`/`cjo` (F), `vj` (V), `m`,
`light` (photocurrent, A), and tunnel `ip` (A), `vp` (V):
`D.Schot1(is=100nA, n=1.05, rs=0.2ohm) between(signal, 0);`.

Also supported: `D.PIN1`, `D.Varactor1`/`D.Varicap1`, `D.Photo1`,
`D.Avalanche1`/`D.TVS1`. The native solver supports DC and oscilloscope
waveforms, with junction capacitance and tunnel negative resistance.
Small-signal AC/port analysis uses SPICE export, as with the existing BJTs.
These models do not include temperature variation, reverse-recovery charge,
PIN carrier-storage/RF behavior, optical LED power, or avalanche noise.
Use measured/model parameters for a particular real device. The current and
capacitance parameters follow the [SPICE diode convention](https://ngspice.sourceforge.io/docs/ngspice-manual.pdf);
the tunnel current uses a generic peak-current/peak-voltage term
([model reference](https://web.mit.edu/cbegroup/www/InternalUse/Thesis/SueYoungMEngThesis.pdf)).
See [diodes.ckt](examples/diodes.ckt) for a runnable example.

## Daily workflow

Use `//` for a line comment and `/* ... */` for a block comment. Existing `#` comments and saved schematic metadata are still supported. Use `floor(a / b)` for floor division in mathematical behavior.

Manual switches use `S1(open) between(a, b);` or `S1(closed) between(a, b);`. Inside `series(...)` or `parallel(...)`, the group supplies the connections, so write `Sbutton(closed);` and `R2(10k);`. The assignment forms `Sbutton = closed;` and `R2 = 10k;` also work. Click the switch contacts to toggle the source state and recalculate. **Undo** restores the previous state. `on`/`off` and `1`/`0` also work, and an omitted state defaults to open. The solver and exports model the selected state as 1 mΩ closed or 1 TΩ open.

Inside a battery block, `Vneg = 5;` declares a −5 V supply on its first wire relative to its return. `Vneg = -5;` gives the same result. Use either `V` or `Vneg` once per battery block.

Electrical `main` and reusable methods can generate numbered wire names with independent counters:

```ooc
main {
    wi = count;
    aux = count;
    R1(1k) between(input, wi*);    # input -> wi1
    R2(1k) between(wi^, wi*);      # wi1 -> wi2
    op1(wi^, wi*, wi^);            # plus=wi2, minus=wi3, out=wi3
    Rload(10k) between(wi^, 0);
    Rbranch(10k) between(input, aux*); # independent counter: aux1
}
```

`wi = count;` starts at zero. `wi*` advances the counter and emits the next wire name. `wi^` reuses its current wire name, including `wi0` before the first advance. Connections expand left to right, and only executed branches and loop iterations advance counters. Counters are local to their `main` or method invocation and start fresh for each call. They can be read with `${wi}` but cannot be assigned directly or updated with `wi++`. The suffix forms apply to connection fields. Arithmetic expressions keep their ordinary multiplication and XOR operators.

Use the explicit forms if the wire prefix differs from the counter name: `w*i` advances `i`, and `w^i` or `w$i` reads it. When `w` itself is not a declared counter, `w^` reuses the last counter associated with prefix `w`. See [the complete wire-count example](examples/wire_counts.ckt).

Op amps use one call syntax, with the model selected by argument count:

| Arguments | Call | Model |
| --- | --- | --- |
| 3 | `op1(plus, minus, out);` | Ideal op amp |
| 5 | `op1(plus, minus, out, vplus, vminus);` | Op amp with supply contacts |
| 8 | `op1(plus, minus, out, vplus, vminus, gain1, gain8, bypass);` | Audio amplifier with gain and bypass contacts |

The first five arguments keep the same meaning in every form. Three- and five-argument calls and an optional `opamp op1;` declaration use ideal defaults: unlimited gain, bandwidth and slew, zero input offset, and zero supply current. Multiline pin lists are accepted.

Override any subset of those defaults before or after the connection call, including inside an electrical `chip` or `circuit` method:

```text
opamp op1 {
    gain = 19e6;
    bandwidth = 7.2MHz;
    slew = 2.5V/us;
    offset = 20uV;
    supply.current = 2.75mA;
}
op1(plus, minus, out, vplus, vminus);
```

`gain` is open-loop DC voltage gain. `bandwidth` is the gain-bandwidth product of a single dominant pole: `1/A(f) = 1/gain + j*f/bandwidth`. `offset` is added to the non-inverting input; it affects DC bias, not the AC small-signal drive. `slew` limits output changes during time simulation. Positive `supply.current` draws that constant current from V+ to V− and requires both pins; output-load-dependent supply current is not included. Missing fields keep their defaults, and `ideal` explicitly restores unlimited gain/bandwidth/slew. Definitions and repeated method instances have independent settings. The bundled LT1677 uses the example overrides above; [the parameterized op-amp example](examples/opamp_parameters.ckt) connects it as an inverting amplifier.

By default this behavioral model has zero input current and output resistance. It does not clip to the supply rails or model trim pins, noise, or detailed silicon behavior. Optional common-mode settings below add input loading, finite rejection and operating-range checks. Explicit output bounds enable saturation. SPICE/LTspice export retains the configured gain, offset, pole, slew, input loading, CMRR and quiescent draw. A slew-only SPICE model uses a 1e15 Hz pole for otherwise unlimited bandwidth; the native time solver uses the ideal-bandwidth limit. A powered amplifier with a separate named output-return domain supports native analysis and SPICE netlist export; LTspice's five-pin opamp2 symbol cannot expose that sixth return contact. Restart an already open viewer after updating the implementation.

Mathematical behavior is optional inside existing electrical `chip`, `circuit`, and `main` blocks. Includes, method calls, parameter binding and the schematic viewer retain their existing roles. A math block never creates a visible schematic component. `analog op1 { ... }` configures the connected amplifier and can follow `op1(...)`; it changes the numerical model while retaining the same triangle and pins. Use plain `analog` and `digital`; the older `:math` suffix is accepted for compatibility but is unnecessary.

```text
analog V(out) = V(input) * 10;

analog {
    V(a) = V(input) * 2;
    V(b) = V(a) + 5V;
    V(out) = V(b) / 3;
}

chip Comparator(plus, minus, out) {
    digital {
        out = V(plus) > V(minus);
    }
}
```

Use either the single-equation or multi-equation form for a given output; assigning it twice is an error. `V(node)` reads relative to 0; `V(node, reference)` reads a differential voltage, and either form can be assigned. Equations are simultaneous and method-local internal nodes remain private. Analog math supports `+ - * / **`, comparisons, Boolean expressions, and `abs`, `sqrt`, `sin`, `cos`, `tan`, `exp`, `log` (natural), `log10`, `floor`, `ceil`, `pow`, `min`, `max`, `clamp(value,low,high)` and `ifelse(condition,yes,no)`. Trigonometric arguments use radians; `pi` and `e` are constants, and `t`/`time` is elapsed seconds during native time simulation. SI literals such as `5V`, `20uV`, `1ms` and `1kHz` are converted to base units. This is numerical scalar math, without dimensional equation checking or execution of arbitrary Python.

`digital { out = (a & b) | (c ^ d); }` defines Boolean logic. In electrical chips, bare digital names read Boolean levels using a 0.5 V threshold; voltage comparisons use the actual `V(...)` values. Boolean results drive 0 or 1 V, without a power-rail or hysteresis model. In existing digital HDL chips, keep the normal input/output/width declarations: the optional `digital` wrapper is removed before the existing digital compiler, preserving its vector, clock, arithmetic and Verilog behavior. Native DC/time simulation supports nonlinear mathematical equations; AC/port analysis supports affine analog equations. Mathematical definitions are exported as behavioral directives rather than schematic symbols; wider integer bitwise math and shifts use Verilog export. Try [electrical math](examples/math_behavior.ckt) and [digital math](examples/digital_math.ckt).

Analog blocks also support instance-local expression variables and state. Variables are algebraic definitions; forward references work, and instantaneous dependency cycles are errors. They create no circuit nodes or schematic symbols. Electrical feedback through a voltage equation remains simultaneous, including feedback into an integrator:

```ooc
analog {
    property tau = 1ms;
    property initial: V = 0V;
    error = V(input) - V(out);
    V(out) = integrate(error / tau, initial);
}
```

Standalone `property name = value;` and grouped `properties { name = value; }` declarations share the same scope, inferred units and override rules. An optional `: V`, other base SI unit, or `: int`/`: float`/`: bool` annotation validates the declared default and later overrides. Internal expressions can use `let name = expression;` as well as `name = expression;` (`let` distinguishes an internal variable from a digital output assignment).

| Stateful function | Native time behavior |
| --- | --- |
| `integrate(rate, initial)` | Backward-Euler integration, including implicit electrical feedback. Initial value defaults to zero; `integrate(rate, time)` also starts from zero. |
| `delay(value, duration)` | Interpolated transport delay with retained accepted samples. |
| `transition(condition, rise, fall)` | Boolean inertial latency: a pending change is cancelled if its input returns before the deadline. `delay(condition, rise, fall)` is an alias. |
| `square(phase, duty)` | Stateless 0/1 waveform using phase in cycles and duty from 0 to 1; combine with `integrate(frequency, time)` for an oscillator. |

Initial values and delay durations must be finite constant model expressions; delays must be nonnegative. The first observed input initializes delay/transition history immediately. Subsequent changes are sampled at accepted solver endpoints, so select a time step smaller than the timing you want to resolve. Newton trials and failed/adaptive steps do not double-advance state. Compatible value/time-step edits preserve accepted history; changing a transport-delay duration or state-operation structure requires Reset. DC holds integrators at their initial values and evaluates delays/transitions without propagation history. Native AC/port analysis and SPICE/LTspice export explicitly reject stateful equations; ordinary voltage-equation rendering and export remain supported. See [the stateful filter example](examples/stateful_analog.ckt) and the bundled VCO.

Apply shared settings with `analog (op1, op2, ...) { ... }`; `gbw` and `bandwidth` name the same property. Use `@override` for intentional changes:

```ooc
include("LM324");

@override
chip LM324 {
    analog (op1, op2, op3, op4) {
        gbw = 2MHz;
        slew = 1V/us;
    }
    @override
    analog op3 { offset = 5mV; }
}
```

A chip patch omits the original pin list and contains only behavior/settings. Its original connections and hierarchy remain unchanged. Ordinary declarations establish the base values; explicit overrides replace only the stated properties. Among overrides, fewer targets are more specific; at equal specificity a chip patch supersedes a local setting. Conflicting values at the same priority are errors, independent of file order. The same chip-patch form can replace electrical `analog`/`digital` equations; existing built-in gate overrides remain supported. See [the LM324 override example](examples/model_overrides.ckt).

The eight-argument audio form uses the same settings syntax:

```ooc
op1(plus, minus, out, vcc, gnd, gain1, gain8, bypass);
analog op1 {
    gain.default = 20;
    gain.max = 200;
    gain.resistor = 1.35k;
    output.bias = 0.5;
    supply.current = 4mA;
}
```

These are also the audio defaults. Open gain pins give `gain.default`; shorting/bypassing them approaches `gain.max`, and external resistance/capacitance participates in the gain network. The bypass contact receives the fractional supply bias through `bypass.resistance` (a configurable behavioral choice, default 50kΩ), so an external capacitor changes bias startup/ripple. The triangle exposes all eight contacts; parameters and internal equations remain hidden. The driver has zero input current/output resistance and no clipping, noise, or load-dependent rail draw. Gain/GBW/slew/offset settings belong to the three/five-pin op-amp model; the eight-pin model accepts the audio properties above, `bypass.resistance`, and `output.type = power_amplifier`. SPICE export retains the audio network; viewer LTspice export saves its eight-pin `.asy` companion. See [the audio example](examples/audio_amplifier.ckt). The bundled LM324 implements gain/GBW/slew/offset; its optional common-mode/output limits are documented in comments.

The bundled LM311, LM339 and LM393 now use native open-collector comparator models. Configure their existing `oocCollectorComparator` method with the same `analog target { ... }` syntax:

```ooc
analog oocCollectorComparator {
    offset = 2mV;
    response.time = 1.3us;
    delay.rise = 115ns;
    delay.fall = 165ns;
    output.type = open_collector;
    output.low.at4mA = 150mV;
}
```

`response.time` supplies either direction without an explicit delay; `delay.rise`/`delay.fall` override their own direction. Rise means the collector opens toward its external pull-up voltage, and fall means it starts sinking toward the emitter. Short pulses cancel pending transitions. LM311 supplies 115 ns rise and 165 ns fall; LM339/LM393 supply 1.3 µs response. Positive `offset` adds to the non-inverting input. The collector requires an external pull-up: low voltage is approximated by an on-resistance `output.low.at4mA / 0.004 A`, with finite off leakage. LM311 retains its separate emitter. Common-mode bounds and per-channel/shared supply-current settings participate in native DC/time simulation; the model does not simulate balance/strobe functions, transistor saturation curves or input bias currents.

The source transistor drawing, package pins and saved layouts stay intact; the comparator replaces its interior only in the numerical graph. Public output/supply measurements report the behavioral model, while probes into unsolved interior nodes/components explicitly report unavailable values. The first observed comparator level initializes immediately, DC has no propagation history, and time steps must resolve the requested latency. Native AC/port analysis and SPICE/LTspice export of configured comparator timing currently report explicit unsupported-analysis/export errors; unconfigured transistor circuits retain their existing behavior.

Layout previews validate electrical topology independently of numerical model completeness. Unsupported specifications or proposed equations are retained as source-located model issues: the catalog still renders and saves the wiring, displays an incomplete-model notice, and refuses simulation/export of that unfinished model. Independent supported equations remain visible to the compiler. Even a wholly unsupported math-only chip retains its package and editable boundary pins. Unknown fields are never silently used as numerical defaults. Proposed LM13700 current equations and PT2399 ADC/DAC/audio-memory behavior remain unsupported numerical models.

Op amps support hard output bounds in native DC/time simulation:

```ooc
analog op1 {
    output.min = vminus + 5mV;
    output.max = vplus - 1.5V;
}
```

Bounds may be constant voltages or affine rail expressions. Defaults remain unlimited. Saturation participates in the electrical equations and load currents; the actual output is retained as GBW/slew history, so it recovers without a hidden state winding up behind the limit. Native AC/port analysis of a bounded model requires bias-point linearization and currently reports an explicit unsupported-analysis error; SPICE export retains the bounded behavioral equation. Export syntax/algebra is tested locally, without an installed external SPICE engine.

A shared chip bias draw can be declared once:

```ooc
analog { supply.current.total = 0.7mA; }
```

The compiler retains explicit per-channel `supply.current` settings and divides the remaining total equally among powered amplifier/comparator channels whose current was not specified. Repeated chip instances get independent totals. Conflicting totals, overlapping total scopes, missing supplies, and totals without a supported numerical channel/load model are errors for simulation and notices for topology previews. AD633 dynamic specifications still need numerical model implementations.

Three/five-pin op amps also accept common-mode properties:

```ooc
analog op1 {
    common.min = vminus + 3V;
    common.max = vplus;
    common.rejection = 100dB;
    common.impedance = 1G;
}
```

Here `vplus` and `vminus` refer to that amplifier's connected rails. Bounds accept affine rail expressions or constant voltages. Native DC/time simulation reports an error if the average input voltage leaves the declared range; these are validity limits, not a clipping model. Defaults are unrestricted range, ideal rejection and infinite input impedance. CMRR uses the voltage convention `k = 10^(-dB/20)` and adds `k * Vcm` to the input difference, with `Vcm` the average input voltage relative to the output return. `common.impedance` is a resistance from **each** input to that return; its terminal currents and power appear in results. AC analysis includes CMRR/loading but needs a separate DC bias check for range validity. SPICE exports range bounds as comments and the CMRR/input shunts as active model equations/components. See [the common-mode example](examples/common_mode.ckt).

Custom chips define their own properties in a grouped block. Units are inferred from defaults; each instance receives independent values. Patches are resolved before equation compilation, so equations use the updated values regardless of source order.

```ooc
chip MyAmplifier(input, out, gnd) {
    analog {
        properties {
            gain = 10;
            offset = 2mV;
            output.max = 12V;
        }
        V(out, gnd) = min(
            gain * V(input, gnd) + offset,
            output.max
        );
    }
}

@override
chip MyAmplifier {
    analog {
        properties { gain = 20; output.max = 15V; }
    }
}
```

Custom values are finite real quantities or booleans. Dotted names are declared constants, not host-language object access. Override units must match the default; bare numbers use that property's inferred unit. Bare engineering prefixes supply scale, so use `1Gohm` to infer resistance and `1G` for a dimensionless magnitude. Properties can be shared by electrical `analog`/`digital` equations in the same chip or method; HDL chips continue to use their existing `const` syntax. Property metadata and equations add no schematic symbols. See [the custom-properties example](examples/custom_properties.ckt).

The proposed category map is recorded in `circuitlab/model/properties.py` alongside metadata for implemented fields, ready for editor tooling. Categories describe the vocabulary; they do not silently enable unimplemented physics. Unsupported fields remain errors.

| Component category | Proposed property categories |
| --- | --- |
| Op amp | gain, gbw, slew, offset, common, output, supply |
| Comparator | offset, delay, response, common, output, supply |
| Audio / power amplifier (eight-argument `op1`) | gain, bandwidth, output, supply, distortion |
| Gate | delay, timing, input, output |
| Flip-flop | delay, timing, clock, reset |
| Resistor | resistance, tolerance, temperature |
| Capacitor | capacitance, voltage, leakage |

- **Electrical circuits:** compile/solve, inspect results or scope waveforms, and export SPICE/LTspice. Chip models are approximations; some catalog devices remain pinout only.
- **Digital circuits:** use Logic simulation to toggle inputs, step or run clocked behavior, and inspect signals across the design.
- **Schematics:** move/rotate parts and wires, open chip internals, and save layout. Layout metadata is embedded in .ckt comments; legacy sidecars still load for migration. Right-click supported components to edit values through the source bridge.
- **Chip library:** include("74HC00"); resolves a bundled definition. Chip sources are in circuitlab/libraries/chips/; shared drawing defaults are in circuitlab/libraries/layouts/.
- **Updates:** installed users use **OOC: Check for Updates**. Developers rebuild the VSIX after extension changes; compatible runtime updates can apply live, while registration/loader changes require reload.

Potentiometers use `P1(10k, 25%) between(high, wiper, low);`. The first value is total resistance; the position runs from 0% at `low` to 100% at `high`. Omitting position gives 50%. Click the potentiometer to open its position slider: dragging previews the electrical result, and releasing updates the code in one undoable edit. Right-click **Edit value** to change resistance and position together; leaving the resistance unspecified creates a schematic placeholder. The native model is an ideal linear potentiometer, exported to SPICE/LTspice as two resistor legs (an exact short at either endpoint). Try [the potentiometer example](examples/potentiometer.ckt).

Click **Oscilloscope** in the tab row to show the resizable panel beside the schematic. Select a node and click **Run**. Click **Oscilloscope** again to close the panel while the simulation keeps running. Value and time-step adjustments preserve elapsed time, capacitor voltages, and inductor currents. Changing the monitored node displays that signal's recorded history without restarting the circuit. AC and DC+AC waveforms use the configured frequency and phase, converting RMS amplitudes to peak amplitudes. **Pause/Resume** preserves the simulation. **Reset** starts it over and applies the initial-condition controls. Changing the circuit's connections requires a new run. Theoretical `Z` blocks still require physical R/L/C equivalents for time simulation.

The scope's time-step selector supports **ns, µs, ms, and s**. Changing units converts the displayed number and time-axis labels while preserving the physical sample interval and running history. Edit the number to change that interval; for example, 10 µs and 0.01 ms describe the same step.

The grey plot shows voltage divisions. Drag its left voltage axis upward to zoom in (smaller volts per division) or downward to zoom out. This freezes the displayed voltage range around its current center, so new samples do not rescale it. Click **Auto V** or double-click the left axis to fit the signal again. Display scaling does not change the simulation or its recorded samples.

**Optimize wiring** can compact empty space and move components as well as shorten wires. It checks electrical connections and grid alignment before accepting changes; **Ctrl+Z** restores the previous drawing. Saved layouts keep their positions until you explicitly optimize or move them.

For a perfboard placement reference, put the board declaration at the bottom of `main`, after the component calls and measurements:

```ooc
main {
    // Component calls and measurements go above the board declaration.
    perf[A-R,1-24] exclude[VfpgaPower, VdaisyLeft, VdaisyRight, RscarlettLeft, RscarlettRight];
}
```

This defines 18 lettered rows and 24 numbered columns at standard 2.54 mm hole spacing. The viewer colors the board green and places each physical chip pin on one hole. Two-terminal parts reserve at least three holes along one row or column, with their terminals two hole pitches apart. Excluded parts stay visible and electrically connected outside the board; the declaration does not change the simulated circuit. Omit `exclude[...]` when every part belongs on the board. See [the perfboard example](examples/perfboard.ckt).

If parts or wiring do not fit yet, the viewer still opens the complete circuit for editing. Overflow parts appear beside the board. Placement and routing warnings remain visible until the layout fits; moving parts, undoing, and reopening a saved layout update those warnings.

Right-click a component and select **Values only** to hide its title and show its value in smaller text. **Values only (all components)** applies this to the visible schematic. Hover to see the full component name and value; toggle the option again to restore titles. These display choices support Undo and are saved with the layout.

For capacitors and other two-terminal perfboard parts, right-click **Adjust length…** and enter the pin length in hole positions, including both terminal holes. A length of **7** spans seven holes (six pitches), keeping the first terminal fixed. The minimum is three holes. This changes the physical placement only, supports Undo, and saves with the layout.

Ctrl-click parts or Ctrl-drag a selection box to select several. Right-click a selected part or the selected area to apply **Adjust length…**, **Rotate clockwise**, or **Rotate counterclockwise** to the selection. Length changes apply to compatible two-terminal parts; rotation turns each selected part about its own center. Each batch operation is one Undo step.

Perfboard wires use half-hole routing steps and can extend outside the board; physical part contacts still snap to whole holes. **Split** creates independently movable matching ends, which stay separate when nearby parts move or rotate. To arrange parts first, right-click and choose **Remove all wires**, move or rotate the parts, then choose **Add wires** to connect their current placement. This placement mode supports Undo and save/reopen without changing the simulated circuit.

**Add wires** and **Optimize wiring** also match interchangeable resistors, capacitors, and other passive parts to nearer connections. Parts must have the same type, value, and model. Their schematic names and source connections move together between the existing physical positions; the source code and chip pin assignments stay intact. The result supports Undo and saves with the layout.

Double-click an empty area of the green perfboard to show a second, mirrored **Underside** view beside it. Hole labels identify the same physical holes on both faces. Right-click a wire or connected component pin and choose **Wire underneath** to put that branch on the solder side. Back wires appear in copper on the underside and dashed on the component side. Drag them in either view to adjust their route; **Wire on top** returns them to the component side. Face assignments support Undo and save/reopen. Double-click the board again, or use **Hide underside**, to close the second view. Ordinary clicks leave the view open.

**Optimize wiring** automatically considers both faces, choosing shorter routes and reducing crossings while keeping the physical part positions fixed. It can run wires beneath components and switch faces at available holes. The underside opens automatically when the result uses it; the complete optimization is one Undo step.

## FPGA

The viewer's **FPGA...** window supports the DE10-Lite HYBRID01 profile: 50 MHz clock, SW0-SW9, LEDR0-LEDR9, HEX0-HEX5 seven-segment displays and optional AUDIO_OUT. The profile can add two-stage switch synchronization; it is not debouncing. Install Quartus with MAX 10 support and the USB-Blaster driver separately.

Open a saved digital design, **Build from OOC**, inspect **Performance**, select the cable, then **Program FPGA**. A verified build is required. Programming uses volatile SRAM (.sof), not power-on flash. Fmax is a fitted estimate, not the running clock. Electrical transistor/op-amp circuits cannot compile directly to FPGA logic.

Digital chip behavior supports bounded `for` loops, bit/slice assignments (`LEDR[7:0] = elapsed[7:0];`), ternary expressions (`fsm = SW1 ? EARLY : WAITING;`), and `switch` with `case`, `break`, and `default`, including inside clock blocks. Loops expand at compile time; they do not consume one clock cycle per iteration. Indexed pin banks such as `LEDR[i]` can refer to separately declared `LEDR0`…`LEDR9` pins. Switches support stacked labels and fallthrough; put `break` directly at the end of a case, since conditional breaks are rejected.

Chip-local loops also expand `${expression}` inside identifiers. Given a declared `Counter(clk, enable, value)` chip with a four-bit output, this creates two independent counters:

```ckt
chip CounterPair(clk, enable0, enable1, first, second) {
    clk, enable0, enable1 = input;
    first[4], second[4] = output;
    for (int i = 0; i < 2; i++) {
        count${i}[4] = wire;
        Counter(clk, enable${i}, count${i});
    }
    first = count0;
    second = count1;
}
```

Interpolation uses the current compile-time loop scope, including decimal `const` bounds. Arithmetic such as `${i + 1}` and nested-index expressions such as `${i * 2 + j}` are supported. Open the parent block in Logic simulation to inspect every generated child and its independent state. Diagnostics retain the original template line. Signal-dependent `if` and `clock` blocks still execute at simulation time.

`*{...}` is an equivalent spelling of `${...}`. For example,
`for (int i = 0; i < 13; i++) { R*{i}[32]; }` generates the same declarations.
The braces distinguish interpolation from multiplication and wire counters;
`*{i}` reads the loop index and does not advance a `count` variable.

Headers and declarations accept numbered pin ranges: `chip HYBRID01(SW[9:0], LEDR[9:0]) { SW[9:0] = input; LEDR[9:0] = output; LEDR[9:0] = SW[9:0]; }`. A range expands in written order (`SW9, SW8, ... SW0`) into individual scalar pins, retaining the board pin assignments. Ranges are inclusive and descending, with indices from 0 to 4095. The existing `data[10] = input;` syntax still declares one ten-bit vector named `data`.

Inside a chip, `requests[8] = SW[7:0];` declares a new eight-bit combinational wire and its expression in one statement. The expression continuously drives the wire; no register is added. Arithmetic and ternary expressions work too. Bare declarations (`phase[32];`) and decimal literal initializers (`phase[32] = 0;`) retain their stored-state meaning. Once a signal is declared, `requests[3]` selects bit 3 instead of declaring a width.

Use two sizes to declare a local array of words. `words[8][32] = 0;` creates eight 32-bit registers and initializes each word to zero. The first size is the word count and the second is the width in bits. An omitted initializer defaults to zero. An array supports up to 256 words, 4,096 bits per word, and 65,536 bits total.

```ckt
chip Lookup(clk, write, read_index, write_index, data, out) {
    clk, write = input;
    read_index[3], write_index[3] = input;
    data[32] = input;
    out[32] = output;

    words[8][32] = 0;
    out = words[read_index];
    clock(clk, 10kHz) {
        if (write) { words[write_index] = data; }
    }
}
```

Indices start at zero. An out-of-range literal index is a compile error. An out-of-range runtime read returns zero and an out-of-range runtime write has no effect. Use `words[index][7:0]` to select part of a word. Reads and index expressions inside a clock block use the pre-edge state. If writes on the same edge overlap, the later write wins on the overlapping bits. Arrays stay local to their chip. Expose an individual word with a named wire such as `first[32] = words[0];` to watch it in the viewer. The Verilog exporter lowers word arrays to registers and selection logic.

Unsigned vectors support whole-bus `&`, `|`, `^`, `+`, `-`, `<<` and `>>`. Shifts zero-fill and keep the left operand's width: an eight-bit `value << 1` discards its old top bit, even when assigned to a wider output. A shift by the width or more produces zero. Addition binds more tightly than shifts, which bind more tightly than comparisons. Clocked state updates support `+=`, `-=`, `&=`, `|=`, `^=`, `<<=` and `>>=`, including constant bit/slice targets; each reads the previous clock-edge state. `!` remains logical NOT (a one-bit result); use `value ^ 255` to invert every bit of an eight-bit vector.

Declare a display bank with `HEX0[7:0] = output;` and drive it using `HEX0[7:0] = sevenseg(value);`. The helper decodes the low four bits as hexadecimal 0–F. `sevenseg(value, point)` lights the decimal point when `point` is nonzero; it is off by default. Bits 0–6 are segments a–g, bit 7 is the point, and zero lights a segment. Raw masks also work (`255` blanks a digit). Include each used bank in the chip header. The logic simulator shows all six digit positions above its schematic; click a segment to probe its signal. [Seven-segment and shift example](examples/advanced/fpga/07_seven_segment_display.ckt) shows these features together on the DE10-Lite without a clock.

Digital chips and `main` blocks also accept grouped signal declarations, with
optional section names:

```ooc
input controls {
    CLK, RESET;
    IRQ_LINES[240];
}
output state {
    PC[32], MSP[32], LR[32], LAST_PC[32];
    IR[16], LAST_IR[16], NZCV[4], FAULT_CODE[3];
    BOOTED, FAULT;
}
wire interrupts {
    irq0_ack, irq0_return;
    external_pending_lines[240];
}
```

Each entry ends with a semicolon. Widths and numbered pin ranges keep their
existing meaning. Static `for` loops can generate members inside a group.
Groups contain declarations; put equations and clocked behavior outside
them. Existing `name = input;`, `name[32] = output;` and `name = wire;`
declarations can be mixed with groups. Section names organize the editor
outline, and signals retain their original names. Input keywords are
orange, output keywords blue and wire keywords teal in both editors. Chip
header parameters use orange for inputs and blue for outputs; group members
retain their normal identifier colors.

Long chip calls can reuse named argument lists declared inside their chip or
`main` scope:

```ooc
pins[3:0] = "a, b, c, d";
FourPins(pins[3:0]);       // a, b, c, d
ThreePins(pins[2:0]);      // b, c, d
Mixed(a, pins[1:0]);      // a, c, d
```

The highest declared index names the first entry. Lists can span multiple
lines, and their declared range must match the number of comma-separated
signal names. Calls can select the whole list, a descending subrange, or one
index. These compile-time lists expand into ordinary connections, retaining
the same pin checks, simulation state and schematic wiring.

Chip-local `int` declarations name unsigned decimal constants:

```ooc
int FP_CLASS_MASK = 65456;
vstmdb = (first & FP_CLASS_MASK) == 60704;
```

Their widths are inferred from the literal, preserving the arithmetic of
the equivalent inline number. Digit separators are accepted. These chip
constants are immutable; `int` variables in `main` and loop headers retain
their existing compile-time behavior.

Clocked reset code can clear several registers or entire state arrays:

```ooc
clock(clk, 10kHz) {
    if (reset) {
        clear {
            priority_registers,
            usagefault_enable_state, memmanage_enable_state,
            busfault_enable_state, divzero_trap_state
        };
    }
}
```

`clear` expands to ordered zero assignments at the clock edge. Whole arrays
clear every word; selected array words, bits and slices work too. Commas or
semicolons can separate targets. In VS Code, groups, control blocks, ARM
code, multiline argument lists and multiline calls can be folded from their
opening line.

## Where to change the implementation

| Folder | Responsibility |
| --- | --- |
| circuitlab/language | Syntax, parameters, includes, generation and compilation |
| circuitlab/model | Electrical graph and component data |
| circuitlab/math | Equations, linear/nonlinear solvers and digital simulation |
| circuitlab/layout | Placement, routing, geometry and saved/shared layouts |
| circuitlab/viewer | Windows, schematic interaction, scope and FPGA UI |
| circuitlab/exporters | SPICE, LTspice and Verilog lowering |
| circuitlab/tooling | CLI, IDE bridge, source edits, updates and FPGA verification |
| circuitlab/libraries | Bundled chips and drawing defaults |
| ide/vscode | VS Code extension, syntax grammar and snippets |
| installer/windows | Installer and release build tooling |
| tests | Regression tests and fixed inputs |

Implement directly in these packages. Preserve source locations, instance names and net identities. Geometry edits must preserve connectivity. Keep electrical units separate from digital bit widths. Check behavior and fitted resources when optimizing HDL.

## Checks and packaging

    $env:QT_QPA_PLATFORM = 'offscreen'
    .\.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
    Get-ChildItem ide/vscode -Filter *.js | ForEach-Object { node --check $_.FullName; if ($LASTEXITCODE -ne 0) { throw "JavaScript syntax check failed" } }

Tests are grouped by the area to work on:

| Test folder | Coverage |
| --- | --- |
| tests/language | Circuit methods, control flow, includes and digital assembly |
| tests/simulation | Analog/digital solvers, BJTs, impedances, sources and MATLAB parity |
| tests/layout | Routing, grid alignment, connections and saved/shared layouts |
| tests/viewer | Schematic interaction, windows, logic simulation UI and FPGA panels |
| tests/exporters | SPICE, LTspice, Verilog and clocked logic generation |
| tests/tooling | CLI, IDE bridge, source editing, FPGA builds, packaging and updates |
| tests/libraries | Bundled chip definitions and imports |
| tests/integration | Architecture boundaries and behavior across subsystems |
| tests/fixtures | Shared circuit inputs, saved layouts, Quartus reports and MATLAB references |

Run one area or one module from the checkout root:

    .\.venv\Scripts\python.exe -m unittest discover -s tests/layout -t . -v
    .\.venv\Scripts\python.exe -m unittest tests.tooling.test_fpga -v

Use -t . with discovery so imports consistently start at the repository root. If a combined Qt run hangs during teardown, run modules in separate processes and report that distinction. Fixtures are regression inputs, not user projects; keep expected values independent of the implementation. Extension packaging and grammar tests are in tests/tooling; the Node command checks JavaScript syntax.

For a complete Windows installer, run the **Windows installer** workflow in GitHub Actions, or build locally with pip/setuptools and Inno Setup:

    .\.venv\Scripts\python.exe -m pip install setuptools
    .\.venv\Scripts\python.exe installer/windows/build_installer.py --iscc "C:\path\to\ISCC.exe"

The build downloads checksum-verified dependencies and writes the installer to dist/windows. Optional draft releases use GitHub-generated notes. See [extension packaging](ide/vscode/README.md); retain license and dependency metadata in distributions.
