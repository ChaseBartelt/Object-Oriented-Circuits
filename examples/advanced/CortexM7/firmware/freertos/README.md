# FreeRTOS on the OOC Cortex-M7 model

This demo runs the unmodified FreeRTOS Kernel V11.1.0 and its GCC ARM_CM3 port on the model's integer Thumb instruction set. The application creates two equal-priority tasks with static storage. The kernel also creates Idle. SysTick drives the upstream tick handler and PendSV runs the upstream scheduler and context switch. Task A adds one to `RAM0` and task B adds three to `RAM1`. [FreeRTOS V11.1.0 ARM_CM3 port](https://github.com/FreeRTOS/FreeRTOS-Kernel/blob/V11.1.0/portable/GCC/ARM_CM3/port.c)

## Open the demo

The test fixture includes the verified 1,640-byte image. From the repository root, generate a standalone viewer document with that image.

```powershell
$firmware = ".\examples\advanced\CortexM7\firmware"
.\.venv\Scripts\python.exe "$firmware\build_firmware.py" --rtos --binary ".\tests\fixtures\digital\freertos_v11_1_0_arm_cm3.bin" --circuit-output ".\CortexM7_FreeRTOS.ckt"
```

Open `CortexM7_FreeRTOS.ckt` in **Logic simulation**, click **Reset**, and leave the `RESET` input low. Use **Run** to reach scheduling, then **Pause** and **Step** to inspect a switch. Watch `IPSR`, `PSP`, `RAM0`, and `RAM1`. The stacks belong to different address ranges, so you can identify the running task from `PSP` when `IPSR = 0`.

| Cycle | Verified checkpoint |
| --- | --- |
| 3251 | The FreeRTOS SVC handler starts with `IPSR = 11`. |
| 3277 | The first application task, B, starts with `PSP = 0x20000348`. |
| 3639 | The FreeRTOS SysTick handler starts with `IPSR = 15`. |
| 3724 | The FreeRTOS PendSV handler starts with `IPSR = 14`. |
| 3806 | Task A first runs with `PSP = 0x20000248`. |
| 5806 | Six switches have completed. `RAM0 = 166` and `RAM1 = 597`. |

## Rebuild from source

Use the official [FreeRTOS Kernel V11.1.0 source release](https://github.com/FreeRTOS/FreeRTOS-Kernel/tree/V11.1.0) and Zig 0.15.2. The build leaves the upstream source files untouched. Clang generates narrow Thumb for the C compiler's own code. The ARMv7-M assembler assembles the port's original inline system and context instructions.

```powershell
.\.venv\Scripts\python.exe "$firmware\freertos\build_freertos.py" --kernel PATH_TO_FREERTOS_KERNEL --zig PATH_TO_ZIG --circuit-output ".\CortexM7_FreeRTOS.ckt"
```

The linked ELF, map, and flat image go under `build/freertos`. The fixture SHA-256 is `7243E1AFD1BDC407D4CA4406AEC57C384C5E4C86198A4B85AD49B3A27F948850`. The [fixture license](../../../../../tests/fixtures/digital/freertos_v11_1_0_LICENSE.md) accompanies the compiled upstream code.

This configuration uses static allocation, preemption, time slicing, and a modeled 25 Hz SysTick. It uses basic integer exception frames. The listed cycles describe the OOC implementation.

## Wake a blocked FreeRTOS task from IRQ0

The blocking workload uses the upstream GCC `ARM_CM7/r0p1` port and integer tasks. One task calls `vTaskDelay(2)`. The other waits on an empty queue with `xQueueReceive`. Its IRQ0 handler sends a value with `xQueueSendFromISR` and requests a context switch through PendSV.

```powershell
.\.venv\Scripts\python.exe "$firmware\build_firmware.py" --rtos --binary ".\tests\fixtures\digital\freertos_v11_1_0_arm_cm7_r0p1_blocking.bin" --circuit-output ".\CortexM7_FreeRTOS_Blocking.ckt"
```

In **Logic simulation**, reset with the inputs low and run until `IRQ0_ENABLED = 1`, `RAM0 >= 2`, and `RAM3 = 1`. Pause, set `IRQ0` high for one **Step**, then set it low. Watch `IPSR` enter IRQ0 (16) and PendSV (14). The receiver stores `1001` in `RAM1` before blocking again. Wait for `RAM3 = 3` and pulse IRQ0 again. The receiver stores `1002` and `RAM2` reaches 2. Holding the IRQ high can trigger it again after exception return.

To compile this workload from source, add `--m7 --blocking` to the [rebuild command](#rebuild-from-source) and change the output filename. The build defaults to `build/freertos-m7-blocking`.

## Try the Cortex-M7 port

The M7 workload links the upstream GCC `ARM_CM7/r0p1` port. Its two tasks use integer arithmetic. The port initializes CPACR and FPCCR and uses each task's EXC_RETURN to skip the conditional floating-point saves and restores for integer frames. [FreeRTOS V11.1.0 Cortex-M7 port](https://github.com/FreeRTOS/FreeRTOS-Kernel/blob/V11.1.0/portable/GCC/ARM_CM7/r0p1/port.c)

```powershell
.\.venv\Scripts\python.exe "$firmware\build_firmware.py" --rtos --binary ".\tests\fixtures\digital\freertos_v11_1_0_arm_cm7_r0p1.bin" --circuit-output ".\CortexM7_FreeRTOS_M7.ckt"
```

Run to around cycle 3300 and step through SVC. In Thread mode, watch `PSP` alternate between `0x20000348` and `0x20000248` across successive PendSV returns while `RAM0` and `RAM1` advance. `CPACR = 0x00F00000` and `FPCCR = 0xC0000000` expose the port's floating-point setup.

To compile the M7 workload from source, add `--m7` to the [rebuild command](#rebuild-from-source) and change the output filename. The build defaults to `build/freertos-m7`.

## Step floating-point context switches

The floating-point workload uses the same upstream M7 port. Both tasks initialize distinct values in `S0`, `S15`, `S16`, `S31`, and `FPSCR`. Each task runs `VADD.F32` once and repeatedly checks that its own registers and sum survived any switches. Before the first FP instruction, each task writes `FPCCR = 0x80000000` to keep automatic preservation enabled and select immediate saves. The core stacks the lower FP registers and `FPSCR`. FreeRTOS saves and restores the upper registers in PendSV.

```powershell
$firmware = ".\examples\advanced\CortexM7\firmware"
.\.venv\Scripts\python.exe "$firmware\build_firmware.py" --rtos --binary ".\tests\fixtures\digital\freertos_v11_1_0_arm_cm7_r0p1_fp.bin" --circuit-output ".\CortexM7_FreeRTOS_FP.ckt"
```

Open the circuit in **Logic simulation**, reset with `RESET` low, and run until both counters have advanced. Pause and step through the next SysTick and PendSV. Watch `IPSR`, `PSP`, `FP_ACTIVE_FRAME`, and `FPSCR`. When `IPSR = 0`, the process stack identifies the running task. The first dispatch of each task uses a basic frame. After FP execution, subsequent interrupt entries from the tasks use an extended frame with `LR = 0xFFFFFFED` at handler entry.

| Task | Thread `PSP` | `S0` | `S15` | `S16` | `S31` | `FPSCR` | Progress and sum |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A | `0x20000250` | 1.0 | 6.0 | 4.0 | 7.0 | `1` | `RAM0` counts checks, `RAM3 = 0x40A00000` holds 5.0 |
| B | `0x20000350` | 2.0 | 12.0 | 8.0 | 14.0 | `2` | `RAM1` counts checks, `RAM4 = 0x41200000` holds 10.0 |

The viewer displays the FP registers as raw 32-bit values. `RAM2` stays at zero on success and becomes 3 if a task detects a mismatch.

To rebuild the 1,932-byte image, add `--floating-point` to the [rebuild command](#rebuild-from-source). This selects the M7 port and writes the ELF, link map, and binary to `build/freertos-m7-fp`. The fixture SHA-256 is `C3A80D26E02C31E0E422CC3B1A3E4AC9C99780D7022FAC5C2AE82BEB173F25A0`.

Use `--lazy-fp` to build the same workload with automatic lazy preservation. Set the circuit output to `CortexM7_FreeRTOS_FP_Lazy.ckt`. In PendSV, watch `FP_LAZY_ACTIVE` and `FPCAR`. The low FP registers and `FPSCR` are written at `FPCAR` when PendSV first executes an FP instruction, then the port saves the upper registers. The task checks and viewer probes are the same in both modes.

## Boot CMSIS and recover from configurable faults

This demo links the unmodified GCC startup and `SystemInit` from the Arm CMSIS 5.9.0 ARMCM7 device template, using its single-precision configuration. The reset handler calls `SystemInit`, copies `.data`, clears `.bss` and the result counters, and transfers control to the demo's freestanding `_start`. Two tasks run on the upstream FreeRTOS V11.1.0 Cortex-M7 r0p1 port. The demo supplies the linker script and the IRQ and fault handlers. [CMSIS device template](https://github.com/ARM-software/CMSIS_5/tree/5.9.0/Device/ARM/ARMCM7) · [FreeRTOS Cortex-M7 port](https://github.com/FreeRTOS/FreeRTOS-Kernel/tree/V11.1.0/portable/GCC/ARM_CM7/r0p1)

Generate the viewer circuit from the checked-in image. From the repository root, run

```powershell
$firmware = ".\examples\advanced\CortexM7\firmware"
.\.venv\Scripts\python.exe "$firmware\build_firmware.py" --rtos --binary ".\tests\fixtures\digital\cmsis_5_9_0_freertos_v11_1_0_arm_cm7.bin" --circuit-output ".\CortexM7_CMSIS_FreeRTOS.ckt"
```

Open `CortexM7_CMSIS_FreeRTOS.ckt` in **Logic simulation**. After both tasks start, task A triggers the three faults in order. Each handler advances the stacked process PC past its 16-bit faulting instruction, clears the recorded `CFSR` bits, and returns to task A. `RAM2 = 2` means all three returned. Both task counters in `RAM0` and `RAM1` then advance, with `CFSR = 0` and `HFSR = 0`.

| Fault | `IPSR` | Trigger | Saved status and address |
| --- | --- | --- | --- |
| UsageFault | 6 | Undefined instruction | `RAM5 = 0x10000` (`UNDEFINSTR`) |
| MemManage | 4 | Read from the MPU's no-access SRAM region | `RAM6 = 0x82` (`DACCVIOL` and `MMARVALID`), `RAM7 = 0x20000040` (`MMFAR`) |
| BusFault | 5 | Read from an unmapped peripheral address | `RAM8 = 0x8200` (`PRECISERR` and `BFARVALID`), `RAM9 = 0x40000000` (`BFAR`) |

### Step NMI and priority grouping

Run until `RAM2 = 2` and pause in Thread mode with `IPSR = 0`. To pulse an input, set it high for one step and bring it back low. Watch `IPSR`, `EXCEPTION_DEPTH`, and `PRIGROUP`.

1. The firmware uses CMSIS to select `PRIGROUP = 5` and encode IRQ0 with group 3 and subpriority 0, and IRQ1 with group 2 and subpriority 1. Pulse `IRQ0`. When `IPSR = 16`, pulse `IRQ1` and step until `IPSR = 17`. IRQ1 preempts because it has the higher group priority. Pulse `NMI` to enter exception 2 at depth three. The return order is IRQ1, IRQ0, then Thread mode. `RAM10` counts NMIs. `RAM3` and `RAM4` count IRQ0 and IRQ1.
2. Run until `RAM2 = 3` and pause in Thread mode. The firmware sets priorities `0xA0` and `0x80` for IRQ0 and IRQ1, placing both in preemption group 2. Pulse `IRQ0` and wait for `IPSR = 16`, then pulse `IRQ1`. `IRQ1_PENDING` stays high through the rest of IRQ0. Step until IRQ0 has returned and IRQ1 runs.
3. After IRQ1 returns, pulse both inputs on the same step. IRQ1 runs first because it has the higher subpriority. IRQ0 follows. When both counts reach 3, task A restores `PRIGROUP = 0` and sets `RAM2 = 4`. Watch `RAM0` and `RAM1` advance as both tasks continue.

To rebuild, pass the [CMSIS_5 5.9.0 source tree](https://github.com/ARM-software/CMSIS_5/tree/5.9.0) in addition to the pinned FreeRTOS kernel and Zig 0.15.2.

```powershell
.\.venv\Scripts\python.exe "$firmware\freertos\build_freertos.py" --kernel PATH_TO_FREERTOS --cmsis PATH_TO_CMSIS_5 --zig PATH_TO_ZIG --circuit-output ".\CortexM7_CMSIS_FreeRTOS.ckt"
```

The 3,504-byte fixture has SHA-256 `409EDE4CA9E8234B994FE87BCFE261B9B97A646767284E744BCAD3B58F03E214`. CMSIS startup and system code are copyright Arm Limited and distributed under Apache 2.0. FreeRTOS is distributed under the MIT license. Both licenses accompany the binary in the test fixtures.
