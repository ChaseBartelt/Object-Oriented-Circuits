# STM32H750 internal-flash boot

This example targets the Daisy Seed's STM32H750 for an internal-flash boot. The
small C application writes a signature to SRAM so you can follow the write from
the CPU bus to memory in the viewer. It uses the MCU startup and memory layout
from ST. [Daisy Seed hardware](https://docs.daisy.audio/hardware/Seed/) ·
[STM32H750 reference manual](https://www.st.com/resource/en/reference_manual/rm0433-stm32h743-753-and-stm32h750-value-line-advanced-arm-based-32-bit-mcus-stmicroelectronics.pdf)

For stereo audio with SAI and DMA, see the
[libDaisy AudioPassthru demo](libdaisy_audio/README.md).

For SPI1 loopback and I2C register-device transfers with ST HAL, see the
[serial demo](hal_serial/README.md) and open
[DaisySeed_SPI_I2C.ckt](../../DaisySeed_SPI_I2C.ckt). The platform provides SPI1
at `0x40013000` and I2C1/I2C2 at `0x40005400`/`0x40005800`, with independent
RCC clock/reset controls and their NVIC interrupt lines. Each I2C bus has its
own 256-byte test device at seven-bit address `0x50`; other addresses NACK.

The viewer exposes `SPI1_TX`, `SPI1_RX`, `SPI1_TRANSFERS`, `SPI1_STATUS`, and
the same four probes for `I2C1` and `I2C2`. Open **M7FirmwareMemory →
M7FirmwarePlatform → STM32H750Serial** to inspect each controller. SPI status
is its SR register. I2C status packs ISR flags in bits 15:0, the target address
in 22:16, the register pointer in 30:23, and peripheral enable in bit 31.
Serial transfers are modeled at register level with explicit test peers;
serial DMA and physical bus waveforms are outside this example.

## Run the HAL clock and LED demo

Open [DaisySeed_STM32H750_HAL_Blink.ckt](../../DaisySeed_STM32H750_HAL_Blink.ckt)
and choose **Logic simulation**. Select **Reset** and **Run**. You can pause and
step through GPIO writes or select signals to inspect them.

| Signal | What to watch |
| --- | --- |
| `SYSCLK_PLL` | Goes high when RCC switches the core to PLL1. |
| `CLOCK_STATUS` | Changes to 1 when HAL finishes configuring the clock. |
| `CLOCK_HZ` | Shows 400,000,000 Hz as reported by HAL. |
| `BOARD_LED` | Follows GPIOC pin 7, the Daisy Seed's active-high LED. |
| `HAL_TICK`, `LED_TRANSITIONS` | Count SysTick interrupts and completed LED toggles. |

In the supplied build, `CLOCK_STATUS` changes to 1 at step 1007.
`BOARD_LED` turns on at step 1889, off at step 2531, and on again at step
3173.

The firmware uses the same ST startup and linker script as the boot example.
ST HAL starts the 16 MHz HSE, configures PLL1, switches the system clock, and
enables GPIOC. `HAL_GPIO_TogglePin` writes GPIOC's bit set/reset register at
`0x58020818`. The demo shortens the SysTick reload after clock setup so the
blinks are visible while stepping. `CLOCK_HZ` continues to report the
configured clock frequency. [Daisy Seed hardware](https://docs.daisy.audio/hardware/Seed/) ·
[Daisy Seed clock initialization](https://github.com/daisyaudio/libDaisy/blob/master/src/sys/system.cpp) ·
[ST HAL driver](https://github.com/STMicroelectronics/stm32h7xx-hal-driver/tree/v1.11.3)

Generate the circuit from the supplied ELF with this command from the repository
root.

```powershell
$firmware = ".\examples\advanced\CortexM7\firmware\stm32h750"
.\.venv\Scripts\python.exe "$firmware\build_firmware.py" `
    --elf ".\tests\fixtures\digital\stm32h750_hal_blink.elf" `
    --circuit-output ".\build\DaisySeed_STM32H750_HAL_Blink.ckt"
```

To rebuild the ELF, use the build inputs below and add `--hal PATH_TO_ST_HAL`
pointing to ST's HAL driver at `v1.11.3`. Use a separate output directory for
the HAL build.

## Generate the viewer circuit

From the repository root, import the checked-in ELF.

```powershell
$firmware = ".\examples\advanced\CortexM7\firmware\stm32h750"
.\.venv\Scripts\python.exe "$firmware\build_firmware.py" `
    --elf ".\tests\fixtures\digital\stm32h750_stock_boot.elf" `
    --circuit-output ".\build\DaisySeed_STM32H750_Boot.ckt"
```

The loader uses the ELF's physical load addresses for flash contents, including
the `.data` initializers. The startup copies those initializers to the linked
SRAM addresses. The generated circuit reads reset vectors from internal flash
at `0x08000000`.

The generated entry file includes the shared Cortex-M7 modules, a ROM under
`firmware/images`, and a platform definition under `boards`. Use
`--standalone` to generate a single circuit file for sharing.

## Step through reset

Open `DaisySeed_STM32H750_Boot.ckt` with **OOC: Run Circuit / Open Schematic**.
The circuit contains the CPU hierarchy and the linked firmware image. Select
**Logic simulation** and use **Reset** to return to the initial state. Keep
**Outputs** expanded or select a signal in **Signals** to follow it as you
click **Step**.

1. Watch `BUS_ADDR` and `BUS_RDATA` while the core reads the initial stack
   pointer `0x20020000` from `0x08000000` and the reset-handler vector
   `0x08000299` from `0x08000004`. After two steps, `PC = 0x08000298`.
2. Follow `PC` through `Reset_Handler` and `SystemInit`. Startup copies the
   initializers for `.data` from flash to SRAM and clears `.bss`. Look for
   `DATA_VALUE = 0x12345678` and `BSS_VALUE = 0` on entry to `main()`.
3. `AT_MAIN` lights when `PC` reaches `MAIN_ADDRESS`, the entry of `main()`.
   For the supplied ELF this is `0x08000408` at cycle 122.
4. When `BUS_WRITE` asserts at `BUS_ADDR = 0x20000008`, step once more to see
   `BOOT_SIGNATURE = 0x44414953` at cycle 136. `BSS_VALUE` changes to
   `0xA5` as the application runs.

The model covers the memory windows and peripheral register behavior exercised
by this startup. Applications that initialize the Daisy Seed's audio or
external memory use additional board peripherals.

## Rebuild the ELF

Use ST's CMSIS device files at `v1.10.6`, Arm CMSIS at `5.9.0`, and ST's
`STM32H750XBHx_FLASH.ld` from the STM32H750B-DK ExtMem_Boot project in
STM32CubeH7 `v1.10.0`. That script links its own boot image to internal flash.
The build uses ST's original startup assembly and `SystemInit` and supplies
the freestanding C runtime hook in `demo.c`. [ST CMSIS device files](https://github.com/STMicroelectronics/cmsis-device-h7/tree/v1.10.6) ·
[Arm CMSIS](https://github.com/ARM-software/CMSIS_5/tree/5.9.0) ·
[ST internal-flash linker script](https://github.com/STMicroelectronics/STM32CubeH7/blob/v1.10.0/Projects/STM32H750B-DK/Templates/ExtMem_Boot/SW4STM32/STM32H750B_DISCO/STM32H750XBHx_FLASH.ld)

The supplied ELF was built with Zig 0.15.2 and Arm target C headers. Its SHA-256
is `d220819b3fe2686575ccf1dda16a8def458713df40543ca67b563362eab8c029`.

```powershell
$firmware = ".\examples\advanced\CortexM7\firmware\stm32h750"
.\.venv\Scripts\python.exe "$firmware\build_firmware.py" `
    --device PATH_TO_ST_CMSIS_DEVICE --cmsis PATH_TO_ARM_CMSIS `
    --linker PATH_TO_STM32H750XBHx_FLASH_LD --zig PATH_TO_ZIG `
    --c-library-include PATH_TO_ARM_C_HEADERS `
    --output ".\build\stm32h750" `
    --circuit-output ".\build\DaisySeed_STM32H750_Boot.ckt"
```

The build leaves `firmware.elf` and `firmware.map` in the specified output
directory. With GNU Arm tools on PATH, omit the Zig and C header options to
build with `arm-none-eabi-gcc`.
