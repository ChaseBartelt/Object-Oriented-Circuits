"""Build or import an STM32H750 ELF and generate a step-through OOC circuit."""

import argparse
from dataclasses import dataclass
from pathlib import Path
import runpy
import struct
import subprocess


HERE = Path(__file__).resolve().parent
TARGET = runpy.run_path(str(HERE / "render_target.py"))
FLASH_BASE = TARGET["FLASH_BASE"]
FLASH_SIZE = TARGET["FLASH_SIZE"]
RAM_REGIONS = TARGET["RAM_REGIONS"]
DEFAULT_PROBES = {
    "BOOT_SIGNATURE": "boot_signature",
    "DATA_VALUE": "data_value",
    "BSS_VALUE": "bss_value",
    "CLOCK_HZ": "clock_hz",
    "CLOCK_STATUS": "clock_status",
    "HAL_TICK": "uwTick",
    "LED_TRANSITIONS": "led_transitions",
}
HAL_MODULES = ("", "_cortex", "_gpio", "_pwr_ex", "_rcc")
ELF_HEADER = struct.Struct("<16sHHIIIIIHHHHHH")
PROGRAM_HEADER = struct.Struct("<8I")
SECTION_HEADER = struct.Struct("<10I")
SYMBOL = struct.Struct("<IIIBBH")


@dataclass
class BootImage:
    image: bytes
    ram_regions: list[tuple[int, int]]
    stack_top: int
    reset: int
    main: int
    probes: dict[str, int]
    symbols: dict[str, int]


def _headers(data, offset, size, count, layout):
    end = offset + size * count
    if size != layout.size or offset < ELF_HEADER.size or end > len(data):
        raise ValueError("Invalid ELF header table.")
    return list(layout.iter_unpack(data[offset:end]))


def _symbols(data, sections):
    symbols = {}
    for _, kind, _, _, offset, size, link, _, _, entry_size in sections:
        if kind != 2:  # SHT_SYMTAB
            continue
        if link >= len(sections) or entry_size != SYMBOL.size or size % entry_size:
            raise ValueError("Invalid ELF symbol table.")
        _, string_kind, _, _, string_offset, string_size, *_ = sections[link]
        if (string_kind != 3 or offset + size > len(data)
                or string_offset + string_size > len(data)):
            raise ValueError("Invalid ELF symbol or string table bounds.")
        strings = data[string_offset:string_offset + string_size]
        for name, value, _, info, _, section in SYMBOL.iter_unpack(data[offset:offset + size]):
            if section == 0 or info >> 4 not in (1, 2):  # Defined global or weak symbol
                continue
            end = strings.find(b"\0", name)
            if end == -1:
                raise ValueError("Invalid ELF symbol name.")
            symbols[strings[name:end].decode("utf-8", errors="replace")] = value
    return symbols


def _in_flash(start, size):
    return FLASH_BASE <= start < start + size <= FLASH_BASE + FLASH_SIZE


def load_elf(path):
    """Read flash load addresses separately from writable RAM runtime addresses."""
    data = Path(path).read_bytes()
    if len(data) < ELF_HEADER.size:
        raise ValueError("Expected a little-endian executable ARM ELF32 file.")
    ident, kind, machine, version, entry, phoff, shoff, _, _, phsize, phnum, shsize, shnum, _ = (
        ELF_HEADER.unpack_from(data)
    )
    if ident[:7] != b"\x7fELF\x01\x01\x01" or (kind, machine, version) != (2, 40, 1):
        raise ValueError("Expected a little-endian executable ARM ELF32 file.")
    programs = _headers(data, phoff, phsize, phnum, PROGRAM_HEADER)
    sections = _headers(data, shoff, shsize, shnum, SECTION_HEADER)
    symbols = _symbols(data, sections)
    if "main" not in symbols:
        raise ValueError("Keep the main symbol in the ELF to show its entry in the viewer.")

    flash = bytearray(b"\xff" * FLASH_SIZE)
    populated = bytearray(FLASH_SIZE)
    used = 0
    executable = []
    ram_regions = []
    for kind, offset, address, load_address, file_size, memory_size, flags, _ in programs:
        if kind != 1 or not (file_size or memory_size):  # PT_LOAD
            continue
        if file_size > memory_size or (file_size and offset + file_size > len(data)):
            raise ValueError("Invalid ELF load segment bounds.")
        content = data[offset:offset + file_size]
        if _in_flash(address, memory_size):
            if address != load_address:
                raise ValueError("Flash segments must run at their linked load addresses.")
            if flags & 1 and file_size:
                executable.append((address, address + file_size))
        elif any(start <= address < address + memory_size <= end for start, end in RAM_REGIONS):
            if flags & 1:
                raise ValueError("This STM32H750 profile fetches instructions from internal flash.")
            ram_regions.append((address, memory_size))
            if load_address == address:
                # Some stock linkers emit zero-filled file space for heap and bss.
                if any(content):
                    raise ValueError("Initialized SRAM data needs an internal-flash load address.")
                continue
        else:
            raise ValueError(f"Unsupported STM32H750 load segment at {address:#010x}.")
        if not file_size:
            continue
        if not _in_flash(load_address, file_size):
            raise ValueError(f"ELF load address {load_address:#010x} is outside internal flash.")
        start = load_address - FLASH_BASE
        end = start + file_size
        if any(populated[start:end]):
            raise ValueError(f"Overlapping ELF flash segments at {load_address:#010x}.")
        flash[start:end] = content
        populated[start:end] = b"\x01" * file_size
        used = max(used, end)

    if not all(populated[:8]):
        raise ValueError("Expected the STM32H750 reset vectors at 0x08000000.")
    stack_top, reset = struct.unpack_from("<II", flash)
    if reset != entry or not reset & 1:
        raise ValueError("The flash reset vector must match the ELF's Thumb entry point.")
    main = symbols["main"] & ~1
    for name, address in (("Reset_Handler", reset & ~1), ("main", main)):
        if not any(start <= address < end for start, end in executable):
            raise ValueError(f"{name} does not point to an executable flash segment.")
    probes = {name: symbols[symbol] for name, symbol in DEFAULT_PROBES.items() if symbol in symbols}
    return BootImage(bytes(flash[:used]), ram_regions, stack_top, reset, main, probes, symbols)


def build(device, cmsis, linker, output, *, prefix, zig=None, c_library_include=None, hal=None):
    """Build with stock ST source files and a byte-preserved ST linker script."""
    output.mkdir(parents=True, exist_ok=True)
    templates = device / "Source" / "Templates"
    startup = templates / "gcc" / "startup_stm32h750xx.s"
    system = templates / "system_stm32h7xx.c"
    definitions = ["-DSTM32H750xx", "-DUSE_PWR_LDO_SUPPLY", "-DUSER_VECT_TAB_ADDRESS"]
    includes = ["-I", str(device / "Include"), "-I", str(cmsis / "CMSIS" / "Core" / "Include")]
    flags = ["-ffreestanding", "-fno-builtin", "-ffunction-sections", "-fdata-sections"]
    sources = [system, HERE / "demo.c"]
    float_flags = ["-mfloat-abi=soft"]
    if hal is not None:
        demo = HERE / "hal_blink"
        sources = [system, demo / "blink.c"]
        sources.extend(hal / "Src" / f"stm32h7xx_hal{suffix}.c" for suffix in HAL_MODULES)
        includes.extend(("-I", str(demo), "-I", str(hal / "Inc")))
        definitions.extend(("-DHSE_VALUE=16000000U", "-DOOC_SIMULATION"))
        float_flags = ["-mfloat-abi=hard", "-mfpu=fpv5-sp-d16", "-ffp-contract=off"]
    gnu = [prefix + "gcc", "-mcpu=cortex-m7", "-mthumb", *float_flags]
    elf = output / "firmware.elf"
    objects = [output / (source.stem + ".o") for source in (startup, *sources)]
    if zig is None:
        assembler = gnu + ["-c"]
        compiler = gnu + ["-Os", "-c"]
        link = gnu + ["-nostdlib", "-Wl,--gc-sections,--build-id=none,--nmagic"]
        map_options = ["-Xlinker", "-Map", "-Xlinker", str(output / "firmware.map")]
    else:
        zig = zig.resolve()
        abi = "eabihf" if hal is not None else "eabi"
        target = ["-triple", f"thumbv7em-unknown-none-{abi}", "-target-cpu", "cortex-m7"]
        codegen = (["-mfloat-abi", "hard", "-target-feature", "+fp-armv8d16sp",
                    "-target-feature", "-fp64", "-ffp-contract=off"] if hal is not None
                   else ["-target-feature", "+soft-float"])
        assembler = [str(zig), "-cc1as", *target, "-filetype", "obj"]
        compiler = [
            str(zig), "-cc1", *target, *codegen, "-target-abi", "aapcs",
            "-fgnuc-version=4.2.1", "-mrelocation-model", "static", "-mframe-pointer=none",
            "-Oz", "-isystem", str(zig.parent / "lib" / "include"),
            "-isystem", str(c_library_include), "-emit-obj",
        ]
        link = [str(zig), "ld.lld", "--gc-sections", "--build-id=none", "--nmagic"]
        map_options = ["-Map", str(output / "firmware.map")]
    subprocess.run([*assembler, str(startup), "-o", str(objects[0])], check=True)
    for source, obj in zip(sources, objects[1:]):
        subprocess.run([*compiler, *flags, *definitions, *includes, str(source), "-o", str(obj)],
                       check=True)
    subprocess.run([*link, *map_options, "-T", str(linker), *map(str, objects), "-o", str(elf)],
                   check=True)
    return elf


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--elf", type=Path, help="import an existing STM32H750 ELF")
    parser.add_argument("--audio", action="store_true",
                        help="model the original Daisy Seed audio board for an imported ELF")
    parser.add_argument("--device", type=Path, help="ST cmsis-device-h7 checkout")
    parser.add_argument("--cmsis", type=Path, help="Arm CMSIS_5 checkout")
    parser.add_argument("--linker", type=Path, help="stock ST internal-flash linker script")
    parser.add_argument("--hal", type=Path, help="build the LED example with an ST HAL driver checkout")
    parser.add_argument("--output", type=Path, default=HERE / "build", help="firmware build directory")
    parser.add_argument("--tool-prefix", default="arm-none-eabi-", help="GNU Arm toolchain prefix")
    parser.add_argument("--zig", type=Path, help="use Zig 0.15.2's Clang and LLD instead of GNU Arm")
    parser.add_argument("--c-library-include", type=Path, help="target C headers for Zig, including math.h")
    parser.add_argument("--circuit-output", type=Path, required=True, help="destination .ckt")
    parser.add_argument("--standalone", action="store_true", help="write one self-contained circuit file")
    args = parser.parse_args()
    build_inputs = (args.device, args.cmsis, args.linker)
    if args.audio and args.elf is None:
        parser.error("--audio requires a prebuilt internal-flash ELF supplied with --elf.")
    if args.elf is None and not all(build_inputs):
        parser.error("Provide --elf or the three build inputs --device, --cmsis, and --linker.")
    if args.elf is not None and any((*build_inputs, args.hal)):
        parser.error("Use --elf alone to skip compiling the demo.")
    if args.elf is None and args.zig is not None and args.c_library_include is None:
        parser.error("--zig requires --c-library-include with target C library headers.")
    try:
        elf = args.elf or build(*build_inputs, args.output, prefix=args.tool_prefix, zig=args.zig,
                                c_library_include=args.c_library_include, hal=args.hal)
        image = load_elf(elf)
        circuit = TARGET["render_demo"](image.image, image.ram_regions, image.stack_top,
                                        image.main, image.probes, audio=args.audio)
        TARGET["write_circuit"](args.circuit_output, circuit, standalone=args.standalone)
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(args.circuit_output)


if __name__ == "__main__":
    main()
