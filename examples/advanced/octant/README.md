# OCTANT

OCTANT scans eight simulated sensor inputs and records over-threshold events. The electrical schematic filters each input, buffers it with an LT1677, and compares it against a shared reference with two quad LM339 packages. An eight-way analog switch feeds the monitor and its exponential moving average. The FPGA console qualifies the eight trip lines, counts rising events, and keeps a set of pending channels until they are acknowledged.

## Single-step the OOC processor

Open [octant_cpu.ckt](octant_cpu.ckt) and select **Logic simulation → Schematic**. The overview opens on the CPU, memory, and alert blocks. Click a block to go inside it. **Back** returns to its parent at the current clock cycle.

| Block | What to watch |
| --- | --- |
| **Octant CPU Core → ROM** | The current `pc` and instruction `word`. |
| **Octant CPU Core → ALU** | The operands, effective address, ALU result, and branch decision. |
| **Octant CPU Core → Register File** | Live values of `r0` through `r7`. |
| **Octant CPU Memory → RAM** | The eight words of the four-entry log. **MMIO** shows the separate alert bus and acknowledgment. |
| **Octant Alerts → Octant Capture** | `pending`, the current `ack` mask, and the cumulative event count. Each qualifier is also inspectable. |

Click **Reset**, switch **TRIP2** and **TRIP5** on, and keep the **RESET** input low. Use **Step** while any block is open. Click a signal in the block to select its live waveform in **Probe**, or open **Signals** for the full register table. The main **Schematic** tab also has **Step logic** for advancing the same simulation while its packages are open.

| Step after Reset | What to look for |
| --- | --- |
| 1 | `R1 = 0x100`, the base address of OCTANT's registers. |
| 6 | `PENDING = 0x24`. OCTANT has qualified the two inputs. |
| 7 | The load at `LAST_PC = 0x08` copies the alert mask into `R3`. |
| 13 | `LOG_MASK0 = 0x24` and `LOG_COUNT0 = 2`. Both words of the first log entry have been stored. |
| 14 | `R4 = 1`. `PC = 0x28` and `ACK = 0x24` show the store that will execute on the next edge. |
| 15 | The store at `LAST_PC = 0x28` clears `PENDING` and leaves `LAST_ACK = 0x24`. The inputs are still high. |
| 16 | `R2 = 8`. The write pointer moves to the next log slot. |

`PC` and `IR` show the next instruction. `LAST_PC` and `LAST_IR` show the instruction executed on the preceding edge. To generate another alert, turn a trip off for at least five steps and then back on. After four batches the log wraps. `R7` reads the previous mask in a slot before firmware overwrites it.

The CPU uses its own Nios-style instruction encoding. It has eight 32-bit registers, a fetch ROM, a single-cycle ALU and branch datapath, and eight 32-bit RAM words. The register file and RAM use directly indexed OOC word arrays. Their individual words remain visible as `r0`–`r7` and `word0`–`word7`. The polling firmware is the ROM table in the `.ckt` file. It uses word-aligned `LDW` and `STW` for RAM and `LDWIO` and `STWIO` for OCTANT.

| Address | I/O operation |
| --- | --- |
| `0x100` | Read pending alert bits. |
| `0x104` | Read the hardware event count. |
| `0x108` | Write a mask to clear the selected pending bits. |
| `0x10C` | Read the current qualified input levels. |
| `0x110` | Read the most recent batch of rising alerts. |

To feed the actual electrical simulation into the processor, run the optional bridge from the repository root. The ROM firmware handles every acknowledgment. The supplied stimulus produces nine events in nine logged batches, with the last four retained in RAM.

```powershell
.\.venv\Scripts\python.exe .\examples\advanced\octant\run_cpu_demo.py
```

## Run the full demo

From the repository root with the project environment installed, run

```powershell
.\.venv\Scripts\python.exe .\examples\advanced\octant\run_demo.py
```

The demo turns on automatic scan for one run. At each 100 µs sample it reads the eight simulated `trip` voltages, applies a 2.5 V logic threshold, and clocks the digital console once. At 20 ms it acknowledges every channel. The table shows the selected channel, monitor and average voltages, live trip mask, sticky pending mask, lowest pending channel, and running event count. The masks are hexadecimal with channel 0 in the least significant bit.

The pending mask reaches `FF` on the first sweep, clears at 20 ms, and returns to `01` when channel 0 trips again. The run ends with nine events and prints both pages of the six-digit telemetry display.

## Open the electrical schematic

Open [octant.ckt](octant.ckt), use **Compile / Solve**, and view **Results**. At rest, the reference is about 1.56 V, channels 6 and 7 are high, and the default channel 0 monitor reads about 0.398 V.

Click an `OctantBank` package to see four input channels and an LM339. Click an `OctantChannel` inside the bank to see its RC filter and LT1677 follower. The top sheet keeps the switches and trip connectors accessible.

The source uses static loops and interpolated names to build the channels, a wire counter for the reference ladder, and an `@override` to set this scanner's averaging time to 2 ms. The reusable readout defaults to 5 ms.

| Schematic control | Effect |
| --- | --- |
| `Sselect0`, `Sselect1`, `Sselect2` | Select a channel in binary with automatic scan off. `Sselect0` is the least significant bit. |
| `Sscan` | Visit channels 0–7 in consecutive 3 ms slots during time simulation. |
| `Sinhibit` | Disconnect the selected input from the monitor bus. |
| `SreferenceDemo` | Lower the comparator threshold to about 1.19 V. The resting trip mask becomes `F0`. |

In **Oscilloscope**, choose `monitor`, `average`, or any `level0`–`level7` or `trip0`–`trip7` node. Set **Time step** to **100 µs** and run for at least 24 ms to see a full scan. The inputs repeat every 24 ms. At a fresh transient start the RC filters charge from zero, so the first scope values differ from the settled DC values.

## Open the FPGA console

Open [octant_controller.ckt](octant_controller.ckt) and select **Logic simulation**. Pulse `RESET` for one step. Raise a `TRIP0`–`TRIP7` input and hold it through six rising edges. `STABLE` shows the qualified level and `PENDING` latches the rising event. Lowering the input clears its qualified level after the falling edge has been qualified. The pending bit stays set until its corresponding `ACK` bit is clocked high. **File → Export Verilog** exports the digital design.

| Output | Readout |
| --- | --- |
| `VALID`, `FIRST`, `ONEHOT` | Whether any channel is pending, the lowest pending index, and the corresponding one-hot mask. |
| `MULTIPLE` | More than one channel is pending. |
| `EVENT_COUNT`, `LAST_EVENT` | Saturating count of qualified channel events and the bit mask of channels that arrived together on the latest event. |
| `HEX` | Active-low seven-segment indication of the first pending channel. Its decimal point lights when more than one is pending. |

The Python demo is the connection between the native electrical and digital simulators. The two entry files can also be opened and exercised independently.

Open [digital_blocks.ckt](digital_blocks.ckt) to exercise the display directly. Supply the same-named controller outputs in **Logic simulation**. With `PAGE=0` it shows active count, first pending channel, pending mask, and stable mask. With `PAGE=1` it shows the last event mask and the total event count. The six digit codes are active-low, with the decimal point at bit 7.
