"""Build the Cortex-M7 firmware and format its linked flash as OOC ROM."""

import argparse
from pathlib import Path
import re
import struct
import subprocess
import tempfile


PROJECT = Path(__file__).resolve().parent
MODEL = PROJECT.parent / "CortexM7_Firmware.ckt"
FLASH_LIMIT = 256
STACK_TOP = 0x20000400
LARGE_FLASH_LIMIT = 4096
LARGE_STACK_TOP = 0x20001000
BOOT_SOURCES = ("startup.S", "firmware.c", "arithmetic.c")
SYSTICK_SOURCES = ("startup_systick.S", "systick.c")
LARGE_SOURCES = ("startup_large.S", "large.c")
HARDFLOAT_SOURCES = ("startup_large.S", "hardfloat.c")
RTOS_SOURCES = ("startup_rtos.S", "rtos.c")
BEGIN = b"// BEGIN LINKED FIRMWARE"
END = b"// END LINKED FIRMWARE"
MODEL_INCLUDE = re.compile(rb'^include\("([^"\r\n]+)"\);[ \t]*(?:\r?\n|$)', re.M)
PORT_DECLARATION = re.compile(r"( +)([^;=]+?)\s*=\s*(input|output|wire);\s*$")


def grouped_ports(source):
    """Keep generated circuits in the same declaration-block style as the sources."""
    source = source.replace("${", "*{")
    lines = source.splitlines(keepends=True)
    result, index = [], 0
    while index < len(lines):
        match = PORT_DECLARATION.fullmatch(lines[index].rstrip("\r\n"))
        if not match:
            result.append(lines[index])
            index += 1
            continue
        indent, _, direction = match.groups()
        result.append(f"{indent}{direction} {{\n")
        while index < len(lines):
            item = PORT_DECLARATION.fullmatch(lines[index].rstrip("\r\n"))
            if not item or item[1] != indent or item[3] != direction:
                break
            result.append(f"{indent}    {item[2].rstrip()};\n")
            index += 1
        result.append(f"{indent}}}\n")
    return "".join(result)


def tool(command, **kwargs):
    try:
        return subprocess.run(command, check=True, **kwargs)
    except OSError as error:
        raise SystemExit(f"Could not launch {command[0]}: {error}") from error


def build_gnu(prefix, output, sources=BOOT_SOURCES, *, linker="linker.ld", hard_float=False):
    """Build with GNU Arm tools and save the linked disassembly."""
    output.mkdir(parents=True, exist_ok=True)
    driver = [prefix + "gcc", "-mcpu=cortex-m7", "-mthumb"]
    if hard_float:
        driver.extend(("-mfpu=fpv5-sp-d16", "-mfloat-abi=hard"))
    flags = [
        "-O2" if hard_float else "-Os", "-mword-relocations", "-ffreestanding", "-fno-builtin",
        "-fno-stack-protector", "-fomit-frame-pointer",
        "-fno-optimize-sibling-calls", "-fno-unwind-tables", "-fno-asynchronous-unwind-tables",
        "-ffunction-sections", "-fdata-sections",
    ]
    objects = []
    for name in sources:
        source = PROJECT / name
        obj = output / source.with_suffix(".o").name
        startup_flags = ["-Wa,--defsym,OOC_HARDFLOAT=1"] if hard_float and source.suffix == ".S" else []
        tool([*driver, *flags, *startup_flags, "-c", str(source), "-o", str(obj)])
        objects.append(str(obj))
    elf = output / "firmware.elf"
    link_map = output / "firmware.map"
    tool([
        *driver, "-nostdlib", "-Wl,--gc-sections", "-Wl,--build-id=none",
        "-Xlinker", "-Map", "-Xlinker", str(link_map), "-T", str(PROJECT / linker),
        *objects, "-o", str(elf),
    ])
    binary = output / "firmware.bin"
    tool([prefix + "objcopy", "-O", "binary", str(elf), str(binary)])
    disassembly = tool([prefix + "objdump", "-d", "-s", str(elf)], capture_output=True, text=True)
    (output / "firmware.disassembly.txt").write_text(disassembly.stdout, encoding="utf-8")
    return binary


def extract_flash(elf, flash_limit=FLASH_LIMIT):
    """Copy the linked ARM ELF's flash segments to their physical byte addresses."""
    if len(elf) < 52 or elf[:6] != b"\x7fELF\x01\x01" or struct.unpack_from("<H", elf, 18)[0] != 40:
        raise ValueError("Expected a little-endian 32-bit ARM ELF.")
    offset = struct.unpack_from("<I", elf, 28)[0]
    entry_size, entries = struct.unpack_from("<HH", elf, 42)
    if entry_size != 32 or offset + entries * entry_size > len(elf):
        raise ValueError("Invalid ARM ELF program headers.")
    image = bytearray()
    for index in range(entries):
        start = offset + index * entry_size
        kind, source, _, address, length = struct.unpack_from("<5I", elf, start)
        if kind != 1 or not length or address >= flash_limit:
            continue
        end = address + length
        if end > flash_limit or source + length > len(elf):
            raise ValueError("An ELF flash segment is outside the supported image.")
        image.extend(bytes(max(0, end - len(image))))
        image[address:end] = elf[source:source + length]
    return bytes(image)


def build_zig(zig, output, sources=BOOT_SOURCES, *, linker="linker.ld", flash_limit=FLASH_LIMIT,
              hard_float=False):
    """Use the Clang frontend, integrated assembler and LLD shipped in Zig."""
    output.mkdir(parents=True, exist_ok=True)
    zig = zig.resolve()
    executable = str(zig)
    abi = "eabihf" if hard_float else "eabi"
    target = ["-triple", f"thumbv7em-unknown-none-{abi}", "-target-cpu", "cortex-m7"]
    startup_source = PROJECT / sources[0]
    startup = output / startup_source.with_suffix(".o").name
    # Calling these tools directly avoids the Windows Zig driver's build cache.
    startup_flags = ["--defsym", "OOC_HARDFLOAT=1"] if hard_float else []
    tool([executable, "-cc1as", *target, *startup_flags, "-filetype", "obj", "-o", str(startup),
          str(startup_source)])
    codegen = (["-mfloat-abi", "hard", "-target-feature", "+fp-armv8d16sp", "-target-feature", "-fp64"]
               if hard_float else ["-target-feature", "+no-movt"])
    cc = [
        executable, "-cc1", *target, *codegen, "-target-abi", "aapcs",
        "-mrelocation-model", "static", "-mframe-pointer=none",
        "-O2" if hard_float else "-Oz", "-ffreestanding", "-fno-builtin", "-fno-optimize-sibling-calls",
        "-ffunction-sections", "-fdata-sections", "-isystem", str(zig.parent / "lib" / "include"),
    ]
    objects = [str(startup)]
    for name in sources[1:]:
        source = PROJECT / name
        obj = output / source.with_suffix(".o").name
        tool([*cc, "-emit-obj", "-o", str(obj), str(source)])
        tool([*cc, "-S", "-o", str(obj.with_suffix(".s")), str(source)])
        objects.append(str(obj))
    elf = output / "firmware.elf"
    tool([
        executable, "ld.lld", "-T", str(PROJECT / linker), "--gc-sections", "--build-id=none",
        f"-Map={output / 'firmware.map'}", *objects, "-o", str(elf),
    ])
    binary = output / "firmware.bin"
    binary.write_bytes(extract_flash(elf.read_bytes(), flash_limit))
    return binary


def banked_rom(image, *, base=0):
    """Use one packed constant per bank and select a 128-bit line at a time."""
    banks = range((len(image) + FLASH_LIMIT - 1) // FLASH_LIMIT)
    bank_width = max(4, (len(banks) - 1).bit_length())
    rows = [
        "chip M7FirmwareRomBank(offset, contents, line_data) {",
        "    offset[4], contents[2048] = input;",
        "    line_data[128] = output;",
        "    const ZERO11[11] = 0;",
        "    line_shift[11] = (ZERO11 + offset) << 7;",
        "    line_data = contents >> line_shift;",
        "}",
        "",
        "chip M7FirmwareRom(address, word, valid) {",
        "    address[32] = input;",
        "    word[32] = output;",
        "    valid = output;",
        "    const ZERO7[7] = 0;",
        f"    image_offset[32] = address - {base};",
        "    offset[4] = image_offset[7:4];",
        "    shift[7] = (ZERO7 + address[3:2]) << 5;",
        f"    bank[{bank_width}] = image_offset[{bank_width + 7}:8];",
        "    selected_line[128] = wire;",
        f"    valid = address >= {base} && address < {base + len(image)} && address[1:0] == 0;",
        f"    const BANK_COUNT[32] = {len(banks)};",
    ]
    for bank in banks:
        start = bank * FLASH_LIMIT
        data = int.from_bytes(image[start:start + FLASH_LIMIT], "little")
        rows.append(f"    const bank_data{bank}[2048] = {data};")
    rows.extend((
        "    for (int i = 0; i < BANK_COUNT; i++) {",
        "        bank_line${i}[128] = wire;",
        "        M7FirmwareRomBank(offset, bank_data${i}, bank_line${i});",
        "    }",
    ))
    rows.append("    switch (bank) {")
    for bank in banks:
        rows.append(f"        case {bank}: selected_line = bank_line{bank}; break;")
    rows.extend(("        default: selected_line = 0;", "    }",
                 "    word = valid ? (selected_line >> shift) : 0;", "}"))
    return grouped_ports("\n".join(rows) + "\n")


def rom_body(image, *, systick=False, large=False, rtos=False):
    """Validate the boot vectors and emit a little-endian word-addressed ROM."""
    flash_limit = LARGE_FLASH_LIMIT if large or rtos else FLASH_LIMIT
    stack_top = LARGE_STACK_TOP if large or rtos else STACK_TOP
    if not 10 <= len(image) <= flash_limit:
        raise ValueError(f"Make the flash image between 10 and {flash_limit} bytes.")
    stack, reset = struct.unpack_from("<II", image)
    entry = reset & ~1
    vector_size = 64 if systick or rtos else 8
    if stack != stack_top or not (reset & 1 and vector_size <= entry <= len(image) - 2):
        raise ValueError(f"Expected MSP {stack_top:#010x} and a Thumb reset vector inside the flash image.")
    handlers = {11: "SVC", 14: "PendSV", 15: "SysTick"} if rtos else ({15: "SysTick"} if systick else {})
    for exception, name in handlers.items():
        handler, = struct.unpack_from("<I", image, exception * 4)
        if not (handler & 1 and vector_size <= (handler & ~1) <= len(image) - 2):
            raise ValueError(f"Expected a Thumb {name} vector inside the flash image.")

    aligned = image + bytes(-len(image) % 4)
    if len(aligned) > FLASH_LIMIT:
        return banked_rom(aligned)
    rows = [
        "chip M7FirmwareRom(address, word, valid) {",
        "    address[32] = input;",
        "    word[32] = output;",
        "    valid = output;",
        f"    valid = address < {len(aligned)} && address[1:0] == 0;",
        "    switch (address) {",
    ]
    for index, (word,) in enumerate(struct.iter_unpack("<I", aligned)):
        address = 4 * index
        rows.append(f"        case {address:3d}: word = {word:10d}; break;  // 0x{address:02X}: 0x{word:08X}")
    rows.extend(("        default: word = 0;", "    }", "}"))
    return grouped_ports("\n".join(rows) + "\n")


def _model_parts(path, source):
    """Return the directly included files in a firmware model entry."""
    return [path.parent / match[1].decode("utf-8") for match in MODEL_INCLUDE.finditer(source)]


def _model_bytes(path):
    parts = iter(_model_parts(path, source := path.read_bytes()))
    return MODEL_INCLUDE.sub(lambda _: next(parts).read_bytes(), source)


def model_source_paths(path=MODEL):
    """List the modules included by the core assembly."""
    return _model_parts(path, path.read_bytes())


def read_model_source(path=MODEL):
    """Load the entry and its local modules as one source for standalone builds."""
    return _model_bytes(path).decode("utf-8-sig").replace("\r\n", "\n")


def replace_rom(path, body, *, template=None):
    """Replace only the region between one matching pair of firmware markers."""
    source = template or path
    original = _model_bytes(source) if template is not None else source.read_bytes()
    if template is None and BEGIN not in original:
        matches = [(part, data) for part in _model_parts(source, original)
                   if BEGIN in (data := part.read_bytes())]
        if len(matches) == 1:
            path, original = matches[0]
    begin = list(re.finditer(rb"(?m)^" + re.escape(BEGIN) + rb"(\r?\n)", original))
    end = list(re.finditer(rb"(?m)^" + re.escape(END) + rb"(?=\r?\n|\Z)", original))
    if len(begin) != 1 or len(end) != 1 or begin[0].end() > end[0].start():
        raise ValueError(f"Expected one ordered pair of firmware markers in {source}.")

    newline = begin[0][1]
    rom = body.encode("ascii").replace(b"\n", newline)
    updated = original[: begin[0].end()] + rom + original[end[0].start() :]
    if template is None and updated == original:
        return
    staged = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}-", delete=False) as stream:
            staged = Path(stream.name)
            stream.write(updated)
        staged.replace(path)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool-prefix", default="arm-none-eabi-", help="GNU Arm toolchain executable prefix")
    parser.add_argument("--zig", type=Path, help="use this Zig executable instead of GNU Arm tools")
    parser.add_argument("--output", type=Path, help="directory for build products")
    profile = parser.add_mutually_exclusive_group()
    profile.add_argument("--systick", dest="profile", action="store_const", const="systick",
                         help="build or convert the SysTick interrupt example")
    profile.add_argument("--large", dest="profile", action="store_const", const="large",
                         help="build or convert the larger C startup example")
    profile.add_argument("--hardfloat", dest="profile", action="store_const", const="hardfloat",
                         help="build or convert optimized Cortex-M7 C with the hard-float ABI")
    profile.add_argument("--rtos", dest="profile", action="store_const", const="rtos",
                         help="build or convert the two-task scheduler")
    parser.set_defaults(profile="boot")
    parser.add_argument("--binary", type=Path, help="convert an already linked flat binary without rebuilding")
    circuit = parser.add_mutually_exclusive_group()
    circuit.add_argument("--replace", type=Path, help="rewrite the firmware markers in an existing OOC file")
    circuit.add_argument("--circuit-output", type=Path, help="write a standalone OOC circuit with the built image")
    args = parser.parse_args()
    default_output = PROJECT / "build"
    if args.profile != "boot":
        default_output /= args.profile
    output = (args.output or default_output).resolve()
    sources, linker, flash_limit = {
        "boot": (BOOT_SOURCES, "linker.ld", FLASH_LIMIT),
        "systick": (SYSTICK_SOURCES, "linker.ld", FLASH_LIMIT),
        "large": (LARGE_SOURCES, "linker_large.ld", LARGE_FLASH_LIMIT),
        "hardfloat": (HARDFLOAT_SOURCES, "linker_large.ld", LARGE_FLASH_LIMIT),
        "rtos": (RTOS_SOURCES, "linker_rtos.ld", LARGE_FLASH_LIMIT),
    }[args.profile]
    try:
        binary = args.binary
        hard_float = args.profile == "hardfloat"
        if binary is None:
            if args.zig:
                binary = build_zig(args.zig, output, sources, linker=linker, flash_limit=flash_limit,
                                   hard_float=hard_float)
            else:
                binary = build_gnu(args.tool_prefix, output, sources, linker=linker, hard_float=hard_float)
        body = rom_body(binary.read_bytes(), systick=args.profile == "systick",
                        large=args.profile in {"large", "hardfloat"}, rtos=args.profile == "rtos")
        output.mkdir(parents=True, exist_ok=True)
        block = output / "firmware.rom.ckt"
        block.write_text(f"{BEGIN.decode()}\n{body}{END.decode()}\n", encoding="ascii")
        if args.replace is not None:
            replace_rom(args.replace, body)
        elif args.circuit_output is not None:
            replace_rom(args.circuit_output, body, template=MODEL)
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(block)


if __name__ == "__main__":
    main()
