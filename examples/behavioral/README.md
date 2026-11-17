# Main assembles; chips define behavior

Open an example in VS Code, run it to open the viewer, then use Logic simulation.
The Schematic tab shows instantiated chip packages and their actual connections.
The Logic simulation hood shows the compiled signal dependencies and state.

| Example | What to try |
| --- | --- |
| [01_byte_selector.ckt](01_byte_selector.ckt) | Click `a` and `b` to enter numbers, then toggle `choose`. `data` changes immediately. |
| [02_counter_bank.ckt](02_counter_bank.ckt) | Run, enable different counters, and inspect `data0`–`data2`. Each instance keeps independent state. |
| [03_hybrid01.ckt](03_hybrid01.ckt) | DE10-Lite profile: SW0 selects 440/880 Hz; LEDR0 toggles every 500 ms. Two chip calls build the design. |
| [04_switch_bank.ckt](04_switch_bank.ckt) | DE10-Lite profile: one loop generates ten switch/LED paths. Change `inverted` and recompile to select different hardware. |

The last two examples use the existing **FPGA… → Build from OOC** workflow;
Quartus generates the programming image. Bus examples are generic digital
designs, not pin mappings for that fixed board profile.

Bus input dialogs accept decimal, `0x` hexadecimal and `0b` binary. Values must
fit their declared unsigned width. Run uses the selected percentage of the
declared clock rate; Step advances one complete cycle. Combinational designs
need no Run button because input changes settle immediately.

See [the language guide](../../docs/behavioral_chips.md) for exact syntax,
compatibility, limits, and validation data.
