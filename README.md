# Electric Circuits

A Python editor, schematic viewer, and solver for the Electric Circuits `.ckt` language.

## Quick start

Install Python 3.10 or newer. Extract the ZIP and open PowerShell in the directory containing `pyproject.toml`.

~~~powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m circuitlab examples\voltage_divider.ckt
~~~

On macOS or Linux, use `python3 -m venv .venv` and `.venv/bin/python` for the last two commands. Run the module without a file to open a blank editor.

In VS Code, select the project's `.venv` interpreter, open `circuitlab/__main__.py`, and click **Run Python File**. The terminal command above opens a specific example directly.

Open existing `.ckt` or `.txt` files with **File → Open**. Click **Compile / Solve** or press **Ctrl+Enter**. The divider example gives **4 V** at `out`. Click a diagnostic to jump to its line. Drag the divider to resize the editor and preview.

- **Schematic** shows the circuit. Click a wire or connector to highlight the whole node. Click a component label for its voltage and current. Junction dots show connections and gaps show unconnected crossings. Use **Fit**, the zoom buttons, or the mouse wheel to explore the drawing.
- **Results** and **Analysis window** display the same quantities, equations, and values. Search by quantity. Labels display spaces in place of underscores, while circuit code uses the original identifiers.
- **Render schematic** draws a parseable circuit when the solver cannot calculate its voltages and currents.

Inside `main` or a `circuit` method, type a section name to see matching definitions from the current editor. Press **Tab** or **Enter** to insert the call and start filling in its arguments. Use **Ctrl+Space** to open suggestions or **Esc** to dismiss them. Hover over a method or `op1` for its argument order. **Enter** follows the current line's indentation and adds a level after `{`. Type `}` on its own line to align it with the opening block. **Tab** indents by four spaces when suggestions are closed. **Shift+Tab** outdents the current line or selection.

## Wiring and pins

**Auto layout** uses conventional placements for recognized circuit shapes and routes other circuits from their compiled connections. On crowded schematics, it uses matching connectors for nets that would create many crossings. **All wires** uses the same conventional placements when available and draws continuous wires for non-ground connections. **Labeled connectors** uses short leads with matching names for distant connections. Grounded branches use local ground symbols where possible. Each ground symbol means node `0`. A gap marks a crossing between separate nets, and a dot marks a junction. Click a wire or connector to trace its net.

Choose **Add pin**, click a wire, connector, or component terminal, and enter a name. The Results tab opens filtered to that pin. It shows voltage and the current, voltage drop, and power of connected components. AC results also include RMS voltage, peak voltage, phase when defined, frequency when known, and reactive and apparent power. Combined DC + AC circuits include the voltage waveform when frequency is known.

Voltage and current sources are drawn vertically. Input sources declared before the circuit stages are placed on the left. Sources declared later are placed near the components that use their nets.

Click a pin to reopen its results. Right-click a pin marker to rename or remove it. Each node has one pin. Save the `.ckt` file to keep your pins with the circuit. They are stored as `# @pin` comments.

Open `examples/three_way_crossover.ckt` and add a pin to `midOut`. The result is about **802 mV RMS** at 1 kHz.

| Mode | Use it for |
| --- | --- |
| Auto | Selects DC, AC, or DC + AC from source units and frequency settings. |
| DC steady state | DC networks and known quantities such as `V = 12` and `R = 800`. |
| AC steady state | RMS phasors and impedance at one frequency. Set `f`, `T`, or `w`. |
| DC + AC | Separate DC and AC solutions plus combined `v(t)` and `i(t)` results. |
| Transient | First-order RC and RL step responses and constant-source ramps. |

## Export to LTspice

Click **Export to SPICE** in the toolbar or File menu. Save the `.cir` file, open it in LTspice, and click **Run**. Export uses the current editor text and analysis mode, including unsaved edits.

The netlist contains the resolved wiring, passive components, voltage and current sources, and a self-contained ideal op-amp model. DC export uses `.op`. AC export uses `.ac list` at the circuit frequency and the same RMS source amplitudes and phases shown in the app. For DC + AC, the netlist includes the DC offsets. Change the analysis line to `.op` in LTspice to view the DC results.

The `.cir` file is an editable text netlist. Schematic placement, named probes, calculated equivalents, and the app's standalone quantity and transient calculators stay in Electric Circuits.

For a drawing, click **Export LTspice .asc** in the Schematic tab or **Export LTspice Schematic** in the File menu. Open the `.asc` in LTspice to edit the components and wiring or run the included analysis command. The export uses the current wiring style and snaps the drawing to LTspice's grid. If the routes cannot fit safely, it keeps the component placement and connects the terminals by matching net labels.

## Circuit language

| Syntax | Meaning |
| --- | --- |
| `V1 in 0 0.5`, `R1 in out 1k` | Component rows give the name, positive node, negative node, and value. Ground is `0`. |
| `V1 = 12 : wire = Vin;` | Shorthand for a voltage source with positive terminal `Vin` and return at ground. Place it at the top level, in `main`, or in a `circuit` method. |
| `I1 = 10m : wire = in;` | Shorthand for a 10 mA current source directed from ground into `in`. Equivalent to `I1 0 in 10m`. |
| `V1 in 0 4 V \| 3 Vpk` | Mixed-mode source with 4 V DC and 3 V peak AC. |
| `(battery V1)` … `(end battery)` | Source block. Use `I = 10m` for a 10 mA current source that feeds the first named wire. In AC, use `V` for RMS, `Vpk` for peak, or `Vpp` for peak-to-peak. Use `phaseV` in degrees and `Vdc` for a DC offset. |
| `(series)` … `(end series)` | Connects entries end to end. Supports nested groups. |
| `(parallel)` … `(end parallel)` | Connects branches across the same two terminals. Supports nested groups. |
| `wire = w1` or `w1` | Names a junction inside a block. Matching names connect. In a battery or parallel block, the first wire is the input and the second is the return. |
| `series name { … }` | Defines a two-terminal section. `parallel name { … }` connects every branch across the two terminals. Both also accept explicit headers. A direct section can specify one input and end with `return wire;`. |
| `circuit name { … }` | Defines a reusable circuit with default terminals `a` and `b`. For a different terminal list, write a header such as `circuit name(input, output, ground) { … }`. |
| `R1(1k) between(a, b);` | Places a component between two wires. The existing component row `R1 a b 1k` also works in a circuit method. |
| `main { name(in, out); }` | Calls section definitions using actual wires. Every call has its own component and internal wire names. |
| `op1(0, sum, out);` | Places an ideal op amp with `in+`, `in-`, and `out` in that order. `(opamp U1)` … `(end opamp)` also accepts named pin assignments. |
| `measure = V out 0` | Measures voltage between nodes. Use `measure = R out 0` for resistance or impedance with sources off. Within a group, `measure = V` and `measure = R` use its terminals. |
| `measure = I R1`, `measure = P V1` | Measures branch current or power absorbed. Within a group, `measure = Ix` inserts an ideal ammeter. |
| `port = out 0` | Finds the Thévenin and Norton equivalents at two nodes. |
| `model = rc`, `model = rl` | Selects the transient step model. Use `Vsrc`, `t`, and `R`, plus `C` and `V0` for RC or `L` and `I0` for RL. |
| `v(t) = 3cos(6000t - 30) V` | Specifies an AC cosine in peak units with phase in degrees. |
| `#`, `## @param input` | Line comments and highlighted comment annotations. |

Bare numbers use base SI units. Common prefixes include `k`, `m`, `u`, and `n`. Use `examples/rc_lowpass.ckt` for an AC filter and `examples/three_band_mixer.ckt` for a 63-component DC + AC circuit.

### Braced batteries

```text
battery V1 {
    Vrms = 2.2;
    rad/s = 1000;
    wire = in;
}
```

`Vrms` sets the RMS voltage magnitude. `rad/s` sets the angular frequency and works like `w`. With one wire, the voltage source returns to `0`. A second `wire = ...;` sets the other terminal explicitly. Use `V` for DC and `Vdc` with `Vrms` for a DC offset. The parenthesized `(battery V1)` syntax is also supported.

### Inline blocks

Use `series { ... }` and `parallel { ... }` directly inside `main`. With one grounded source, bare top-level blocks in `main` connect in order from the source's non-ground terminal to `0`. A block inside a series section becomes one series element. A block inside a parallel section becomes one branch. Use `wire = out;` at a junction to name it, or specify terminals directly with `series(Vin, out) { ... }` and `parallel(out, 0) { ... }`. Explicit terminals also let you place inline blocks inside reusable `circuit` methods.

### Circuit methods

In a reusable `series` or `parallel` section, bind a parameter to a component or a tap with `parameter = R1;` or `parameter = wire;`. A call such as `divider(1k, 1k, 2k, left);` inside a group uses that group's terminals. Component bindings add parts in the order written, and a wire binding taps the junction at its position. To take a divider output between R2 and R3, place the wire binding between those component bindings.

To let a call choose the component type, use `a = part;` inside a reusable series or parallel section, or `a between(input, tap);` inside a circuit method. Pass a resistor, capacitor, or inductor as `R(1k)`, `C(100n)`, or `L(10m)`. You can supply a label with `R1(1k)` or `C1(100n)`. Circuit methods can mix part arguments and wire arguments in the same header.

`wire = name;` inside a parallel block marks the next branch's input, or the last branch's return if it is written after the final branch. The input taps share one electrical rail even when they have different names. Inside a series block, a wire marks the junction at that position.

Use `ground = agnd;` in a battery or section to name its return. Matching ground names connect to the same net, even when drawn with separate symbols. Different ground names are separate nodes. Use `ground = 0;` for the standard return. An isolated circuit with a named ground measures its voltages relative to that ground. Ideal op amps use the ground of their output network, or the ground reached through their signal connections when the output has no grounded load.

Inside a parallel block, `ground = name;` after a branch sets that branch's return. A `wire = ...;` between the branch and its ground assignment leaves the branch selection in place. Put `ground = name;` before the first branch to set the default return for the block. Branches without their own ground use the block's return.

A circuit method can call other circuit methods, series or parallel sections, and op amps. Each call gets its own components and local wires. A definition without a terminal list has two terminals, `a` and `b`, in that order. The call supplies their wires. Use an explicit list for extra terminals or custom terminal names. Literal `0` always connects to the shared ground. Circuit methods use wire arguments and reject recursive calls.

~~~text
V1 = 12 : wire = Vin;

series feed {
    R1 = 2k;
}

parallel load {
    R2 = 1k;
}

main {
    feed(Vin, out);
    load(out, 0);
    measure = V out 0;
}
~~~

This divider gives **4 V** at `out`. A three-terminal method can package both resistors in one definition.

~~~text
V1 vin 0 5

circuit divider(input, output, ground) {
    R1(10) between(input, output);
    R2(30) between(output, ground);
}

main {
    divider(vin, out, 0);
    measure = V out 0;
}
~~~

The example gives **3.75 V** at `out`. Open `examples/circuit_method.ckt` for the same divider powered by a battery block.

## Contributing

The editable install in Quick start picks up changes to `circuitlab/`. Restart the desktop app after changing Python code. Use one circuit per `.ckt` file. For a quick check, open `examples/voltage_divider.ckt` and look for **4 V** at `out`. Use `examples/rc_lowpass.ckt` and `examples/three_band_mixer.ckt` to exercise AC and mixed DC + AC analysis.

| If you're changing… | Start here |
| --- | --- |
| Source syntax or editor recognition | `syntax.py` for shared grammar, source lines, and mode detection. `editor.py` for completion and syntax highlighting. |
| Reusable circuit methods | `methods.py` for declaration validation, argument binding, and per-call names. |
| Wiring and compiler diagnostics | `compiler.py` builds the graph in `graph.py` from expanded statements. |
| Equations, units, or calculated quantities | `quantities.py` defines available inputs. `numbers.py` parses SI values. `math_engine.py` applies equation rules and uses SymPy for RC and RL step responses. |
| Circuit simulation or probes | `analysis.py` coordinates the solve and evaluates probes. `simulation.py` solves the nodal equations with NumPy and SciPy. `report.py` formats the results. |
| Schematic placement and wiring | `layout.py` builds conventional layouts. `routing.py` routes general circuits. `wiring.py` handles labeled connectors and buffered signal paths. `layout_checks.py` verifies net connectivity. |
| Desktop interactions | `gui.py` connects commands to the editor, results, and schematic. `schematic.py` paints the shared layout and handles clicks. `pins.py` reads and writes saved pin comments. |
| LTspice export | `spice.py` formats electrical values and writes `.cir` netlists. `ltspice.py` places native symbols and wires in `.asc` schematics. |

Both the solver and renderer consume the compiled `Circuit`. Keep electrical connections in that graph. The layout tracks each wire by its net so a visual crossing cannot become an electrical junction. `legacy_layout.py` retains the historical geometry used by the MATLAB comparison tests.

Call `circuitlab.analysis.solve_input(source, mode, backend=...)` to solve without Qt. A new simulation backend implements `SimulationBackend.solve` and returns the node voltages, branch values, and equivalent circuit fields defined by `Network`.

### Run the tests

~~~powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
~~~

The suite checks circuit methods, wiring, both export formats, and the desktop editor with Qt's offscreen platform. The captured MATLAB R2026a fixtures allow the parity tests to run with Python alone. To regenerate them in MATLAB, add `tests/` to the MATLAB path and call `captureReference(legacyRoot, casesJsonPath, referenceJsonPath)`.
