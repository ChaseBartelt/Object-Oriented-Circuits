# Step sequencer synth

Open `synth.ckt` in the updated Electric Circuits app and click **Render schematic**. Click a block to see its internals. In **Results**, choose `speaker`, set **Time step** to **40 µs**, leave **Initial capacitor** at **None**, and click **Run**. After startup, the output swings around 0 V. The scope advances in simulated time and can take a while to reach the next beat.

| Probe | What you see |
| --- | --- |
| `beat` | 10 Hz clock for the CD4017 |
| `step0`, `step5` | Two of the counter's decoded steps |
| `pitch` | Selects the 440 Hz or 588 Hz source through the CD4066 |
| `envelope` | Charges on accented steps and decays between them |
| `mix` | LM358 buffer output before the low-pass filter |
| `speaker` | AC-coupled LM386 output across a 1 kΩ test load |

Edit note periods in `modules/timing.ckt`, the pattern in `modules/sequencer.ckt`, and the filter and amplifier in `modules/audio.ckt`. The main synth uses clock and tone sources so the sequencer and audio signal path can be studied independently. Open demos 01 and 02 to see actual 555-based oscillators.

The `demos` folder, together with the synth, instantiates all 40 catalog chips. Demos 01 and 02 use **10 µs**. Demo 05 renders and exports but hit a built-in DC convergence limit with its CD4051. The ripple-divider demo can also stall after several edges. Demo 13 is a schematic wiring reference for the seven pinout-only entries. The remaining circuit demos include the logic gates, clocked parts, analog stages, and supply/driver circuits.
