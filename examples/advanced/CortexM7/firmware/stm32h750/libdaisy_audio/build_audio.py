"""Build unmodified libDaisy AudioPassthru for Seed internal flash."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess


# These are the upstream library objects pulled in by AudioPassthru's ELF.
DAISY_MODULES = (
    "daisy_seed.cpp", "dev/codec_ak4556.cpp", "dev/codec_wm8731.cpp", "dev/sdram.cpp",
    "hid/audio.cpp", "per/gpio.cpp", "per/i2c.cpp", "per/qspi.cpp", "per/rng.cpp",
    "per/sai.cpp", "per/spi.cpp", "per/tim.cpp", "per/uart.cpp", "sys/dma.c",
    "sys/system.cpp", "sys/system_stm32h7xx.c",
)
USB_DEVICE = "Middlewares/ST/STM32_USB_Device_Library"
USB_HOST = "Middlewares/ST/STM32_USB_Host_Library"
HAL = "Drivers/STM32H7xx_HAL_Driver"
HAL_MODULES = (
    "hal", "hal_cortex", "hal_dma", "hal_gpio", "hal_i2c", "hal_i2c_ex", "hal_mdma",
    "hal_pwr_ex", "hal_qspi", "hal_rcc", "hal_rcc_ex", "hal_sai", "hal_sdram",
    "hal_spi", "hal_tim", "hal_tim_ex", "hal_uart", "hal_uart_ex", "ll_fmc",
)
USB_IRQS = ("OTG_HS_EP1_OUT_IRQHandler", "OTG_HS_EP1_IN_IRQHandler", "OTG_HS_IRQHandler")


def build(daisy, zig, toolchain, output):
    """Use Zig Clang/LLD and the target libraries from GNU Arm Embedded 10.2.1."""
    example = daisy / "examples/AudioPassthru/AudioPassthru.cpp"
    startup = daisy / "core/startup_stm32h750xx.c"
    linker = daisy / "core/STM32H750IB_flash.lds"
    # libDaisy's public structs contain enums. Match their layout across the
    # example and library, using the application Makefile's enum width.
    # Public board structures cross application/library boundaries. Every
    # object must use the same enum layout as the application Makefile.
    basic = [
        str(zig), "-cc1", "-triple", "thumbv7em-unknown-none-eabihf",
        "-target-cpu", "cortex-m7", "-mfloat-abi", "hard",
        "-target-feature", "+fp-armv8d16sp", "-target-feature", "-fp64",
        "-ffp-contract=off", "-target-abi", "aapcs", "-fgnuc-version=4.2.1",
        "-mrelocation-model", "static", "-mframe-pointer=none",
        "-fshort-enums", "-ffunction-sections", "-fdata-sections", "-Oz", "-emit-obj",
    ]
    defines = [
        "-DCORE_CM7", "-DSTM32H750xx", "-DSTM32H750IB", "-DARM_MATH_CM7",
        "-DHSE_VALUE=16000000", "-DUSE_HAL_DRIVER", "-DUSE_FULL_LL_DRIVER",
        "-Dflash_layout", "-DNDEBUG=1", "-DRELEASE=1",
    ]
    include_dirs = (
        ".", "src", "src/sys", "src/usbd", "src/usbh", "core",
        "Drivers/CMSIS_5/CMSIS/Core/Include", "Drivers/CMSIS-Device/ST/STM32H7xx/Include",
        "Drivers/CMSIS-DSP/Include", f"{HAL}/Inc", f"{HAL}/Inc/Legacy",
        f"{USB_DEVICE}/Core/Inc", "Middlewares/Patched/ST/STM32_USB_Device_Library/Class/CDC/Inc",
        f"{USB_DEVICE}/Class/CDC/Inc", f"{USB_HOST}/Core/Inc", f"{USB_HOST}/Class/MSC/Inc",
        f"{USB_HOST}/Class/MIDI/Inc", "Middlewares/Third_Party/FatFs/src",
    )
    includes = [arg for directory in include_dirs for arg in ("-I", str(daisy / directory))]
    system = toolchain / "arm-none-eabi"
    version = "10.2.1"
    gcc = toolchain / "lib/gcc/arm-none-eabi" / version
    cxx = system / "include/c++" / version
    multilib = Path("thumb/v7e-m+fp/hard")
    cxx_dirs = (cxx, cxx / "arm-none-eabi" / multilib, cxx / "arm-none-eabi", cxx / "backward")
    cxx_includes = [arg for directory in cxx_dirs for arg in ("-isystem", str(directory))]
    system_includes = ["-isystem", str(zig.parent / "lib/include"), "-isystem", str(system / "include")]
    library_sources = [
        *sorted(daisy / "src" / name for name in DAISY_MODULES),
        *sorted(daisy / HAL / "Src" / f"stm32h7xx_{name}.c" for name in HAL_MODULES),
    ]
    object_dir = output / "obj"
    object_dir.mkdir(parents=True, exist_ok=True)

    def compile_one(source):
        relative = source.relative_to(daisy)
        target = object_dir / Path("_".join(relative.parts)).with_suffix(".o")
        if source.suffix == ".cpp":
            language = [*cxx_includes, "-x", "c++", "-std=gnu++14", "-fno-rtti"]
        else:
            language = ["-std=gnu11"]
        flags = (["-include", "stm32h7xx.h"] if source in (example, startup)
                 else ["-DDATA_IN_D2_SRAM"])
        if source != example:
            # Clang uses the ordinary C symbol for main in hosted C++ mode.
            flags.append("-ffreestanding")
        subprocess.run([*basic, *language, *system_includes, *includes, *defines, *flags,
                        str(source), "-o", str(target)], check=True)
        return target

    with ThreadPoolExecutor(max_workers=6) as pool:
        objects = list(pool.map(compile_one, (startup, example, *library_sources)))
    # Create a fresh archive so members from another build cannot affect resolution.
    archive = output / "libdaisy.a"
    archive.unlink(missing_ok=True)
    subprocess.run([str(zig), "ar", "rcs", str(archive), *map(str, objects[2:])], check=True)
    gcc_lib, target_lib = gcc / multilib, system / "lib" / multilib
    libs = [target_lib / name for name in
            ("libstdc++_nano.a", "libsupc++_nano.a", "libc_nano.a", "libm.a", "libnosys.a")]
    elf = output / "AudioPassthru.elf"
    # These unused vectors can use the stock default handler. This lets the
    # linker exclude the legacy USB middleware from the simulation image.
    vectors = [flag for name in USB_IRQS
               for flag in (f"--wrap={name}", f"--defsym=__wrap_{name}=Default_Handler")]
    subprocess.run([
        str(zig), "ld.lld", "--gc-sections", "--build-id=none", "--nmagic",
        *vectors, "-Map", str(output / "AudioPassthru.map"), "-T", str(linker),
        str(gcc_lib / "crti.o"), str(gcc_lib / "crtbegin.o"), *map(str, objects[:2]),
        "--start-group", str(archive), *map(str, libs), str(gcc_lib / "libgcc.a"), "--end-group",
        str(gcc_lib / "crtend.o"), str(gcc_lib / "crtn.o"), "-o", str(elf),
    ], check=True)
    return elf


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--daisy", type=Path, required=True, help="libDaisy v8.0.0 with submodules")
    parser.add_argument("--zig", type=Path, required=True, help="Zig 0.15.2 executable")
    parser.add_argument("--toolchain", type=Path, required=True, help="GNU Arm Embedded 10.2.1 root")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(build(args.daisy.resolve(), args.zig.resolve(), args.toolchain.resolve(), args.output.resolve()))
    except (OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{error}\n")
