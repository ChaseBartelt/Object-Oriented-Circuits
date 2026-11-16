# HYBRID01 on the DE10-Lite

This project builds `examples/digital/HYBRID01.ckt` for the Terasic DE10-Lite's
MAX 10 `10M50DAF484C7G`. Edit the OOC file, then build and program it through the
lightweight viewer or the PowerShell workflow below. Both regenerate Verilog
from OOC; changes to generated HDL are replaced by the next build. The board
assignments and timing constraints are kept here in source control.

## Ten switches and ten LEDs

The DE10-Lite build synchronizes each declared slide-switch input automatically.
This complete example connects all ten switches to their matching onboard LEDs,
without state declarations or a clock block:

```text
chip HYBRID01(MAX10_CLK1_50, SW0, SW1, SW2, SW3, SW4, SW5, SW6, SW7, SW8, SW9, LEDR0, LEDR1, LEDR2, LEDR3, LEDR4, LEDR5, LEDR6, LEDR7, LEDR8, LEDR9) {
    MAX10_CLK1_50, SW0, SW1, SW2, SW3, SW4, SW5, SW6, SW7, SW8, SW9 = input;
    LEDR0, LEDR1, LEDR2, LEDR3, LEDR4, LEDR5, LEDR6, LEDR7, LEDR8, LEDR9 = output;

    LEDR0 = SW0;
    LEDR1 = SW1;
    LEDR2 = SW2;
    LEDR3 = SW3;
    LEDR4 = SW4;
    LEDR5 = SW5;
    LEDR6 = SW6;
    LEDR7 = SW7;
    LEDR8 = SW8;
    LEDR9 = SW9;
}
```

Open [`examples/digital/de10_switch_led.ckt`](../../examples/digital/de10_switch_led.ckt)
and use **FPGA… → Build from OOC**. Each used switch gets its own two-stage
synchronizer driven by the onboard 50 MHz clock. The corresponding LED follows
the synchronized switch, adding two clock stages of latency. Existing validated
`sw_meta`/`sw_sync` code keeps its own two stages when SW0 is the only declared
switch, preserving the original four-port oscillator. When adding other
switches, remove that manual SW0 chain and read SW0 directly so every switch
uses automatic synchronization. Mixing the old manual chain with additional
switches is rejected.

This behavior belongs specifically to the DE10-Lite profile's SW0–SW9 inputs.
It does not debounce the switches or create clocks. The ten independent paths
do not provide an atomic ten-bit bus transfer: switches changing together can
be observed on different clock cycles.
**File → Export Verilog** remains board-neutral and exports the source
literally; automatic board synchronization is applied by **Build from OOC**
and the build script below.

## Build and program from the viewer

Install Quartus Prime with MAX 10 device support separately; the OOC installer
does not include Quartus or the USB-Blaster driver. Connect the DE10-Lite and
install its driver if necessary, using the connection instructions below.

1. Save the OOC source in VS Code and run it to open or refresh the viewer.
2. Click **FPGA…** and select the installed Quartus tools folder when needed.
3. Click **Build from OOC**. The viewer exports the currently loaded saved
   circuit and runs Quartus in the background. Read the progress log and wait
   for a successful build before continuing. **Open build folder** opens the
   generated project, reports, and programming image in the build cache.
4. Click **Refresh cables**, then select the board's **USB-Blaster** connection.
   The detected chain must contain the compatible MAX 10 device.
5. Click **Program FPGA** to load the generated `.sof` over JTAG. Wait for the
   operation's success message before testing the LED and audio output.

This viewer profile requires the `HYBRID01` chip name and `MAX10_CLK1_50` input.
Choose any subset of SW0–SW9 inputs and LEDR0–LEDR9 outputs; `AUDIO_OUT` is
optional. Declare at least one switch and one output. Existing four-port
oscillator programs remain supported. This is not a general board or pin
assignment editor. A source change invalidates the previous build; build again
before programming. Saving a file refreshes the viewer but never programs the
FPGA automatically. The `.sof` configures volatile FPGA memory only; it does
not replace the board's power-on flash image.

The viewer keeps generated files in its build cache. The command-line workflow
below instead writes to this project's `build/` folder. Hardware programming
still requires a working cable, driver, and connected board; the existing build
verification described below did not include programming a physical board.

## Pin assignments and wiring

| OOC port | FPGA pin | Board connection |
| --- | --- | --- |
| `MAX10_CLK1_50` | P11 | Onboard 50 MHz oscillator |
| `SW0` | C10 | Slide switch 0 |
| `SW1` | C11 | Slide switch 1 |
| `SW2` | D12 | Slide switch 2 |
| `SW3` | C12 | Slide switch 3 |
| `SW4` | A12 | Slide switch 4 |
| `SW5` | B12 | Slide switch 5 |
| `SW6` | A13 | Slide switch 6 |
| `SW7` | A14 | Slide switch 7 |
| `SW8` | B14 | Slide switch 8 |
| `SW9` | F15 | Slide switch 9 |
| `LEDR0` | A8 | Red LED 0 |
| `LEDR1` | A9 | Red LED 1 |
| `LEDR2` | A10 | Red LED 2 |
| `LEDR3` | B10 | Red LED 3 |
| `LEDR4` | D13 | Red LED 4 |
| `LEDR5` | C13 | Red LED 5 |
| `LEDR6` | E14 | Red LED 6 |
| `LEDR7` | D14 | Red LED 7 |
| `LEDR8` | A11 | Red LED 8 |
| `LEDR9` | B11 | Red LED 9 |
| `AUDIO_OUT` | V10 | GPIO0, JP1 **physical pin 1** |

All supported ports use **3.3-V LVTTL**. Only declared ports are assigned in a
generated project. These assignments come from Terasic's
[DE10-Lite User Manual, June 2020](https://www.mouser.com/datasheet/2/598/DE10-Lite_User_Manual-1100361.pdf),
Tables 3-2, 3-4, 3-5 and 3-7. Figure 3-18 shows the physical JP1 header numbering.

Disconnect board power while attaching the piezo:

```text
JP1 pin 1 (GPIO0) ---- 1 kΩ resistor ---- passive piezo ---- JP1 pin 12 (GND)
```

Identify pin 1 using the board/header marking. JP1 pin 30 is also GND;
pin 11 is **5 V** and pin 29 is **3.3 V**, not ground. V10 is the FPGA package
pin name, not a physical header position. Use a passive piezo, not headphones
or a low-impedance speaker. An active buzzer generates its own pitch and will
not reproduce this oscillator. The LED can be tested with no piezo connected.

## Build from OOC

From the repository root in PowerShell, with the existing OOC environment:

```powershell
.\fpga\de10_lite\build.ps1 -QuartusBin C:\altera_lite\25.1std\quartus\bin64
```

The default source remains `examples/digital/HYBRID01.ckt`. To build the ten-pair
switch example using the same board profile:

```powershell
.\fpga\de10_lite\build.ps1 -Source .\examples\digital\de10_switch_led.ckt -QuartusBin C:\altera_lite\25.1std\quartus\bin64
```

Use your own Quartus `bin64` folder if different. If Quartus is on PATH, omit
`-QuartusBin`. The script uses `.venv/Scripts/python.exe` when present, otherwise
`python`; `-Python` selects another interpreter with OOC available. It does not
install software, change drivers, or program hardware.

Each export calls the same DE10-Lite compiler as the viewer and writes the
generated HDL plus matching `HYBRID01.qsf` and `HYBRID01.sdc` **before** running
Quartus. The checked-in QSF/SDC describe the original oscillator's explicit
synchronizer; both files are regenerated for the selected source on every
build, including `-ExportOnly`. Make profile changes in OOC's board compiler
instead of relying on edits to these regenerated files.

To export only, then compile interactively:

```powershell
.\fpga\de10_lite\build.ps1 -ExportOnly
```

Open `HYBRID01.qpf` in Quartus and choose **Processing → Start Compilation**.
Re-export after changing the OOC source; Quartus cannot compile `.ckt` directly.
After a successful build the temporary programming image is
`build/output_files/HYBRID01.sof`.

## Clock and synchronization

`HYBRID01.sdc` defines the onboard oscillator as a **20 ns / 50 MHz** clock
and derives device clock uncertainty. The OOC `clock(..., 50MHz)` annotation
and this board constraint must agree; neither changes the physical oscillator.

Each slide switch is asynchronous. The generated constraints cut only each
switch's path to its first synchronizer register and preserve timing between
the two stages at 50 MHz. The QSF preserves and identifies both registers of
each chain. For the original oscillator's SW0 path, these are the explicit
`sw_meta` and `sw_sync` states; direct switch use gets automatically generated
registers and matching constraints. The circuit synchronizes individual switch
bits but does not debounce them or capture them as an atomic bus.

The onboard LEDs and passive piezo do not have a receiving clock, so only their
external output paths are false-pathed. Internal phase, timer and synchronizer
paths remain constrained. These output exceptions would need revisiting if
you replace the piezo with a synchronous digital peripheral. There are no
generated audio clocks: `AUDIO_OUT` is data from the phase accumulator.

Check the Fitter and Timing Analyzer reports in `build/output_files` before
programming. A build completing is not permission to ignore critical warnings
or negative setup/hold slack.

The November 16, 2026 build with Quartus Prime Lite 25.1std.0 completed the full
compile (including fitting, assembly and timing): **0 errors, 2 warnings**.
Worst setup slack was **+14.706 ns**, worst hold slack **+0.148 ns**, and all
reported endpoint TNS values were zero. Timing Analyzer reported zero
unconstrained input/output ports or paths. The remaining warnings were the
Lite-edition LogicLock license notice and the generic MAX 10 3.3 V input
interface notice (AN 447); the input pins are the board's documented onboard
oscillator and slide switch. The generated `.sof` was not programmed onto a
board during this build verification.

## Program the FPGA temporarily

1. Connect the board's onboard USB-Blaster **USB Type-B port J3** to the PC
   with a data-capable USB cable. Check the board's power-good indicator.
2. Open `HYBRID01.qpf` in Quartus and select **Tools → Programmer**.
3. Select **Hardware Setup → USB-Blaster [USB-0] → Close**. Set mode to **JTAG**.
4. Click **Auto Detect**. The FPGA should appear as **10M50DA** (the programmer
   may omit the full package/speed-grade suffix). If asked to select a device,
   select the matching MAX 10 device.
5. Right-click that FPGA and choose **Change File**, then select
   `build/output_files/HYBRID01.sof`. If there is no existing device row,
   **Add File** can add the `.sof`; do not add a second copy of the FPGA row.
6. Check **Program/Configure** for that row and click **Start**. Wait for
   **100% (Successful)**.
7. For the ten-switch example, each LED follows its matching switch. For the
   original oscillator example, LED0 is off for about half a second and on for
   half a second; SW0 low selects approximately 440 Hz and high selects 880 Hz.

These JTAG steps follow the manufacturer's manual, pages 18–22. Use the `.sof`
for this first test. It configures volatile FPGA memory and does **not** replace
the power-on flash image. After power cycling, the board loads its previously
stored flash design. Persistent `.pof` flash programming is a separate step.

If USB-Blaster is missing or Auto Detect fails, check the cable and board power,
close other programmer/control-panel sessions, and inspect Windows Device
Manager. A missing USB-Blaster driver can be installed by pointing Device
Manager's driver update at the Quartus `drivers/usb-blaster` directory for your
installation. Do not change the FPGA model to get around a detection error.

## After changing the code

Use **FPGA… → Build from OOC** again, wait for completion, then click **Program
FPGA**. Alternatively, run the build script again and load the newly generated
`.sof` in Quartus Programmer. Saving OOC or refreshing the schematic does not
update the FPGA by itself.
