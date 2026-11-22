# libDaisy AudioPassthru on the Daisy Seed

This demo runs the original stereo callback from libDaisy v8.0.0 on the
OOC STM32H750 model. It uses the original Daisy Seed board profile with
the AK4556 codec. The firmware runs from internal flash at `0x08000000`,
initializes the QSPI and SDRAM interfaces, and starts SAI1 with circular
DMA. [AudioPassthru source](https://github.com/daisyaudio/libDaisy/blob/v8.0.0/examples/AudioPassthru/AudioPassthru.cpp) ·
[Daisy Seed initialization](https://github.com/daisyaudio/libDaisy/blob/v8.0.0/src/daisy_seed.cpp)

## Open the viewer demo

Open the bundled [Daisy Seed AudioPassthru circuit](../../../DaisySeed_AudioPassthru.ckt)
and choose **Logic simulation**. Select **Reset** then **Run** to advance through
startup. Use **Signals** to inspect `QSPI_READY`, `SDRAM_READY`, and
`AUDIO_FRAMES`. Inspect `AUDIO_IN_LEFT`, `AUDIO_IN_RIGHT`, `AUDIO_OUT_LEFT`,
and `AUDIO_OUT_RIGHT` to see the 24-bit PCM words move through the model.
The input is a deterministic stereo stream. The output follows after a DMA
buffer cycle.

To generate a new entry circuit from the supplied ELF, run this from the repository root.

```powershell
$firmware = ".\examples\advanced\CortexM7\firmware\stm32h750"
.\.venv\Scripts\python.exe "$firmware\build_firmware.py" `
    --audio --elf ".\tests\fixtures\digital\libdaisy_v8_0_0_audio_passthru.elf" `
    --circuit-output ".\build\DaisySeed_AudioPassthru.ckt"
```

The generated entry includes the shared CPU and board modules, with the
ROM and platform files under `firmware/images` and `boards`. Add
`--standalone` for a single-file export.

To verify the bundled demo from reset, run this from the repository root.

```powershell
.\.venv\Scripts\python.exe .\examples\advanced\CortexM7\firmware\stm32h750\libdaisy_audio\verify_audio.py
```

The check can take several minutes. It uses the viewer's simulator to
confirm QSPI and SDRAM readiness, entry into both DMA interrupt handlers
and the stock callback. Playback starts with one silent 96-frame DMA
buffer, and the next 14 stereo pairs match the original input.

The bundled build was verified through 110 frames with no CPU faults. The first 96 output frames were silent, followed by 14 exact left/right sample matches.

| Checkpoint | Simulation cycle |
| --- | ---: |
| QSPI memory-mapped mode ready | 44,538 |
| SDRAM initialization complete | 65,593 |
| First stock audio callback | 78,095 |
| First nonzero stereo output | 79,574 |

The latest full verification took 6 minutes 28 seconds while other development
checks ran concurrently; an earlier run took 3 minutes 21 seconds. Same-design
stepping benchmarks measured a 1.6–1.9× improvement
after compiling validated digital expressions and clocked writes to bytecode;
512-cycle comparisons matched every internal value and public output. Building
the audio runtime initially takes about two seconds longer with this approach.

## Model scope

The firmware configures 48 kHz stereo with two 32-bit SAI slots carrying
signed 24-bit PCM and processes 48 frames per callback. OOC advances each
SAI slot after 32 simulated clock cycles and accelerates long SysTick
intervals while preserving the firmware's reload register. Viewer cycles
can be used to follow sample and interrupt order.

The AK4556 model responds to its GPIO reset and supplies a repeatable
digital stereo stream. Host audio input, analog codec behavior, and other
Daisy Seed codec revisions would need separate models. CPU and DMA see
coherent SRAM, with CMSIS cache control registers acknowledged by the
model. Cache line contents and bus timing are outside this simulation.

QSPI initialization provides an erased read window. QSPI programming,
external-flash boot, and detailed SDRAM refresh and transfer timing
remain outside this demo.

## Rebuild the ELF

Use Zig 0.15.2 and the GNU Arm Embedded 10.2.1 toolchain supplied with
DaisyToolchain. The build takes the Arm target headers and runtime archives
from GNU Arm Embedded. The tag below resolves to
`c6887c69c9d38a54bc5ba15bc89e4c2256c04895` and pins the upstream submodules.

```powershell
git clone --recurse-submodules --branch v8.0.0 https://github.com/daisyaudio/libDaisy.git
$firmware = ".\examples\advanced\CortexM7\firmware\stm32h750"
.\.venv\Scripts\python.exe "$firmware\libdaisy_audio\build_audio.py" `
    --daisy ".\libDaisy" --zig PATH_TO_ZIG --toolchain "C:\Program Files\DaisyToolchain" `
    --output ".\build\libdaisy_audio"
```

The build uses libDaisy's original startup, linker script, and application
source. All sources use the application Makefile's short enum layout so
the library and example agree on their shared C++ structures. The
simulator link redirects three unused USB HS interrupt vectors
to libDaisy's stock `Default_Handler` so the legacy USB middleware stays
out of the image. Import the rebuilt `AudioPassthru.elf` with `--audio`.
The supplied fixture's SHA-256 is
`ac24118f6187ded1a2dc4b232ae2ac0c3ae9141852e1cac78feeec07afb55321`.
Its source and runtime license notices live beside the ELF.
[Startup source](https://github.com/daisyaudio/libDaisy/blob/v8.0.0/core/startup_stm32h750xx.c) ·
[Linker script](https://github.com/daisyaudio/libDaisy/blob/v8.0.0/core/STM32H750IB_flash.lds) ·
[ELF fixture](../../../../../../tests/fixtures/digital/libdaisy_v8_0_0_audio_passthru.elf)
