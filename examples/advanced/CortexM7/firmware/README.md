# Cortex-M7 C firmware

The reset vector supplies an initial MSP of `0x20000400` and the Thumb address of `Reset_Handler`. The handler calls `firmware_main` and waits in a self-branch when it returns. The C code makes two nested, non-inlined calls and writes the result, 13, to the first word of SRAM at `0x20000000`.

The bundled [CortexM7_Firmware.ckt](../CortexM7_Firmware.ckt) includes the editable `arm` block in [ThumbBoot.ckt](images/ThumbBoot.ckt). Compile the entry circuit after editing the boot program. The build commands below generate ROMs from C and linked binaries.

From the repository root, build with a GNU Arm toolchain on PATH.

```powershell
.\.venv\Scripts\python.exe .\examples\advanced\CortexM7\firmware\build_firmware.py
```

Add `--zig PATH_TO_ZIG` to use the Clang and LLD tools bundled with Zig 0.15.2. Use `--tool-prefix PATH_TO_ARM_PREFIX` for a GNU Arm toolchain outside PATH.

The build prints the path to `firmware.rom.ckt`. It also leaves the linked ELF, flat binary, and link map in the firmware `build` directory. GNU Arm builds include a linked disassembly. Zig builds include the compiler's C assembly listings.

Add `--systick` to build the interrupt example in `build/systick`. Its vector table points exception 15 to `SysTick_Handler`. The C setup writes a reload value of 31, clears the current counter, and enables the timer and its interrupt on the core clock. Each interrupt increments the SRAM word at `0x20000004`.

Use `--large` for the C startup example with 4 KiB flash and 4 KiB SRAM. This PowerShell command builds the image and produces a standalone circuit for the viewer.

```powershell
$firmware = ".\examples\advanced\CortexM7\firmware"
.\.venv\Scripts\python.exe "$firmware\build_firmware.py" --large --circuit-output "$firmware\build\large\CortexM7_Large.ckt"
```

Add `--zig PATH_TO_ZIG` to use the compiler verified for the documented step counts. The larger image contains a sample table in flash and the load image for the initialized C globals. The linker map shows the flash source and SRAM destination of `.data` and the `.bss` range.

Convert an existing flat binary with `--binary PATH_TO_BINARY` and select the corresponding profile. To update an existing circuit, add `--replace PATH_TO_CKT`. This replaces the contents between one existing `// BEGIN LINKED FIRMWARE` and `// END LINKED FIRMWARE` marker pair, including any inline assembly there. Use `--circuit-output` with a separate filename to keep your editable boot program. The converter validates the boot vectors and the selected profile's flash capacity before writing.

Different compiler versions can emit different Thumb instructions. Check the listings against the simulator's decoder before replacing a known working ROM.

## Build the optimized hard-float example

The sensor calibration workload builds C with Cortex-M7 code generation at `-O2` and the FPv5-SP-D16 hard-float ABI. The startup enables the FPU before the compiled function enters. C code calls a helper with FP arguments, then uses multiply, divide, comparison, integer conversions and unsigned integer division to process four channels.

```powershell
$firmware = ".\examples\advanced\CortexM7\firmware"
.\.venv\Scripts\python.exe "$firmware\build_firmware.py" --hardfloat --zig PATH_TO_ZIG --circuit-output "$firmware\build\hardfloat\CortexM7_HardFloat.ckt"
```

Use Zig 0.15.2 for the verified 680-byte firmware. To build the same circuit from the checked-in firmware, replace `--zig PATH_TO_ZIG` with `--binary .\tests\fixtures\digital\cortex_m7_hardfloat.bin`.

Open the circuit in **Logic simulation**. Step through the work and watch `RAM5` count the channels. When the program returns to its idle loop at `PC = 0x40`, `RAM0 = 98` is the checksum and `RAM1 = 9` marks channels 0 and 3 as meeting their thresholds. `RAM2 = 0x41A80000` and `RAM3 = 0x40A80000` are the binary32 values 21.0 and 5.25 from the last channel. `RAM4 = 8` is the sum of the signed conversions and `RAM5 = 4` confirms all four channels ran.

## Build the two-task scheduler

From the repository root, generate a standalone circuit with `--rtos`. Use `--zig PATH_TO_ZIG` with Zig 0.15.2 for the verified image and step counts.

```powershell
$firmware = ".\examples\advanced\CortexM7\firmware"
.\.venv\Scripts\python.exe "$firmware\build_firmware.py" --rtos --circuit-output "$firmware\build\rtos\CortexM7_RTOS.ckt"
```

`startup_rtos.S` builds two initial task contexts and implements the SVC launch and PendSV switch. `rtos.c` provides the task workload and SysTick handler. The first task increments its SRAM counter by one, and the second increments its own counter by three. They share an instruction image and maintain separate stacks. See the [viewer walkthrough](../README.md#step-through-two-scheduled-tasks) for the probes and checkpoints.
