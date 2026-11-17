# Object-Oriented Circuits (OOC)

A `.ckt` circuit programming language with an interactive schematic and simulation viewer. Write code in VS Code, save to refresh the viewer, and keep electrical connections in source. The original desktop editor remains available for existing workflows.

## Quick start

Windows users can use the standalone installer when provided with a release. It includes Python, the compiler/viewer, dependencies and chip library, and installs the extension into an existing VS Code installation. No repository checkout is needed. See the [installer guide](installer/windows/README.md) for installation, updates and release packaging. The source-development setup follows below.

Install Python 3.10 or newer. Extract the ZIP and open PowerShell in the directory containing `pyproject.toml`.

~~~powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\python.exe -m circuitlab --editor examples\circuit_method.ckt
~~~

On macOS or Linux, use `python3 -m venv .venv` and `.venv/bin/python` for the last two commands. `--editor` means the source is edited in your IDE: it opens the lightweight schematic/results viewer. Omit `--editor` to use the optional original desktop editor; running without a file opens a blank editor.

In VS Code, install the [OOC language extension](ide/vscode/README.md), open a `.ckt` file, and click its **play button**. The extension provides syntax highlighting, Outline, folding, navigation, compiler diagnostics, and gate/chip/wire hover previews. It reuses the viewer window and refreshes it when you save the circuit or its included files. Save all included source files before running.

In Visual Studio on Windows, use the [OOC editor setup](ide/visual-studio/README.md) to edit `.ckt` files with syntax highlighting and run OOC Check or OOC Solve from the Tools menu.

Open existing `.ckt` or `.txt` files with **File → Open**. Click **Compile / Solve** or press **Ctrl+Enter**. `examples/circuit_method.ckt` gives **3.75 V** at `out`. In the viewer, double-click a diagnostic to open its source location in VS Code. The viewer contains no source-editor widgets; **Save layout** writes presentation data into comments in the circuit file, checking with the IDE before saving so unsaved code is not overwritten. Unsaved layout changes pause automatic refresh until you save them.

- **Schematic** shows the circuit. Click a wire or connector to highlight the whole node. Click a component label for its voltage and current. Junction dots show connections and gaps show unconnected crossings. Use **Fit**, the zoom buttons, or the mouse wheel to explore the drawing.
- Drag components, packages, grounds, and labeled connectors to move them. Drag a horizontal wire up or down or a vertical wire left or right. Hold **Shift** while moving a symbol for diagonal leads. Right-click a part or ground and choose **Rotate clockwise** or **Rotate counterclockwise** to turn it 90°. Press **Ctrl+Z** in the schematic to undo a placement edit. **Reset placement** restores the automatic layout.
- Right-click a wire and choose **Split** to replace its continuous connection with matching labels. On a ground wire, this creates separate ground symbols instead. Drag a split end onto another segment of the **same node** to reconnect it; redundant split labels disappear. A wire on the other side of a resistor or another component is a different node and cannot be joined this way. These are layout edits, including undo and saved placement; the circuit source connections stay intact.
- **Ctrl+click** components and packages to toggle their selection, or hold **Ctrl** and drag from empty space to draw a selection box. Drag any selected item to move the group. Click empty space to clear the selection.
- **Grid** shows faint snap rows and columns, with a darker line every four intervals and no dots. Parts, grounds, pin ends, and wire segments always snap to grid steps, even with **Alt** held or the grid hidden. Alignment magnets only choose positions on the grid. Labels remain freely movable. Blocked moves show a prohibited cursor and keep the last valid position; releasing commits that position. Placement edits leave circuit values and connections unchanged.
- **Align to grid** adjusts the whole visible circuit, including opened chip contents, in one undoable step. It trims component leads to grid-sized lengths, spaces device pins on the grid, and reconnects wires, grounds and connector ends. **Ctrl+Z** restores the previous arrangement; **Save layout** keeps the aligned geometry for next time. New automatic drawings use the same alignment when the connection checks succeed. Saved circuit and shared gate layouts are kept until you explicitly align them. If a crowded drawing cannot align without joining different nets, the action leaves it unchanged. Zoom in to see the finer rows. Customize grid colors, line width and major spacing at the top of `circuitlab/viewer/schematic.py`; movement spacing and magnet strength are in `circuitlab/viewer/snapping.py`.
- Open **View → Gate Layouts** or right-click a built-in gate and choose **Edit shared gate layout**. Drag parts, wire segments, and pin labels or their endpoint circles. Select **Apply to all** to save the internals for that gate type and input count. Existing circuit-specific placements take priority. Use **Restore automatic → Apply to all** to remove a shared layout.
- Open **View → Chip & Circuit Layouts** to search the bundled library by category or device name, preview its internals, and copy an include/call template. Drag parts, wires, pin ends and labels, then choose **Apply to all instances** to save that device's default. Click a nested block to edit its own default; built-in gates open the gate editor. Right-clicking a library chip in the main schematic also opens its shared editor. Each declaration has an independent default, including the seven shared circuits and the uA741 wrapper; include-only aliases use the same declared chip. The seven pinout-only chips show their packages but have no internal model to arrange. Defaults also supply child sheets during LTspice export. Existing circuit-specific saved placements still take priority.
- Chip defaults persist in the same per-user app settings as gate defaults, under `device-layouts/v1/<name>` (gate keys remain unchanged). **Restore automatic → Apply to all instances** disables only the selected default and keeps its saved design for **Use saved layout**. Previews connect a declared `gnd` pin to `0`. If an override or different exposed-pin structure does not fit the saved geometry, the viewer uses automatic placement instead of changing connections. Applying a shared default does not rewrite circuit source or circuit-specific saved layouts. The browser is implemented in `circuitlab/viewer/device_layouts.py`; library discovery, persistence and instance mapping are in `circuitlab/layout/devices.py`.
- **Save layout** stores drawing edits inside the `.ckt` file in a managed `# @ooc-layout-begin` / `# @ooc-layout-end` comment block. Carry the `.ckt` file when moving the project. Existing `.ckt.layout.json` files still load and are copied into comments on save; the old JSON is retained as a backup. Embedded layouts take priority when both exist. In VS Code, hover over a wire argument to see a representative connected path with the hovered net in blue, or over a gate/chip name for its reference symbol or declared package pinout.
- **Results** and **Analysis window** display the same quantities, equations, and values. Search by quantity. Labels display spaces in place of underscores, while circuit code uses the original identifiers.
- **Logic simulation** appears for digital OOC chips in the IDE viewer. Toggle inputs, watch output indicators, use **Step** for one clock cycle, or **Run / Pause** for continuous simulation. **Reset** restores initial state and clears the trace. Select a signal to inspect its digital waveform and value. The schematic's live wire colors show logical **1** in green and **0** in gray; selected wires remain blue. These controls do not edit the source or saved layout.
- In **Logic simulation → Schematic**, click an input switch directly on the diagram; output lights and wires update in the same view. Clocked outputs advance with **Run** or **Step**, including the two-stage DE10-Lite input delay. Click the chip to open its hood and see compiled signal dependencies, register stages and live internal values; click its header to close it. Hover internal nodes for their expressions and clock information, or click a signal to select its trace. Zoom with the wheel and drag empty space to pan. The **Inputs** and **Outputs** arrow buttons beside **Run / Step / Reset** independently collapse the duplicate control panels. Routine status text and compiler diagnostics stay hidden on this tab; a message appears only if simulation fails.
- **Render schematic** draws a parseable circuit when the solver cannot calculate its voltages and currents.
- **FPGA…** opens the DE10-Lite `HYBRID01` workflow: **Build from OOC**, **Refresh cables**, select **USB-Blaster**, then **Program FPGA**. The viewer builds its currently loaded saved file with Quartus and displays progress in a log. **Open build folder** opens generated files and reports. Source changes invalidate the build; saving never programs hardware automatically. Quartus with MAX 10 support and the USB-Blaster driver must be installed separately. This profile uses the `HYBRID01` chip name, the onboard 50 MHz clock, any subset of SW0–SW9 and LEDR0–LEDR9, and optional `AUDIO_OUT`; it programs a volatile `.sof`, not power-on flash. See the [board guide](fpga/de10_lite/README.md).

### Optional desktop editor

Launch without `--editor` to use the original code editor, file tabs and quantity picker. Click a diagnostic to jump to its line. Drag the divider to resize the editor and preview; **Hide preview** and **Show preview** control the preview pane. **Save** writes source and its embedded layout comments together.

Inside `main` or a `circuit` method in this editor, type a section name to see matching definitions. Press **Tab** or **Enter** to insert the call and start filling in its arguments. Use **Ctrl+Space** to open suggestions or **Esc** to dismiss them. Hover over a method or `op1` for its argument order. **Enter** follows the current line's indentation and adds a level after `{`. Type `}` on its own line to align it with the opening block. **Tab** indents by four spaces when suggestions are closed. **Shift+Tab** outdents the current line or selection. The extension's separate editing features are described in the [VS Code guide](ide/vscode/README.md).

## Wiring and pins

**Auto layout** uses conventional placements for recognized circuit shapes and routes other circuits from their compiled connections. On crowded schematics, it uses matching connectors for nets that would create many crossings. **All wires** starts with continuous wires where possible. **Labeled connectors** uses short leads with matching names for distant connections. Right-click a non-ground wire and choose **Split** to create matching connectors. Drag matching ends together to draw a continuous connection. A rotated ground snaps onto its connected pin when brought close to it. A gap marks a crossing between separate nets, and a dot marks a junction. Click a wire or connector to trace its net.

Choose **Add pin**, click a wire, connector, or component terminal, and enter a name. The Results tab opens filtered to that pin. It shows voltage and the current, voltage drop, and power of connected components. AC results also include RMS voltage, peak voltage, phase when defined, frequency when known, and reactive and apparent power. Combined DC + AC circuits include the voltage waveform when frequency is known.

Voltage and current sources are drawn vertically. Input sources declared before the circuit stages are placed on the left. Sources declared later are placed near the components that use their nets.

Click a pin to reopen its results. Right-click a pin marker to rename or remove it. Each node has one pin. Existing `# @pin` source comments still work. In the IDE viewer, new names, renames and removals are saved as optional pin overrides in the embedded layout comment block; executable circuit code remains unchanged. The optional desktop editor continues to support source-comment pins and preserves these overrides when saving.

Open `examples/three_way_crossover.ckt` and add a pin to `midOut`. The result is about **802 mV RMS** at 1 kHz.

| Mode | Use it for |
| --- | --- |
| Auto | Selects DC, AC, or DC + AC from source units and frequency settings. |
| DC steady state | DC networks and known quantities such as `V = 12` and `R = 800`. |
| AC steady state | RMS phasors and impedance at one frequency. Set `f`, `T`, or `w`. |
| DC + AC | Separate DC and AC solutions plus combined `v(t)` and `i(t)` results. |
| Transient | First-order RC and RL step responses and constant-source ramps. |

## Connect circuit files

Use VS Code's Explorer and editor tabs to organize circuit source files. The optional desktop editor also retains **File → Open Folder**, **Back**, **Forward**, **Up**, multiple tabs and **View → Files**; each of its tabs keeps its editor text and layout. The lightweight viewer presents the circuit most recently run or opened.

Put `include("load.ckt");` at the top level of the file you build. Each path is relative to the file containing the include. Declare the nets to share with `connector(...);` in both files. Names match exactly, independent of pin order. Node `0` is common ground. Other undeclared nets stay local to their file, and component names can repeat across files.

```text
# main.ckt
include("load.ckt");
connector(signal, 0);
V1(8) between(vcc, 0);
R1(1k) between(vcc, signal);
```

```text
# load.ckt
connector(signal, 0);
R1(3k) between(signal, 0);
```

Select the including file to solve, render, or export the combined circuit. The IDE viewer and CLI use saved include files; the optional desktop editor can use unsaved text from its open tabs. Each included file can define and use its own methods. File connectors link component-based circuits for simulation and SPICE export.

### Chip library

Use `include("74HC00");` to load a bundled chip by name. OOC searches the categories in `circuitlab/libraries/chips` automatically; you do not need the folder or `.ckt` extension. Open **View → Chip Library** to browse the package definitions, or consult the [chip catalog](circuitlab/libraries/chips/README.md). Call the chip in `main`, with arguments in physical pin order, starting at pin 1. Hover over an imported chip call in VS Code for the pin diagram; the optional desktop editor also offers autocomplete inside `main`. Open `examples/chip_library_nand.ckt` for a runnable example.

An included file containing only `chip`, `gate`, or `circuit` definitions supplies reusable methods to the caller. Short chip names are case-insensitive and work in saved and untitled circuits. Existing paths such as `include("@chips/logic/74HC00.ckt");` still work. Use ordinary relative `.ckt` or `.txt` paths for your own definition files; these retain their local-file meaning. If two library files share a short name, specify the full `@chips/` path. Calls instantiate separate copies with local internal nets. See the catalog for supported behavior and any package-specific limits.

Open `examples/synth/synth.ckt` for a step sequencer that selects two notes, adds an accent envelope and filters the output. Its [project guide](examples/synth/README.md) lists the signals to probe and the chip demos.

## Export to LTspice

Click **Export to SPICE** in the toolbar or File menu. Save the `.cir` file, open it in LTspice, and click **Run**. The viewer exports its loaded source and analysis mode with saved include files. The optional desktop editor exports its current text, including unsaved edits. Export does not modify circuit source.

The netlist contains the resolved wiring, passive components, voltage and current sources, bipolar transistors, and a self-contained ideal op-amp model. DC export uses `.op`. AC export uses `.ac list` at the circuit frequency and the same RMS source amplitudes and phases shown in the app. For DC + AC, the netlist includes the DC offsets. Change the analysis line to `.op` in LTspice to view the DC results.

The `.cir` file is an editable text netlist. Schematic placement, named probes, calculated equivalents, and the app's standalone quantity and transient calculators stay in Electric Circuits.

For a drawing, click **Export LTspice .asc** in the Schematic tab or **Export LTspice Schematic** in the File menu. Open the `.asc` in LTspice to edit the components and wiring or run the included analysis command. The export uses the current wiring style and snaps the drawing to LTspice's grid. If the routes cannot fit safely, it keeps the component placement and connects the terminals by matching net labels.

Chips and gates export as hierarchical blocks. Saved internal placements are used even when the package is collapsed. Open the main `.asc` in LTspice, then right-click a block to open its internal schematic. Keep the generated `.asy` symbols and child `.asc` sheets beside the main file when moving the export.

## Circuit language

| Syntax | Meaning |
| --- | --- |
| `V1(0.5) between(in, 0);`, `R1(1k) between(in, out);` | Place components between their two terminals. Ground is `0`. |
| `V1 = 12 : wire = Vin;` | Shorthand for a voltage source with positive terminal `Vin` and return at ground. Place it at the top level, in `main`, or in a `circuit` method. |
| `I1 = 10m : wire = in;` | Shorthand for a 10 mA current source directed from ground into `in`. Equivalent to `I1 0 in 10m`. |
| `V1(4 V \| 3 Vpk) between(in, 0);` | Mixed-mode source with 4 V DC and 3 V peak AC. |
| `(battery V1)` … `(end battery)` | Source block. Use `I = 10m` for a 10 mA current source that feeds the first named wire. In AC, use `V` for RMS, `Vpk` for peak, or `Vpp` for peak-to-peak. Use `phaseV` in degrees and `Vdc` for a DC offset. |
| `(series)` … `(end series)` | Connects entries end to end. Supports nested groups. |
| `(parallel)` … `(end parallel)` | Connects branches across the same two terminals. Supports nested groups. |
| `wire = w1` or `w1` | Names a junction inside a block. Matching names connect. In a battery or parallel block, the first wire is the input and the second is the return. |
| `series name { … }` | Defines a two-terminal section. `parallel name { … }` connects every branch across the two terminals. Both also accept explicit headers. A direct section can specify one input and end with `return wire;`. |
| `circuit name { … }` | Defines a reusable circuit with default terminals `a` and `b`. For a different terminal list, write a header such as `circuit name(input, output, ground) { … }`. |
| `R1(1k) between(a, b);` | Places a component between two wires at the top level or inside a method. Existing node rows also work. |
| `R1 between(a, b);`, `Z1 between(a, b);` | Place a component with an unspecified value. Value parentheses are optional for R, C, L, V, I, and Z; write `(value)` when assigning one. Numerical analysis requires values. |
| `main { name(in, out); }` | Calls section definitions using actual wires. Every call has its own component and internal wire names. |
| `op1(0, sum, out);` | Places an ideal op amp with `in+`, `in-`, and `out` in that order. `(opamp U1)` … `(end opamp)` also accepts named pin assignments. |
| `transQ1(b, c, e, npn);` | Places transistor Q1 with base, collector, emitter, and polarity in that order. |
| `nor(vcc, (a, b), y);` | Places a built-in NOR gate with supply, grouped inputs, and output. |
| `measure = V out 0` | Measures voltage between nodes. Use `measure = R out 0` for resistance or impedance with sources off. Within a group, `measure = V` and `measure = R` use its terminals. |
| `measure = I R1`, `measure = P V1` | Measures branch current or power absorbed. Within a group, `measure = Ix` inserts an ideal ammeter. |
| `port = out 0` | Finds the Thévenin and Norton equivalents at two nodes. |
| `model = rc`, `model = rl` | Selects the transient step model. Use `Vsrc`, `t`, and `R`, plus `C` and `V0` for RC or `L` and `I0` for RL. |
| `v(t) = 3cos(6000t - 30) V` | Specifies an AC cosine in peak units with phase in degrees. |
| `#`, `## @param input` | Line comments and highlighted comment annotations. |

Bare numbers use base SI units. Common prefixes include `k`, `m`, `u`, and `n`. Use `examples/rc_lowpass.ckt` for an AC filter and `examples/three_band_mixer.ckt` for a 63-component DC + AC circuit.

### Theoretical complex impedances

Use `Zname(R+jX) between(a,b);` to place a fixed complex impedance in ohms for AC phasor analysis. The schematic shows an impedance block. Positive reactance is inductive; negative reactance is capacitive. For example:

```text
f = 1k
Vac(1 Vpk) between(input, 0);
Zs(2-j2) between(input, inv);
Zf(4+j4) between(output, inv);
op1(0, inv, output);
```

The ideal inverting amplifier has gain `-Zf/Zs = -2j`, so the output is **2 V peak at −90°**, or **1.414214 V RMS at −90°**. The op-amp pin order is `in+, in-, out`. Open [the complete example](examples/theoretical/complex_impedance_amplifier.ckt) to solve or render it.

Leave a theoretical impedance unspecified with `Z1 between(output, 0);`. It remains connected in the schematic and displays just its title, with no value or unit line. Defined complex values remain visible. The same optional-value syntax works for resistors, capacitors, inductors, voltage sources and current sources. Empty parentheses such as `Z1()` also remain accepted. Numerical analysis lists the parts that still need values; an unspecified value does not mean a short circuit or an open circuit. This also works in reusable circuits. Open [the unspecified-impedance example](examples/theoretical/unspecified_impedance.ckt) for a drawing to fill in later.

Right-click a Z block and choose **Edit value…** to enter a complex value such as `2-j2` or `4+j4`, or clear the field to make it unspecified again. Filling a value adds its parentheses in the source; clearing a placed R/C/L/Z value removes the parentheses. The edit recalculates the circuit when all required values are present. **Ctrl+Z** restores the previous value and source; Save/Discard works the same way as resistor value edits.

`Z` is a theoretical AC-only component: its value stays fixed when frequency changes. It does not define a DC or transient response and cannot be exported to SPICE/LTspice. Use explicit physical R/L/C parts for those workflows. Zero impedance is rejected; use the same wire name for a direct connection. Existing standalone `Z = ...` quantity calculations remain available.

### Bipolar transistors

Open `examples/bjt_switch.ckt` for a common-emitter NPN switch. Use `transQ1(base, collector, emitter, npn);` at the top level, in `main`, or directly in a method. Write the part name immediately after `trans`. The fourth argument is `npn` or `pnp`. The three pins connect to nets and can share a net. The older `transistor Q1 { … }` form also works.

Choose **Compile / Solve** for a DC operating point with generic NPN and PNP transistors. The built-in Ebers–Moll model uses fixed room-temperature parameters. Use SPICE for named models, BJT AC or port analysis, and circuits that do not converge.

The **Results** tab can step circuits with DC sources in time. Select a node, set the time step, and click **Run**. **Pause** freezes the display and **Resume** continues from the current simulation time. For `examples/bjt_oscillator.ckt`, select `c1`, set the initial capacitor to `C1` and its voltage to **1 V**, then run. For `examples/timer555.ckt`, leave reset low for a DC operating point. To see it oscillate, change `VReset(0)` to `VReset(5)`, render, then run `out` at the default 10 µs step. The solver automatically retries difficult switching edges with smaller internal steps.

For clocks and test signals, use `VClock(pulse(0, 5, 1m, 10u, 10u, 400u, 1m)) between(clk, 0);`. Arguments are low and high voltage, delay, rise time, fall time, high duration, and period. Your DC operating point uses the initial voltage. **Results → Run** steps the pulse, and SPICE export writes a `PULSE` source with a transient analysis command.

Omit the fifth argument to get a generic SPICE model of the selected polarity. To use a specific part, write `transQ1(b, c, e, npn, 2N3904);` and make the matching model available to the simulator. If needed, add a manufacturer `.model` card or `.include` to the netlist or as a SPICE directive in the LTspice schematic. The name alone carries no device parameters.

### Built-in gates

Call `not`, `buf`, `and`, `nand`, `or`, `nor`, `xor`, or `xnor` directly. A call such as `not(vcc, a, y);` uses the enclosing method's `gnd` pin when present, or node `0` otherwise. To specify ground, pass it after the supply as in `not(vcc, return_net, a, y);`.

`not` and `buf` take one input, `xor` and `xnor` take two, and `and`, `nand`, and `or` take two or more. `nor` takes one or more. With ground omitted, put multiple inputs to a variable-input gate in parentheses, as in `nor(vcc, (a, b), y);`. The existing flat form `nor(vcc, return_net, a, b, y);` passes an explicit ground. Each call expands to transistor circuitry for schematic viewing and SPICE export. Hover over a call for its pin order.

Put `@Override` above a top-level `gate` definition to replace a built-in for calls with the same number of pins in that file. Keep the built-in pin order. For example, this two-input NOR uses a different pull-up and leaves other NOR input counts available.

~~~text
@Override
gate nor(vcc, gnd, a, b, y) {
    RC(3.3k) between(vcc, y);
    CNode(10p) between(y, gnd);
    RA(47k) between(a, ba);
    RB(47k) between(b, bb);
    transQA(ba, y, gnd, npn);
    transQB(bb, y, gnd, npn);
}
~~~

### 555-style oscillator

Open `examples/timer555.ckt` for an eight-pin 555-style chip. A component-based `chip name(pins) { … }` or `gate name(pins) { … }` uses the same method calls and components as `circuit`. Its `srLatch` method connects the outputs of two transistor NOR gates back to each other's inputs. Two comparator methods monitor the timing capacitor against the reference resistor divider. The external resistors and capacitor set the oscillation rate.

Chips, gates, and transistors start as packages. Click a package to show its internals on the same schematic. Click it again to collapse it. Open the 555, then the latch, then a NOR gate to see its resistors, capacitor, and transistors. A single transistor opens to its three-terminal symbol. The schematic routes package wires even while their internals are collapsed. LTspice export includes the whole electrical circuit.

Choose **Render schematic** to trace the nets. OOC's SPICE export starts with a DC operating point, with reset held low in this example. To run the oscillator, replace `VReset reset_n 0 DC 0` in the exported netlist with `VReset reset_n 0 PULSE(0 5 20u 10u 10u 100m 200m)`, replace `.op` with `.tran 0 12m 0 1u`, and plot `V(timing)` and `V(out)`.

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

### Compile-time control flow

Use `for`, `while`, `do while` and `if / else if / else` in `main` and in electrical method bodies. The compiler evaluates these statements while building the circuit. Simulation voltages, currents and time are outside their scope.

~~~text
V1(12) between(n0, 0);

main {
    for (int i = 1; i <= 3; i++) {
        R${i}(1k) between(n${i - 1}, n${i});
    }
    RLoad(1k) between(n3, 0);
}

measure = V n3 0
~~~

This creates three 1 kΩ series resistors and one 1 kΩ load. Node `n3` is **3 V**. Use `${expression}` to insert a compile-time value into a name, net or component value.

~~~text
let count = 0;
while (count < 2) { count++; }
do { count++; } while (count < 3);
if (count == 3) { RLoad(3k) between(out, 0); }
else if (count == 2) { RLoad(2k) between(out, 0); }
else { RLoad(1k) between(out, 0); }
~~~

Put the fragment above inside an electrical body. Start each brace on the same line as its branch or loop header. Declare local integer or boolean values with `let`, `int` or `bool`. Use `break;` to exit the innermost loop and `continue;` to advance to its next iteration. Compilation reports an error beyond 10,000 entered loop iterations. Verilog export uses the separate digital-chip grammar below.

### Digital chips and Verilog

Open `examples/digital/nand.ckt` and choose **File → Export Verilog** to save a `.v` file. Select `NandGate` as the top-level entity when using this example as a standalone Quartus design.

After **FPGA… → Build from OOC**, the **Performance** tab shows fitted resource
usage, estimated Fmax, timing margins and worst setup paths. **Save as baseline**
keeps a verified build for comparison after edits and rebuilds. See the
[performance report guide](docs/fpga_performance.md) for interpreting measurements
and comparing results without changing the programming checks.

~~~text
chip NandGate(a, b, y) {
    a, b = input;
    y = output;
    y = !(a & b);
}
~~~

Declare each pin from the header as `input` or `output`. The grouped declaration `a, b = input;` is equivalent to `a = input; b = input;`. Header order sets Verilog port order. Each output gets one assignment.

Ports default to one bit; `data[8] = input;` or `data[8] = output;` declares an unsigned bus. Logic expressions support `!` (logical NOT), `&`, `^`, `|`, parentheses and one-bit values as before; typed bus expressions also support whole vectors, integer literals, `+`, `-`, comparisons and `&&`/`||`. Without a `main` block, a file can contain multiple chip definitions, each exported as a Verilog module.

Digital **`main` assembles chips; chips define behavior**. Use `main { ... }` or `main Demo { ... }`, declare external input/output signals and internal `link[8] = wire;` connections, then instantiate chips with ordinary positional calls. Existing compile-time `if`, `for`, `while`, and `do while` work in digital main. Within chips, complete `if/else` branches outside `clock` describe combinational selectors; inside `clock`, they select state updates on each rising edge. Each chip instance owns independent state. See the [complete syntax and compatibility guide](docs/behavioral_chips.md), [validation data](docs/behavioral_chips_validation.md), and [four runnable examples](examples/behavioral/README.md). Existing electrical main blocks and component-based chips retain their meaning.

Internal state can be declared separately from the ports. A bare name declares one unsigned bit; `[width]` declares an unsigned vector with a literal width from 1 through 4096. Both default to zero:

```text
phase[32];
blink_counter[25];
led_state;
sw_meta;
sw_sync;
```

An optional initializer replaces that zero: `phase[32] = 100;` or `flag = 1;`. Initializers are unsigned decimal integers, may contain underscores such as `1_024`, and must fit the declared width. Declare a state name once and leave it out of the chip's port list. These declarations emit initialized Verilog `reg` declarations. State advances only when assigned inside a `clock` block.

Read a vector bit with a literal zero-based index, such as `phase[31]`, in an output's logic expression. A bare scalar such as `flag` is read by name and cannot be indexed. Explicit `[1]` still declares a vector, so `one_bit[1];` is read as `one_bit[0]`. Whole vectors can drive vector outputs and wires, or be compared to produce a one-bit result; assigning a whole multi-bit vector directly to a scalar output is rejected.

Open [`examples/digital/state_declarations.ckt`](examples/digital/state_declarations.ckt) for an exportable example with zero defaults, nonzero initializers, and bit reads. Its state stays at its initialized values: names such as `phase` and `blink_counter` do not count or advance automatically. Digital main assembly supports chip instances and nested calls, with recursive calls rejected. Hardware support for declaration initializers depends on the FPGA target and synthesis tools; exporting Verilog does not establish board compatibility.

Chips with `input` and `output` declarations export to Verilog. Chips and gates built from electrical components export to SPICE.

### Clocked digital logic

[`examples/digital/HYBRID01.ckt`](examples/digital/HYBRID01.ckt) implements the complete 50 MHz switch-controlled 440/880 Hz phase accumulator and blinking LED example in OOC. Use **Logic simulation** in the IDE viewer to inspect its digital behavior, or export Verilog for synthesis. The electrical solver remains separate from the digital simulator.

The [DE10-Lite project and programming guide](fpga/de10_lite/README.md) supplies
HYBRID01's board pin assignments, 50 MHz timing constraint, the viewer's
**FPGA…** build/program workflow, and a PowerShell build alternative. Both
export the OOC source and run Quartus. Board configuration
lives separately from the language source; saving a `.ckt` does not program hardware.

The DE10-Lite build automatically synchronizes each declared SW0–SW9 input
through its own two 50 MHz clock stages. Connect switches to LEDs directly,
such as `LEDR0 = SW0;` through `LEDR9 = SW9;`, without manual registers or a
clock block. The profile requires `HYBRID01` and its `MAX10_CLK1_50` input;
choose at least one switch and one output from SW0–SW9, LEDR0–LEDR9, and optional
`AUDIO_OUT`. Existing four-port oscillator programs remain supported. Validated
`sw_meta`/`sw_sync` logic is retained without adding another pair when SW0 is the
only declared switch. For multiple switches, remove that manual SW0 chain and
read the switch inputs directly; mixing manual SW0 synchronization with other
switches is rejected.
Synchronization is independent per switch: it does not debounce switches,
transfer their values as an atomic ten-bit bus, or generate clocks. Generic
**File → Export Verilog** stays literal and board-neutral. Open
[`examples/digital/de10_switch_led.ckt`](examples/digital/de10_switch_led.ckt)
for the full ten-pair example, or see the
[board guide and optional `-Source` build argument](fpga/de10_lite/README.md).

For a phase accumulator, write the desired frequency directly and supply the input clock rate once on the block:

```text
clock(MAX10_CLK1_50, 50MHz) {
    if (sw_sync == 0) {
        phase += 440Hz;
    } else {
        phase += 880Hz;
    }
}
```

With `phase[32];`, these increments compile to 37796 and 75591. The compiler calculates `round(frequency * 2^width / update_frequency)` using exact rational arithmetic, rounding ties upward. Read the phase vector's highest bit for the requested square wave. Actual frequency is quantized to the nearest representable step. `Hz`, `kHz`, `MHz`, and `GHz` accept decimal values and underscore digit separators. The rate annotation describes the external clock; it does not generate a clock or create FPGA timing constraints.

Frequency literals are supported specifically in `phase += frequency;` (or `phase = phase + frequency;`) updates of a vector at least two bits wide. The enclosing clock block must specify its input frequency. Zero Hz holds phase; positive frequencies that round to zero, frequencies above half the phase update rate, and conflicting rates for the same clock pin are rejected. Ordinary `clock(clk)` blocks and integer arithmetic remain supported.

```text
chip Blink(clk, y) {
    clk = input;
    y = output;
    led;
    clock(clk, 50MHz) {
        every(500ms) {
            led = !led;
        }
    }
    y = led;
}
```

`every(500ms)` automatically creates an initialized counter sized for the interval. At 50 MHz it runs its body on rising edges 25,000,000, 50,000,000, and so on: the LED toggles twice per second, completing one full on/off cycle per second. Other statements in the clock block continue to run on every edge; `every` does not delay or stop the clock.

Intervals accept `s`, `ms`, `us` (also `µs`/`μs`), and `ns`, with decimals and underscore digit separators. They round to the nearest whole number of input clock cycles, with ties rounded upward. Intervals shorter than one clock period and missing clock frequencies are rejected. Place each `every(...)` directly inside its `clock(...)` block; nested or conditional timers are not supported. Conditions inside an `every` body are supported and do not pause its timer. Multiple `every` blocks have independent counters. An interval of one clock period executes every edge without a counter. Assignments still use the previous state, and the last executed assignment to a state wins.

Inside `every(...)`, Hz increments use the effective update rate: input clock frequency divided by the rounded interval cycle count. For example, at an 8 Hz clock, `every(500ms) { phase += 1Hz; }` advances an 8-bit phase by 128 every four edges. A request above half that effective update rate is rejected. Conditional branches can further gate updates; while disabled, phase holds and the output no longer runs continuously at the requested frequency.

- `clock(input_pin) { ... }` runs on each rising edge. Every expression reads the state from before that edge; updates take effect together. Thus `sw_meta = SW0; sw_sync = sw_meta;` describes two stages of synchronization, not two immediate software assignments.
- Inside clock blocks, `state += expression;` is shorthand for `state = state + (expression);`. It also accepts frequency increments such as `phase += 440Hz;`. Both forms read the previous state; repeated `+=` assignments on one edge do not accumulate sequentially.
- Unassigned state holds its value. If a state is assigned more than once on an executed path, the last assignment wins. A state may be driven by only one clock block.
- `const NAME[width] = value;` declares an unsigned constant; omit `[width]` for a one-bit constant. Constants require a decimal initializer that fits the width.
- Clock expressions support decimal literals, constants, signals, constant bit reads, parentheses, `+`, `-`, `==`, `!=`, `<`, `<=`, `>`, `>=`, `!`, `&`, `^`, `|`, `&&`, and `||`. Arithmetic and bitwise results have the larger operand width; overflow wraps at that width. Unsized decimal literals use the minimum unsigned width. Assignments truncate or zero-extend to the destination. Comparisons and logical operators produce one bit. `!` means logical NOT (zero becomes one).
- `if (...) { ... }`, `else if (...) { ... }`, and `else { ... }` select updates inside a clock block. These are runtime hardware branches, separate from electrical compile-time control flow. Multiplication, division, signed arithmetic, slices, runtime loops, falling-edge blocks, and reset declarations are not implemented.
- Scalar ports and combinational expressions retain their existing syntax. Ports, internal wires and state can also be unsigned vectors; `phase[32]` declares 32 bits and `phase[31]` reads its most significant bit. Complete combinational if/else branches outside clocks drive outputs/wires without creating storage.

The exporter uses initialized registers and nonblocking Verilog assignments. Provide the actual board's pin assignments, I/O standards, and clock timing constraints in your FPGA project. A two-stage switch synchronizer does not debounce the switch. Tests compare HYBRID01 state updates against its VHDL behavior using a test-only evaluator of the emitted subset; this is not a substitute for an independent HDL simulation, synthesis, or board validation.

### Interactive digital simulation

Open [`examples/digital/logic_playground.ckt`](examples/digital/logic_playground.ckt) and select **Logic simulation**. Toggle `a` and `b` to test AND/XOR outputs. **Speed** selects **25%, 50%, 75%, or 100%** of the declared clock rate; 100% is the default. Run follows elapsed wall time instead of a fixed number of cycles per display refresh. For HYBRID01 at 50 MHz, `every(500ms)` toggles its LED every 0.5 real seconds at 100%, or every 2 seconds at 25%. Pause freezes simulation time, changing speed preserves state, and Step always advances exactly one cycle. A clock without a declared frequency can be stepped, but requires a frequency such as `clock(clk, 50MHz)` before Run is available.

The trace shows the selected signal and its upstream dependencies; **All ports** adds the remaining inputs and outputs. Repeated levels are compressed, with a bounded history of 512 points. During Run, fast-changing vector values are sampled to keep the viewer responsive; intermediate bus values may be omitted from the display. Observed single-bit transitions retain their cycle positions. Step records the next cycle exactly. The horizontal axis uses clock cycles, or input events for combinational chips. Clock pins are omitted from traces and stay neutral in the schematic because samples are taken between full clock cycles. Click a clock pin to see its declared rate. The diagram and waveform use neutral gray backgrounds; active signals remain green.

The runtime advances simple counters, accumulators and stable logic through mathematically proven intervals, stopping at branch changes, observed signal transitions and unsigned wrap boundaries. Unsupported expressions use the cycle-by-cycle interpreter. This lets HYBRID01 and the switch/LED demo keep up with their 50 MHz clock without discarding state updates. Real-time performance is not guaranteed for arbitrary circuits or computers: **Behind target** appears beside Speed when computation falls behind. Lower the speed or use Pause/Step in that case. An input click takes effect at the displayed simulation cycle, after processing due old-input work within the UI's time budget; it is not a timestamped hardware event.

For [`examples/digital/de10_switch_led.ckt`](examples/digital/de10_switch_led.ckt), the **DE10-Lite** profile uses the same generated synchronization wrapper as **Build from OOC**. Toggle `SW0` and step twice: `LEDR0` follows after two rising edges. The other nine switch/LED pairs behave independently. The **OOC** profile runs the chip's literal declarations without the board wrapper; direct combinational assignments then update immediately. A compatible board design initially selects DE10-Lite; you can choose the profile explicitly.

The simulator evaluates only the Verilog subset emitted by OOC, with unsigned widths, initialized state, simultaneous clocked updates, combinational and clocked `if/else`, Hz increments, and `every(...)` timers. It supports one chip or assembled main design with at most one clock input. Click a bus input to enter an unsigned decimal, hexadecimal (`0x`) or binary (`0b`) value; buses display numeric values and neutral wire colors rather than treating an entire number as one logic level. It does not load arbitrary Verilog or emulate an ARM processor. Signal displays and traces represent ideal two-state logic, sampled at settled clock-low states; they do not model analog voltage/current, propagation delays, unknown/high-impedance states, switch bounce or metastability. They are software simulation, not readings from a connected FPGA. Display refresh is about 30 Hz, separate from the simulated clock rate: use the waveform to examine faster signals.

For an electrical circuit, the **Results** tab instead contains the existing voltage oscilloscope: choose a node, time step and optional initial capacitor voltage, then **Run**. For example, open `examples/bjt_oscillator.ckt` and follow its initial-condition instructions. Its voltages come from the native electrical model, with that model's convergence and accuracy limits. Digital traces never substitute logic 0/1 for those voltage measurements.

Implementation: `circuitlab/math/digital.py` evaluates compiled logic; `circuitlab/math/digital_fast.py` proves intervals safe to advance together; `circuitlab/viewer/digital.py` contains the speed controls, indicators and digital traces; `circuitlab/viewer/logic_scene.py` draws the interactive gray diagram; `circuitlab/viewer/schematic.py` paints live logic colors; `circuitlab/viewer/window.py` connects them. The analog oscilloscope remains in `circuitlab/viewer/scope.py`.

## Compact circuit examples

Explore the [compact circuit lab](examples/compact_lab/README.md): ten fresh
examples covering phase cancellation, resonators, pulse envelopes, an oscillator,
logic, a metallic percussion patch, a generated DAC, passive filtering and a gate
override. Each includes useful probes and settings; the guide lists verified
outputs and the shortcuts used.

## Contributing

The editable install in Quick start picks up changes to `circuitlab/`. Restart running viewer/hover processes after changing Python code. Read the [architecture overview](docs/architecture.md) and [development guide](docs/development.md) before adding a feature. The [refactor validation report](docs/refactor-report.md) records preservation checks, test results and remaining limits.

For a quick check, use `examples/circuit_method.ckt` and look for **3.75 V** at `out`. The current `examples/voltage_divider.ckt` has a 2 kΩ upper resistor and 10 kΩ lower resistor on 12 V, giving **10 V**. The original 4 V scenario is preserved separately in `tests/fixtures/voltage_divider.ckt`; tests do not depend on changing that editable example back. Use `examples/rc_lowpass.ckt` and `examples/three_band_mixer.ckt` for AC and mixed DC + AC analysis.

| If you're changing… | Start here |
| --- | --- |
| Source syntax or editor recognition | [`language/syntax.py`](circuitlab/language/syntax.py) for grammar and source rows; [`tooling/source_info.py`](circuitlab/tooling/source_info.py) for shared text inspection. |
| Reusable circuit methods | [`language/methods.py`](circuitlab/language/methods.py) for declaration validation, argument binding and per-call names. |
| Compile-time control flow | [`language/control_flow.py`](circuitlab/language/control_flow.py) expands loops and branches into electrical statements. |
| Wiring and compiler diagnostics | [`language/compiler.py`](circuitlab/language/compiler.py) is the entry point, [`language/document.py`](circuitlab/language/document.py) expands documents, and [`language/builder.py`](circuitlab/language/builder.py) constructs the [`model/graph.py`](circuitlab/model/graph.py) circuit/component records. |
| Equations, units, or calculated quantities | [`math/quantities.py`](circuitlab/math/quantities.py), [`math/numbers.py`](circuitlab/math/numbers.py), and [`math/equations.py`](circuitlab/math/equations.py). |
| Circuit simulation or probes | [`math/analysis.py`](circuitlab/math/analysis.py) orchestrates solving; [`math/linear.py`](circuitlab/math/linear.py) and [`math/nonlinear.py`](circuitlab/math/nonlinear.py) implement numerical analysis; [`math/results.py`](circuitlab/math/results.py) defines backend/result contracts; [`math/report.py`](circuitlab/math/report.py) formats results. |
| Schematic placement and wiring | [`layout/geometry.py`](circuitlab/layout/geometry.py) defines drawing data/pins, [`layout/automatic.py`](circuitlab/layout/automatic.py) places known circuit shapes, [`layout/routing.py`](circuitlab/layout/routing.py) routes arbitrary graphs, and [`layout/editing.py`](circuitlab/layout/editing.py) handles interactive movement. |
| Viewer and original editor | [`viewer/window.py`](circuitlab/viewer/window.py) is the lightweight viewer; [`viewer/legacy.py`](circuitlab/viewer/legacy.py) is the optional editor; [`tooling/session.py`](circuitlab/tooling/session.py) owns source/compilation state without Qt. |
| LTspice export | [`exporters/spice.py`](circuitlab/exporters/spice.py) writes netlists; [`exporters/ltspice.py`](circuitlab/exporters/ltspice.py) writes native sheets and hierarchy companions. |
| Digital chip export | [`exporters/verilog.py`](circuitlab/exporters/verilog.py) validates one-bit combinational outputs, unsigned state, constants, and rising-edge clock blocks, then emits Verilog. Expression parsing and clocked branches live in [`exporters/clocked.py`](circuitlab/exporters/clocked.py). |

Both the solver and renderer consume the compiled `Circuit`. Keep electrical connections in that graph. The layout tracks each wire by its net so a visual crossing cannot become an electrical junction. `layout/legacy.py` retains the historical geometry used by MATLAB comparison tests. Root-level compatibility modules keep older imports working; new implementation belongs in the organized packages.

Call `circuitlab.math.analysis.solve_input(source, mode, backend=...)` to solve text without Qt, or use `circuitlab.tooling.session.CircuitSession` for a saved document and its includes. Import `Network` and `SimulationBackend` from `circuitlab.math.results`. A new simulation backend implements `SimulationBackend.solve` and returns the node voltages, branch values and equivalent-circuit fields defined by `Network`.

### Run the tests

~~~powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
$env:OOC_PYTHON = (Resolve-Path .\.venv\Scripts\python.exe).Path
node --test ide/vscode/test/*.test.js
~~~

The suites check language and graph behavior, nominal gate truth tables, calculations, wiring, exports, both viewer shells and IDE integration. Qt tests use the offscreen platform; Node is needed for extension tests. Immutable refactor fixtures preserve source scopes, terminal mappings, selected numerical outputs, SPICE text and saved root/child layouts. The captured MATLAB R2026a fixtures allow parity tests to run with Python alone. To regenerate them in MATLAB, add `tests/` to the MATLAB path and call `captureReference(legacyRoot, casesJsonPath, referenceJsonPath)`.

### Distributing shared defaults

In **Gate Layouts** or **Chip & Circuit Layouts**, apply your edits, then click
**Save defaults to project**. Save to `circuitlab/libraries/layouts/defaults.json`
and commit that file. It includes active gate and chip defaults, and is included
in the Python package/installer. A fresh installation reads these bundled defaults;
personal overrides and circuit-specific saved layouts take priority. Publishing does not
commit or upload anything. The initial file may be empty until you export from the
app running under your own Windows account.

Ctrl-click or Ctrl-drag a selection rectangle now includes boundary pin ends,
split connectors and grounds, alongside components. Drag a selected member to move
the group; fixed terminals remain connected and Ctrl+Z undoes the group movement.
New automatic layouts also shorten unnecessary channel detours and try reversing
passive components when that reduces wiring. Shortcuts retain branch connections,
avoid symbol bodies, and must pass the electrical net checks. Block layouts try
four additional placements without increasing their congestion score. These searches
are bounded heuristics, not a global minimum; automatic optimization is limited to
64 components per displayed level to keep large drawings responsive.

For an existing drawing, click **Optimize wiring** beside Undo, choose
**View → Optimize wiring**, or right-click the
schematic and choose **Optimize wiring** (also available in the gate/chip layout
editors). This also tries moving components and chip/circuit blocks toward their
connections on the grid, accepting shorter routes that preserve connectivity and
avoid covering other components or wires. It can rotate passive parts by 90° or
180° to fit the signal flow, and remove staircase bends even when the wire is
already at its shortest orthogonal length. The search tries at most 36 moves per
displayed level; it does not guarantee a globally optimal arrangement.
The status line reports the wire-length reduction or explains that no safe
improvement was found. **Ctrl+Z** undoes the whole operation. Saved layouts are only optimized when
you request it; save/apply the result as usual to retain or distribute it.

### Editing values from the schematic

Right-click a resistor, capacitor or inductor and choose **Edit value…** to update
its value in the source. Add or remove
components in your code; the schematic does not offer component insertion or deletion.

Value edits recompile the circuit while preserving positions and connections.
Ordinary dragging only changes presentation. In VS Code, value edits use an undoable
workspace edit and save the document. Dirty or stale source is refused: save and
refresh first. The viewer keeps track of value changes until you choose **Save**.
When closing it or switching files, **Discard** restores values in the code to the
last viewer save (or the values before the first edit if you have not saved).
This includes edited definitions in linked files. If the code has changed independently,
discard is refused and the viewer stays open rather than overwriting those edits.
The optional built-in editor keeps edits in its text undo history
until saved. Editing an included/shared definition changes its repeated instances.
Generated built-in components or ambiguous statements must be edited in their source definition.

In the viewer, **Ctrl+Z** (or the **Undo** button) undoes value changes and layout
moves in chronological order. Value undo restores the code and preceding drawing.
In VS Code it uses the same checked edit-and-save bridge and waits for acknowledgment.
A rejected undo remains available to retry; it never overwrites externally changed source.
Undo history is session-only and is cleared when a different file or changed source
is loaded. Toolbar focus and rerendering the same source do not discard undo.
**Reset placement** is also undoable; layout changes wait while VS Code is applying
a value edit so they cannot interfere with its acknowledgment.
The optional built-in editor keeps text undo separate from the schematic page.
Value undo is also available in VS Code or the built-in text editor.
