"""Build unmodified ST HAL SPI/I2C drivers into the OOC serial demonstration."""

import argparse
from pathlib import Path
import runpy
import subprocess


HERE = Path(__file__).resolve().parent


def build(device, cmsis, hal, linker, output, *, prefix="arm-none-eabi-", zig=None,
          c_library_include=None):
    output.mkdir(parents=True, exist_ok=True)
    templates = device / "Source/Templates"
    sources = [templates / "gcc/startup_stm32h750xx.s", templates / "system_stm32h7xx.c",
               HERE / "demo.c"]
    sources.extend(hal / "Src" / f"stm32h7xx_hal_{module}.c" for module in ("spi", "i2c", "dma"))
    flags = ["-ffreestanding", "-fno-builtin", "-fshort-enums", "-ffunction-sections",
             "-fdata-sections", "-DSTM32H750xx", "-DUSE_PWR_LDO_SUPPLY", "-DUSER_VECT_TAB_ADDRESS"]
    for directory in (HERE, device / "Include", cmsis / "CMSIS/Core/Include", hal / "Inc"):
        flags.extend(("-I", str(directory)))
    objects = []
    if zig is None:
        compiler = [prefix + "gcc", "-mcpu=cortex-m7", "-mthumb", "-mfloat-abi=hard",
                    "-mfpu=fpv5-sp-d16", "-Os", "-c"]
        assembler = compiler
        link = [*compiler[:-2], "-nostdlib", "-Wl,--gc-sections,--build-id=none,--nmagic",
                "-Wl,-Map=" + str(output / "firmware.map")]
    else:
        zig = zig.resolve()
        target = ["-triple", "thumbv7em-unknown-none-eabihf", "-target-cpu", "cortex-m7"]
        assembler = [str(zig), "-cc1as", *target, "-filetype", "obj"]
        compiler = [str(zig), "-cc1", *target, "-mfloat-abi", "hard", "-target-abi", "aapcs",
                    "-target-feature", "+fp-armv8d16sp", "-target-feature", "-fp64",
                    "-fgnuc-version=4.2.1", "-mrelocation-model", "static", "-mframe-pointer=none",
                    "-Oz", "-isystem", str(zig.parent / "lib/include"),
                    "-isystem", str(c_library_include), "-emit-obj"]
        link = [str(zig), "ld.lld", "--gc-sections", "--build-id=none", "--nmagic",
                "-Map", str(output / "firmware.map")]
    for source in sources:
        obj = output / (source.stem + ".o")
        command = assembler if source.suffix == ".s" else [*compiler, *flags]
        subprocess.run([*command, str(source), "-o", str(obj)], check=True)
        objects.append(obj)
    elf = output / "firmware.elf"
    subprocess.run([*link, "-T", str(linker),
                    *map(str, objects), "-o", str(elf)], check=True)
    return elf


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("device", "cmsis", "hal", "linker", "output", "circuit-output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--tool-prefix", default="arm-none-eabi-")
    parser.add_argument("--zig", type=Path)
    parser.add_argument("--c-library-include", type=Path)
    args = parser.parse_args()
    if args.zig is not None and args.c_library_include is None:
        parser.error("--zig requires --c-library-include with target C headers.")
    elf = build(args.device, args.cmsis, args.hal, args.linker, args.output, prefix=args.tool_prefix,
                zig=args.zig, c_library_include=args.c_library_include)
    builder = runpy.run_path(str(HERE.parent / "build_firmware.py"))
    image = builder["load_elf"](elf)
    image.probes.update(SERIAL_STAGE=image.symbols["serial_stage"],
                        SERIAL_IRQ_COMPLETIONS=image.symbols["serial_irq_completions"])
    target = builder["TARGET"]
    text = target["render_demo"](image.image, image.ram_regions, image.stack_top, image.main, image.probes)
    target["write_circuit"](args.circuit_output, text)
    print(f"Built {elf}; wrote {args.circuit_output}")


if __name__ == "__main__":
    main()
