# Cortex-M7 in OOC

[CortexM7_Hierarchy.ckt](CortexM7_Hierarchy.ckt) opens the working CPU, register file, ROM, RAM and system-control hierarchy. It uses the same boot program as [CortexM7_Firmware.ckt](CortexM7_Firmware.ckt). Choose **Logic simulation**, then **Step** or **Run**; the program writes `13` to `RAM0`.

The [FreeRTOS demos](firmware/freertos/README.md) boot the upstream V11.1.0 kernel. Watch the Cortex-M7 port switch integer and floating-point tasks through SysTick and PendSV, or pulse IRQ0 to wake a task waiting on a queue.

The [Daisy Seed STM32H750 boot demo](firmware/stm32h750/README.md) uses ST's internal-flash linker script and startup. Step from the hardware reset vector to C main and watch the result appear in SRAM.

The [Daisy Seed HAL blink demo](DaisySeed_STM32H750_HAL_Blink.ckt) configures PLL1 for a 400 MHz core clock and toggles the onboard LED through GPIOC pin 7. Watch the live `BOARD_LED` output in **Logic simulation**.

The [libDaisy AudioPassthru demo](firmware/stm32h750/libdaisy_audio/README.md) initializes QSPI, SDRAM, SAI and circular DMA. Inspect the input and output PCM words in the viewer.

The [SPI and I2C HAL demo](firmware/stm32h750/hal_serial/README.md) exercises
SPI1 loopback and independent I2C1/I2C2 register devices using polling and
interrupts. Open [DaisySeed_SPI_I2C.ckt](DaisySeed_SPI_I2C.ckt) to inspect
transmitted and received values, transfer counts and controller status.

## Source folders

Open an entry circuit such as [CortexM7_Firmware.ckt](CortexM7_Firmware.ckt) or [DaisySeed_AudioPassthru.ckt](DaisySeed_AudioPassthru.ckt). Its `include(...)` statements connect the shared implementation files.

Run the entry files in this folder to execute the connected design. The `core/` and `boards/` files are reusable libraries. Structural library fragments open as interface previews; inspect their live internals through a running entry's hierarchy.

| Location | Contents |
| --- | --- |
| `core/thumb` | Integer instruction decode, arithmetic, DSP and memory operations. |
| `core/floating_point` | Floating-point registers, decoding and arithmetic. |
| `core/system` | System registers, interrupts, memory protection and cache control registers. |
| `core/memory` | SRAM, flash fetch and memory bus logic. |
| `core/execution.ckt` | CPU sequencing and exception entry and return. |
| `boards` | Board-specific memory maps and viewer probes. |
| `firmware/images` | Editable boot assembly and generated firmware ROMs. |
| `firmware/stm32h750` | GPIO, RCC, QSPI, SDRAM, timer and audio peripherals. |

The boot, HAL blink and audio entries share these modules. Builders produce include-based entries by default; use `--standalone` when a single portable file is needed.

Declarations use `input { ... }`, `output { ... }` and `wire { ... }` groups.
The CPU groups have names such as `memory_bus`, `interrupts`, `fault_handling`
and `system_control`. The entry circuits keep
their long bus connections in local `memory_pins[93:0]` and
`core_pins[111:0]` argument lists, then pass those lists to the memory and CPU
chips. The range counts are checked before connecting any signals.
Generated names use `*{i}` interpolation (the original `${i}` also works).
Arithmetic stages, register probes, memory lanes and repeated peripheral
instances use static loops; each generated chip remains individually
inspectable. ROM images reuse one bank implementation, and the builders
preserve this source style when regenerating an entry.

Repeated FP decoder fields use chip-local `int` constants. Reset code uses
`clear { ... };` for register groups and whole state arrays. Those reset
conversions produce exactly the same generated HDL in all five tested entry
circuits. The editor can fold `memory_pins` and `core_pins` independently.

## Inspect generated SRAM pages

The Daisy platform files define a `M7FirmwareMemoryBank` containing a chip-local `for` loop. Names such as `sram${i}_mapped` and `sram${i}_words` expand at compile time. Open **M7FirmwareMemory → M7FirmwarePlatform → M7FirmwareMemoryBank** in the running hierarchy, then select a `M7FirmwareMemoryWindow` to inspect its page and stored words. Every generated window has its own instance and state.

The bank keeps the existing eleven `first_words` probe slices in their original order. CPU data accesses retain priority over DMA. Cross-page reads combine their byte contributions, while unmapped or incomplete transfers retain the existing fault behavior.

## External interrupt inputs

`IRQ_LINES[240]` exposes external IRQ0 through IRQ239. Bit `n` maps to exception
`n + 16` and its vector-table entry. The NVIC has eight enable/pending/active
register banks, 240 independently programmable priority bytes (four implemented
priority bits), and software triggering through STIR. PRIGROUP, BASEPRI,
PRIMASK and FAULTMASK control selection and preemption. Existing `IRQ0` and
`IRQ1` buttons and probes remain available; Daisy audio uses their existing
aliases for DMA1 streams at IRQ11 and IRQ12.

In a running board hierarchy, open **M7FirmwareMemory → M7FirmwareNVIC** to
inspect the pending and active masks and priority groups. IRQ handlers use the
same stack entry and return path as the existing firmware and RTOS examples.

## Run optimized hard-float C

The [hard-float firmware](firmware/README.md#build-the-optimized-hard-float-example) runs four calibration channels from the reset vector. The compiler emits wide Thumb arithmetic, hardware integer division, and single-precision instructions for arithmetic, comparisons, and conversions. In **Logic simulation**, `RAM5` counts the channels. The finished run leaves checksum `98` in `RAM0` and the threshold mask `9` in `RAM1`.

## Step through two scheduled tasks

The [RTOS firmware](firmware/README.md#build-the-two-task-scheduler) runs two compiled C tasks with separate process stacks. `SVC #0` starts task A. Each SysTick handler sets PendSV pending through ICSR, then returns. PendSV saves R4–R11 and the outgoing PSP, restores the other task's registers and PSP, and returns through that task's hardware frame.

Open `CortexM7_RTOS.ckt` in **Logic simulation** and click **Reset** with the `RESET` input low. Use **Run** and **Pause** to reach the switches, or **Step** to inspect a stack transfer. Watch `IPSR`, `SP`, `PSP`, `PENDSV_PENDING`, and `RAM3`. When `IPSR = 0`, `RAM3 = 0` means task A is running and `RAM3 = 1` means task B is running. `RAM0` and `RAM1` are their counters. `RAM2` counts switches, and `RAM4` and `RAM5` hold saved software frame pointers.

| Step | What to watch in the verified build |
| --- | --- |
| 100 | `IPSR = 11` on SVC entry. The boot frame is on MSP. |
| 129 | Task A starts with `PSP = 0x20000800`. |
| 292 | `IPSR = 15` in SysTick, the task frame is on PSP, and the handler runs on MSP. |
| 314 | `IPSR = 14` in PendSV. The scheduler saves task A and selects task B. |
| 365 | Task B starts with `PSP = 0x20000C00`. |
| 556 | Task A resumes with its own stack. Both task counters have advanced. |

Open **M7FirmwareMemory → M7FirmwareSysTick** for the timer, **M7FirmwareMemory → M7FirmwareSCB** for ICSR and priorities, or **M7FirmwareCore → M7SystemRegisters** for stack selection. The demo uses basic integer frames and the model's own clock timing. [Cortex-M7 programming manual](https://www.st.com/resource/en/programming_manual/DM00237416.pdf)

## Boot the larger C program

The larger example boots a 444-byte linked image in the 4 KiB flash and SRAM model. The reset handler copies three initialized `.data` words from flash and clears two `.bss` words in SRAM. The C program calibrates 64 flash samples through nested calls and leaves the checksum `0x123493EB` in `RAM0` and the sample count `64` in `RAM1`. The program reaches its idle loop at `PC = 0x30` on step 1609 with `MSP = 0x20001000` and `FAULT = 0` in the verified build.

The [firmware build guide](firmware/README.md) shows how to generate the standalone `CortexM7_Large.ckt`. Open it in **Logic simulation** and watch `BUS_ADDR`, `BUS_WRITE`, and `BUS_WDATA` through startup. The `.data` stores begin at `0x20000008`, followed by zero writes at `0x20000014` and `0x20000018`. Use **Run** to reach the checksum or **Step** to inspect each transfer. This program exercises register-offset `LDR` and a 32-bit `CMP.W` with a modified immediate.

## Read and edit the boot program

[CortexM7_Firmware.ckt](CortexM7_Firmware.ckt) connects the CPU modules and boots the program in [ThumbBoot.ckt](firmware/images/ThumbBoot.ckt). Edit the `arm(address, word, valid)` instructions in that image, then compile the entry circuit to assemble the ROM. Branches and literal loads use labels. The block reproduces the original 52-byte [linked C image](firmware/README.md).

The reset handler calls `firmware_main`, which calls `nested_add(4)`. That function calls `leaf_add(4)` and adds 2 to its result. The program writes 13 to `RAM0` at `0x20000000`, restores `MSP` to `0x20000400`, and idles in the reset handler.

Open the file with **OOC: Run Circuit / Open Schematic**. In **Logic simulation**, click **Reset**, leave the `RESET` input low, and press **Step**. The first two clicks load MSP and the reset entry. Watch `PC`, `IR`, `MSP`, `LR`, and `RAM0`. `WIDE` identifies a 32-bit instruction, with its first halfword in the low 16 bits of `IR`. `LAST_PC`, `LAST_IR`, and `RETIRED` update when an instruction completes. Each stack word uses its own clock, so you can watch `BUS_ADDR` and the data throughout `PUSH` and `POP`.

| Step | What to watch in the bundled image |
| --- | --- |
| 2 | `MSP = 0x20000400`, `PC = 0x08`, and `BOOTED = 1`. |
| 12 | The third `BL` enters `leaf_add`. `MSP = 0x200003F0` and `LR = 0x2B`. |
| 20 | The store sets `RAM0 = 13`. |
| 24 | `PC = 0x0C` in the idle loop, `MSP = 0x20000400`, and `FAULT = 0`. |

Open **M7FirmwareCore** to inspect the decoder, ALU, and register file. Expand **M7FirmwareFlash** for the ROM or **M7FirmwareMemory → M7FirmwarePlatform → M7FirmwareRAM** for SRAM pages and banks. The first bank of the first page contains the result. The small program's stack is at the top of the first page, and the larger program's stack is at the top of the fourth page.

The core supports mixed-width Thumb fetches, an integer Thumb subset with IT blocks, direct calls and register returns, register-list transfers, and naturally aligned byte, halfword, and word transfers. `QADD` and `QSUB` clamp signed 32-bit results and set the sticky saturation flag in APSR.Q. SRAM is 4 KiB starting at `0x20000000`. A valid HardFault vector can handle and recover from a fault. If the model halts, `FAULT_CODE` reports the cause. The fault codes below also apply here. An even branch target from `BX` or `POP` reports code 5. Invalid exception vectors and invalid stacked status on exception return report code 6. An SVC that cannot activate reports code 7.

## Step a SysTick interrupt

Build the [alternate image](firmware/README.md) with `--systick --circuit-output` to keep the default boot program intact. The C program enables SysTick on the processor clock. Its handler increments `RAM1` at `0x20000004`. In the viewer, open **M7FirmwareMemory → M7FirmwareSysTick** to watch the counter, reload value, pending flag, and acknowledgement.

With the bundled build settings, the first interrupt enters at step 56. `IRQ_ACTIVE` goes high, `PC = 0x70`, and the eight-word stack frame lowers `MSP` to `0x200003E0`. At step 69, `RAM1 = 1`, the stack returns to `0x20000400`, and execution resumes at `PC = 0x44`. The second return leaves `RAM1 = 2`.

## Inspect system registers

Open **M7FirmwareCore → M7SystemRegisters** to watch special-register access. `MRS` and `MSR` implement the model's status registers, MSP/PSP, `CONTROL`, and interrupt masks. `CPSID` and `CPSIE` also change the interrupt and fault masks in privileged mode. The modeled SCB registers expose ICSR, VTOR, and four priority bits each for SVC, PendSV, and SysTick. An interrupt can become pending while masked and enter as soon as it is eligible.

The exception path supports Thread mode using MSP or PSP with basic integer and extended FP frames. The handler executes on MSP and can return to the process stack. IRQ0 and IRQ1 have independent enable, pending, active, and priority bits. `AIRCR.PRIGROUP` splits the implemented priority bits. Group priority controls preemption and `BASEPRI` masking. Subpriority orders pending exceptions in the same group. Equal group priority and subpriority are resolved by the lower exception number. Watch `EXCEPTION_DEPTH` and `IPSR` through the nested return, and inspect **M7FirmwareMemory → M7FirmwareNVIC** for both IRQs. [Arm exception priorities and preemption](https://support.arm.com/documentation/ddi0403/d/System-Level-Architecture/System-Level-Programmers--Model/ARMv7-M-exception-model/Exception-priorities-and-preemption)

Pulse the `NMI` input or set `ICSR.NMIPENDSET` from privileged firmware to pend exception 2. The model latches a rising edge and services the request at an instruction boundary. NMI can preempt HardFault and configurable handlers even while the priority masks are set. Watch `NMI_PENDING` during request handling and `NMI_ACTIVE` through the return. Firmware writes the `0x05FA` key when updating `AIRCR.PRIGROUP`. The register reads back with `0xFA05` in the key field. [Arm Cortex-M7 exception model](https://support.arm.com/documentation/dui0646/c/The-Cortex-M7-Processor/Exception-model/Exception-types?lang=en)

## Boot CMSIS and exercise interrupts and faults

The CMSIS workload enters through Arm's generic ARMCM7 startup and runs two tasks using the upstream FreeRTOS Cortex-M7 port. Task A triggers UsageFault, an MPU-denied read, and a read from an unmapped address. Watch `IPSR` for the three handlers (6, 4, and 5). They record fault status and addresses in `RAM5`–`RAM9` and advance the stacked PC. `RAM2 = 2` confirms Task A returned from all three handlers. The following interrupt tests use `PRIGROUP`, IRQ0, IRQ1, and NMI. Watch `RAM3`, `RAM4`, and `RAM10` for handler counts and `RAM0` and `RAM1` for the task counters. [Demo steps for NMI and priority grouping](firmware/freertos/README.md#step-nmi-and-priority-grouping)

## Inspect floating-point state

Open **M7FirmwareCore → M7FPRegisters** for the 32 single-precision registers. `S0`, `S15`, `S16`, `S31`, and `FPSCR` are also top-level probes. Single-precision support includes `VADD`, `VSUB`, `VMUL`, `VNMUL`, `VDIV`, `VSQRT`, `VCMP`, `VCMPE`, conversions between 32-bit integers and floats, `VABS`, and `VNEG`. `VMLA`, `VMLS`, `VNMLA`, and `VNMLS` round the product and sum separately. `VFMA`, `VFMS`, `VFNMA`, and `VFNMS` read the old destination and round the combined result once. Square root and fused operations use the FPSCR rounding, default-NaN, and flush-to-zero modes and update the cumulative exception flags.

`VMRS APSR_nzcv, FPSCR` makes the comparison flags available to a following branch or IT block. Register moves, scalar loads and stores, and FP stack transfers support the compiled C and FreeRTOS examples.

`CONTROL[2]` marks an active floating-point context. An exception reserves a 104-byte extended frame for that context before any alignment padding. With lazy stacking enabled, `FP_LAZY_ACTIVE` goes high and `FPCAR` identifies the reserved space for the lower FP registers. Step an FP instruction in the handler to see the deferred writes, or return through a basic nested handler and observe the older deferred frame stay pending. `FPDSCR` supplies mode defaults for the first FP instruction in an automatically activated context. [Armv7-M Architecture Reference Manual](https://developer.arm.com/documentation/ddi0403/latest/)

## Inspect integer and DSP instructions

The model handles Thumb-2 shifted and immediate arithmetic, bitfield operations, reversal, integer division, indexed and doubleword loads and stores, and table branches. The exclusive monitor covers byte, halfword, and word operations in the single-core SRAM model. `CLREX` and exception entry clear its reservation. Integer division by zero returns zero at reset. Setting `CCR.DIV_0_TRP` enables the trap and preserves the destination register when it fires.

Watch `SLEEPING` and `EVENT_LATCH` while stepping `WFI`, `WFE`, and `SEV`. SysTick, IRQ0, IRQ1, and NMI can wake the core. The System Control Register models event-on-pending and sleep-on-exit, and retains the deep-sleep bit for readback. `YIELD`, `PLD`, and `PLI` retire without bus accesses in this single-core model without caches.

Open **M7FirmwareCore → M7DSPExecute** to inspect packed lane operations, saturation, `SEL`, packing and extension, and short and long multiply-accumulate instructions. These operations expose the result and the individual updates to APSR.Q and APSR.GE. The firmware core implements an architectural subset with FPv5 single precision.

## Inspect memory protection and faults

Open **M7FirmwareMemory → M7FirmwareMPU** for the eight programmable regions. The MPU starts disabled. When enabled, it checks execute-never and access permissions using the highest matching region, with subregion masks and an optional privileged background map. Watch `FETCH_ALLOWED` and `DATA_ALLOWED` to see the checks. Precise MPU, bus, and usage faults enter vectors 4, 5, and 6 when enabled in `SHCSR` and eligible at the current priority. Disabled or priority-blocked faults escalate to HardFault. Watch `CFSR` for the cause and address-valid bits, `MMFAR` for a denied data address, and `BFAR` for a precise data bus error. The live active flags are `MEMMANAGE_ACTIVE`, `BUSFAULT_ACTIVE`, and `USAGEFAULT_ACTIVE`. [Armv7-M Architecture Reference Manual](https://developer.arm.com/documentation/ddi0403/latest/)

## Step through the original 16-bit demo

[CortexM7_ThumbDemo.ckt](CortexM7_ThumbDemo.ckt) is an OOC model of a small, 16-bit Thumb instruction subset. The demo reads its reset vectors from flash, executes a loop, then stores a word to RAM and reads it back.

The flash chip uses inline Thumb assembly. In `arm(address, word, valid)`, `address` is a byte address and `word` is the 32-bit little-endian read result. The optional `valid` output is high for aligned addresses inside the image. Use labels for branches and PC-relative literal loads, and `.word` for vectors and data. Addresses are assigned from zero. Append `+ 1` to a vector label to set the Thumb state bit.

```ckt
arm(address, word, valid) {
    .word 0x20000040;
    .word reset + 1;
reset:
    MOVS r0, #5;
idle:
    B idle;
}
```

OOC assembles the ROM during compile and Verilog export. Use `//` for line comments inside the block, where `#` prefixes an immediate. The built-in assembler supports common moves, arithmetic, loads, stores, branches and register lists, plus single-precision FP and selected DSP forms. Use `.align 2` to align a literal pool to four bytes. The firmware-image import accepts binaries from an Arm toolchain for larger C or assembly projects.

Open the demo using **OOC: Run Circuit / Open Schematic**, then select **Logic simulation**. Click **Reset** and leave the `RESET` input low. Each click of **Step** advances one clock cycle.

| Step | What to watch |
| --- | --- |
| 1 | `MSP` becomes `0x20000040` from the vector at address `0x00`. |
| 2 | `PC` becomes `0x08` and `BOOTED` goes high. The vector at `0x04` contains `0x09`, including the Thumb state bit. |
| 3–5 | The literal load sets `R1` to `0x20000000`. The two following moves set `R0 = 5` and `R2 = 3`. |
| 6–14 | `ADDS` and `SUBS` run three times. `BNE` jumps back twice, then falls through with `R2 = 0` and `R0 = 11`. |
| 15–16 | `CMP` leaves `NZCV = 0b0110` and `BEQ` jumps from `0x16` to `0x1C`, skipping the instruction that would set `R0` to 255. |
| 17–18 | `STR` sets `RAM0 = 11` at `0x20000000`. The following `LDR` sets `R3 = 11`. |
| 19 onward | `PC` reaches the self-branch at `0x22`. `FAULT` stays low. |

`PC` and `IR` show the instruction about to execute. `LAST_PC` and `LAST_IR` show the most recently completed instruction.

Click the **CORTEX_M7_THUMB_DEMO** package in the logic diagram to open its blocks. Inside **M7ThumbCore**, open **M7ThumbRegisters** for the register file, **M7ThumbALU** for arithmetic and flags, or **M7ThumbDecode** for the decoded instruction. Open **M7DemoFlash** for the ROM or **M7DemoMemory → M7DemoRAM** for the RAM. You can keep stepping while a block is open. **Signals** and **Probe** show the values in the selected block.

## Model scope

The typed top-level ports expose the bus, PC, MSP, LR, R0–R12, and the four NZCV flags. The register file is a 13-word OOC array and RAM is a 16-word OOC array at `0x20000000`–`0x2000003F`. The flash table contains the two reset vectors, the program, and its RAM base literal.

The 16-bit instruction subset includes `MOVS`, `ADDS`, `SUBS`, and `CMP` with immediate operands, `LDR` from a PC-relative literal, `LDR` and `STR` with a base register and immediate offset, conditional and unconditional branches, and `NOP`. These encodings access R0–R7. Word accesses in the demo require four-byte alignment. A fault stops execution until reset, with `FAULT_CODE` identifying the cause.

| Code | Cause |
| --- | --- |
| 1 | Invalid reset-vector read or missing Thumb state bit |
| 2 | Invalid instruction fetch address |
| 3 | Unsupported instruction |
| 4 | Invalid data access |

The model executes one instruction per simulated clock after boot. Its timing, small memory map, and reset behavior for the teaching arrays are specific to the demo. Full Thumb-2, exception handling, on-chip peripherals, and hardware timing are outside its scope. [ST Cortex-M7 programming manual](https://www.st.com/resource/en/programming_manual/DM00237416.pdf)
