# STM32H750 ST HAL serial demo

Open [`DaisySeed_SPI_I2C.ckt`](../../../DaisySeed_SPI_I2C.ckt), choose
**Logic simulation**, and use **Step** or **Run**. The short entry document
connects the existing Cortex-M7 core, flash image, SRAM, NVIC, and serial
controllers. Its included chip blocks remain individually inspectable.

The program executes ST's original reset handler and `SystemInit`, then calls
unmodified ST HAL SPI and I2C drivers. `SERIAL_STAGE` shows its progress:

| Stage | Firmware action |
| --- | --- |
| 1 | Initialize SPI1 and poll an eight-byte loopback transfer. |
| 2 | Repeat SPI1 using its interrupt, physical NVIC line 35. |
| 3 | Write and read four registers on I2C1 using `HAL_I2C_Mem_Write/Read`. |
| 4 | Verify that absent I2C address `0x51` NACKs, then read `0x50` successfully again. |
| 5 | Send and receive through I2C1 event IRQ 31. |
| 6 | Read I2C2's independent test device using event IRQ 33. |
| 7 | All checks passed. `BOOT_SIGNATURE` becomes `0x5352494C`. |

At success, `SERIAL_IRQ_COMPLETIONS = 4`, `DATA_VALUE = 0x438`, and
`BSS_VALUE = 0x5599`. SysTick continues updating `HAL_TICK`. A failed firmware
check writes `0xBAD00000 | (stage << 8) | reason` instead of the success marker.
The supplied firmware reaches success at cycle 6,061 with 16 SPI frames,
21 I2C1 bytes and one I2C2 byte transferred.

The viewer exposes `SPI1_TX`, `SPI1_RX`, `SPI1_TRANSFERS`, `SPI1_STATUS`, and
the corresponding four fields for I2C1 and I2C2. SPI's count is completed
frames; I2C counts completed bytes, including register-address bytes.

## Modeled connections and scope

SPI1 at `0x40013000` has an explicit digital loopback peer. Its transmit FIFO
can be filled before `CSTART`, then finite `TSIZE` transfers clock frames into
the receive FIFO. Byte, halfword, and word CPU accesses, frame masks, FIFO
thresholds, end-of-transfer, overrun, suspend, and write-one-to-clear flags
are modeled. The regression exercises 8-, 16-, and 32-bit frames.

I2C1 at `0x40005400` and I2C2 at `0x40005800` each connect to a separate,
256-byte register test device at **7-bit address `0x50`**. Register zero starts
at `0x42`; other bytes start at zero. A write's first byte selects the register,
later bytes write sequentially, and reads advance the pointer with wraparound.
Device storage and the pointer survive an RCC controller reset. The viewer's
board reset restores initial values. Other device addresses NACK.

Both I2C controllers support TXDR preload, repeated START, `RELOAD`/`TCR`,
`TC`, `AUTOEND`/`STOPF`, NACK detection, W1C flags, and event interrupts. Event
and error outputs occupy separate physical NVIC lines: 31/32 and 33/34.
RCC clocks gate register access and IRQ delivery; peripheral resets operate
independently. RXDR consumes data only on an authorized CPU read, so viewer
inspection and denied MPU accesses do not remove received values.

This is a register-level transfer model. It does not simulate serial pin
edges, arbitration, external clock stretching, CRC, SPI/I2C DMA requests,
general I2C slaves, or the WM8731 codec. Bus-error/timeout/arbitration events
are not synthesized. SPI frame scheduling is abstract, so this example does
not establish interrupt throughput at a physical SCK frequency.

The I2C status probe packs ISR flags in bits 15:0, target address in 22:16,
device pointer in 30:23, and `PE` in bit 31. SPI status is its SR register.

## Rebuild

The checked-in ELF is `tests/fixtures/digital/stm32h750_hal_serial.elf`.
It uses ST CMSIS device `v1.10.6`, Arm CMSIS `5.9.0`, the original
`STM32H750XBHx_FLASH.ld` from STM32CubeH7 `v1.10.0`'s STM32H750B-DK
ExtMem_Boot template, and the unmodified SPI/I2C/DMA drivers bundled with
libDaisy `v8.0.0` (ST HAL `1.11.1`). DMA driver code satisfies HAL handler
link dependencies; the demo executes polling and interrupt transfers.

```powershell
.\.venv\Scripts\python.exe `
  .\examples\advanced\CortexM7\firmware\stm32h750\hal_serial\build_serial.py `
  --device PATH_TO_ST_CMSIS_DEVICE --cmsis PATH_TO_ARM_CMSIS `
  --hal PATH_TO_LIBDAISY_Drivers_STM32H7xx_HAL_Driver `
  --linker PATH_TO_STM32H750XBHx_FLASH_LD `
  --output .\build\hal_serial `
  --circuit-output .\examples\advanced\CortexM7\DaisySeed_SPI_I2C.ckt `
  --zig PATH_TO_ZIG --c-library-include PATH_TO_ARM_C_HEADERS
```

The supplied fixture was built with Zig `0.15.2` using uniform
`-fshort-enums` across the application and HAL objects. With GNU Arm tools on
PATH, omit the final two options. The builder preserves the ST startup,
system source, linker script, and HAL driver source; `demo.c` supplies the
freestanding C runtime hook and interrupt callbacks.

Fixture SHA-256:
`9f9573f3356232179cf9f11e79173c5808b5227f86656d302faf86f6b7b1b845`.
ST and CMSIS redistribution notices are in `tests/fixtures/digital`, including
`stm32h7xx_hal_driver_v1_11_1_LICENSE.md` for these HAL objects.

Run the bounded serial checks with:

```powershell
.\.venv\Scripts\python.exe -m unittest tests.exporters.test_stm32h750_serial -v
```

The seven peripheral tests run without booting a CPU. The additional compiled
firmware regression executes all stages and verifies SysTick and the three
high NVIC vectors, callback counts, received data, and NACK recovery. It uses
a fixed 16,000-step maximum and does not run the long Daisy audio regression.
