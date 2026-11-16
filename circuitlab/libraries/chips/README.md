# OOC chip library

In Electric Circuits, open **View → Chip Library** to browse the source and physical pin lists. Each entry shows the pin order for the package used by the model. Check the datasheet linked at the top of each file for other packages.

To arrange reusable schematics, open **View → Chip & Circuit Layouts**. Select a chip or shared circuit, move its parts/wires/labels, and click **Apply to all instances**. Every declaration has its own saved default. Clicking a nested block opens its own editor. Built-in gates use **View → Gate Layouts**. Circuit-specific saved layouts take priority; changing defaults does not change electrical source or values. Pinout-only chips have no modeled internals to arrange.

Copy the import into the circuit you are writing, then call the chip inside `main`. The arguments follow the order in the chip's header, from pin 1 onward. Give unused outputs separate net names. Tie unused inputs to a defined voltage. Hover over the call for the pin diagram.

```text
include("74HC00");

VCC(5) between(supply, 0);
VA(5) between(a, 0);
VB(5) between(b, 0);

main {
    SN74HC00(a, b, y, 0, 0, y2, 0, y3, 0, 0, y4, 0, 0, supply);
}
```

**Compile / Solve** gives a low reading for `y` in this example. Change either input source to 0 V to see it go high.

## Models by group

Most models are intended for nominal 5 V digital or small-signal demonstrations. Each file describes the modeled behavior. Use **Results → Run** for clocks and oscillators, and drive sequential parts through their reset, clear, or parallel-load pins to initialize them. The hold circuits used by the sequential models retain charge for a finite time. Their leakage and propagation delays are approximations.

| Folder | Package sources with models | Pinout references |
| --- | --- | --- |
| `timers` | NE555, NE556, CD40106B, CD4093B | CD4046B, XR2206 |
| `logic` | SN74HC00, SN74HC02, SN74HC04, SN74HC08, SN74HC32, SN74HC86 | |
| `sequential` | SN74HC74, SN74HC595, SN74HC165, SN74HC138, SN74HC157, CD4017, SN74HC4040 | |
| `analog` | LM741 (also uA741), LM358, LM324, TL071, TL072, NE5532, LM393, LM339, LM311, LM386 | |
| `switches` | CD4066, CD4051, CD4053 | |
| `audio` | | LM13700, AD633, PT2399 |
| `power` | LM317, TL431, ULN2003A | |
| `converters` | | ADS1115, MCP4921 |

Import a part with `include("74HC00");`, `include("NE555");`, or another chip filename without its extension. Names are case-insensitive and search all categories automatically. The files live under `circuitlab/libraries/chips/<folder>/<part>.ckt`. Existing `include("@chips/<folder>/<part>.ckt");` paths still work and can disambiguate duplicate short names. TI 74HC files can also be imported without the `SN` filename prefix. Calls use the `SN74HC…` name. Internal methods shared by several packages live in `shared` and load automatically.

A pinout reference can be placed and wired on the schematic. Simulation and SPICE export report the chip by name when a circuit calls a part that is waiting for a model. These packages use electrical chip definitions. Verilog export uses the language's separate digital chip declarations.
