"""Place a linked STM32H750 boot image onto the reusable OOC Cortex-M7 core."""

from os.path import relpath
from pathlib import Path
import re
import runpy
import struct


HERE = Path(__file__).resolve().parent
CORE = HERE.parents[1] / "CortexM7_Firmware.ckt"
BOARD_SOURCES = (
    "STM32H750_StartupRegisters.ckt", "STM32H750BoardMemory.ckt",
    "STM32H750_DaisyAudio.ckt", "STM32H750ByteRAM.ckt", "STM32H750_Serial.ckt",
)
FLASH_BASE = 0x08000000
FLASH_SIZE = 128 * 1024
RAM_PAGE = 1024
STACK_BYTES = 4096
RAM_REGIONS = (
    (0x00000000, 0x00010000),  # ITCM
    (0x20000000, 0x20020000),  # DTCM
    (0x24000000, 0x24080000),  # AXI SRAM
    (0x30000000, 0x30048000),  # SRAM1–3
    (0x38000000, 0x38010000),  # SRAM4
    (0x38800000, 0x38801000),  # Backup SRAM
    (0xC0000000, 0xC4000000),  # FMC bank 1 SDRAM
)
AUDIO_OUTPUTS = (
    ("AUDIO_IN_LEFT", 24, 0), ("AUDIO_IN_RIGHT", 24, 32),
    ("AUDIO_OUT_LEFT", 24, 64), ("AUDIO_OUT_RIGHT", 24, 96),
    ("AUDIO_FRAMES", 32, 128), ("QSPI_READY", 1, 160), ("SDRAM_READY", 1, 161),
)


SERIAL_OUTPUTS = tuple(
    (f"{peripheral}_{field}", 32, lane * 128 + word * 32)
    for lane, peripheral in enumerate(("SPI1", "I2C1", "I2C2"))
    for word, field in enumerate(("TX", "RX", "TRANSFERS", "STATUS"))
)


def _ram_windows(regions, stack_top, probes):
    stack_base = next((base for base, end in RAM_REGIONS if base < stack_top <= end), None)
    if stack_base is None or stack_top % 8:
        raise ValueError("The initial MSP must be 8-byte aligned and in STM32H750 RAM.")
    stack_start = max(stack_top - STACK_BYTES, stack_base)
    regions = [*regions, (stack_start, stack_top - stack_start)]
    pages = set()
    for address, size in regions:
        if size == 0:
            continue
        if size < 0 or not any(base <= address < address + size <= end for base, end in RAM_REGIONS):
            raise ValueError(f"Writable ELF range {address:#010x} + {size} byte(s) is outside modeled SRAM.")
        pages.update(range(address & -RAM_PAGE, address + size, RAM_PAGE))

    for name, address in probes.items():
        if address % 4 or (address & -RAM_PAGE) not in pages:
            raise ValueError(f"Probe {name} must address an aligned word in an allocated SRAM page.")

    # The RAM page exposes its first eleven words for inspection. Split at a
    # distant probe so its value comes from the same storage used by the CPU.
    windows = []
    for page in sorted(pages):
        start = page
        for address in sorted(set(probes.values())):
            if start + 44 <= address < page + RAM_PAGE:
                windows.append((start, address))
                start = address
        windows.append((start, page + RAM_PAGE))
    return windows


def _memory_window(audio):
    """Wrap one independently inspectable SRAM page with its physical decode."""
    rows = [
        "chip M7FirmwareMemoryWindow(clk, reset, write_enable, address, last_address, byte_count,",
        "                           size_code, write_data, base, limit, ready,",
        "                           data, mapped, first_hit, last_hit, first_words) {",
        "    clk, reset, write_enable, ready = input;",
        "    address[32], last_address[32], write_data[32], base[32], limit[32] = input;",
        "    size_code[2], byte_count[3] = input;",
        "    data[32], first_words[352], mapped, first_hit, last_hit = output;",
        "    first_hit = address >= base && address < limit;",
        "    last_hit = last_address >= base && last_address < limit;",
        "    page_data[32], page_write = wire;",
        "    page_write = write_enable && mapped;",
    ]
    if audio:
        rows.extend((
            "    mapped = ready && address < limit && (address >= base || base - address < byte_count);",
            "    offset[11] = mapped ? address - base : 0;",
            "    length[11] = limit - base;",
            "    payload[32] = page_write ? write_data : 0;",
            "    STM32H750ByteRAM(clk, reset, page_write, offset, length, size_code, payload, page_data, first_words);",
        ))
    else:
        rows.extend((
            "    mapped = ready && first_hit;",
            "    offset[10] = address - base;",
            "    M7FirmwareRAMPage(clk, reset, page_write, offset, size_code, write_data, page_data, first_words);",
        ))
    rows.extend(("    data = mapped ? page_data : 0;", "}"))
    return "\n".join(rows)


def _memory_bank(windows, probes, audio):
    """Describe the page array once, retaining probe order and split-page reads."""
    count = len(windows)
    if not count:
        raise ValueError("A memory bank requires at least one SRAM window.")
    rows = [
        _memory_window(audio),
        "chip M7FirmwareMemoryBank(clk, reset, write_enable, address, size_code, write_data, external_ready,",
        "                         data, mapped, range_valid, first_words) {",
        "    clk, reset, write_enable, external_ready = input;",
        "    address[32], write_data[32], size_code[2] = input;",
        "    data[32], mapped, range_valid, first_words[352] = output;",
        f"    const PAGE_COUNT[32] = {count};",
    ]
    # Chunk constant tables at the ordinary maximum signal width. Increasing
    # the allocated memory does not require wider language primitives.
    for group in range(0, count, 128):
        chunk = windows[group:group + 128]
        width = 32 * len(chunk)
        bases = sum(start << (32 * index) for index, (start, _) in enumerate(chunk))
        limits = sum(end << (32 * index) for index, (_, end) in enumerate(chunk))
        rows.extend((f"    const BASE_TABLE_{group // 128}[{width}] = {bases};",
                     f"    const LIMIT_TABLE_{group // 128}[{width}] = {limits};"))
    external = sum(1 << index for index, (start, _) in enumerate(windows)
                   if audio and start >= 0xC0000000)
    rows.extend((
        f"    const EXTERNAL_PAGES[{count}] = {external};",
        "    byte_count[3] = size_code == 0 ? 1 : size_code == 1 ? 2 : 4;",
        "    last_address[32] = address + byte_count - 1;",
        "    store_enable = wire;",
        f"    store_enable = write_enable{' && range_valid' if audio else ''};",
        "    decoded0, first0, last0 = wire;",
        "    data_or0[32] = wire;",
        "    decoded0 = 0; first0 = 0; last0 = 0; data_or0 = 0;",
        "    for (int i = 0; i < PAGE_COUNT; i++) {",
        "        sram${i}_base[32], sram${i}_limit[32], sram${i}_data[32], sram${i}_words[352] = wire;",
        "        sram${i}_ready, sram${i}_mapped, sram${i}_first, sram${i}_last = wire;",
        "        sram${i}_base = BASE_TABLE_${i / 128}[32 * (i % 128) + 31:32 * (i % 128)];",
        "        sram${i}_limit = LIMIT_TABLE_${i / 128}[32 * (i % 128) + 31:32 * (i % 128)];",
        "        sram${i}_ready = !EXTERNAL_PAGES[i] || external_ready;",
        "        M7FirmwareMemoryWindow(clk, reset, store_enable, address, last_address, byte_count,",
        "                               size_code, write_data, sram${i}_base, sram${i}_limit, sram${i}_ready,",
        "                               sram${i}_data, sram${i}_mapped, sram${i}_first, sram${i}_last, sram${i}_words);",
        "        decoded${i + 1}, first${i + 1}, last${i + 1} = wire;",
        "        data_or${i + 1}[32] = wire;",
        "        decoded${i + 1} = decoded${i} || sram${i}_mapped;",
        "        first${i + 1} = first${i} || sram${i}_first;",
        "        last${i + 1} = last${i} || sram${i}_last;",
        "        data_or${i + 1} = data_or${i} | sram${i}_data;",
        "    }",
        f"    range_valid = first{count} && last{count};",
        f"    mapped = {'range_valid && ' if audio else ''}decoded{count};",
        f"    data = data_or{count};",
    ))
    probe_data = {}
    for index, (start, end) in enumerate(windows):
        for name, address in probes.items():
            if start <= address < end:
                low = (address - start) * 8
                probe_data[name] = f"sram{index}_words[{low + 31}:{low}]"
    for index, name in enumerate(probes):
        value = probe_data[name]
        rows.append(f"    first_words[{32 * index + 31}:{32 * index}] = {value};")
    if len(probes) < 11:
        rows.append(f"    first_words[351:{32 * len(probes)}] = 0;")
    rows.extend(("}", ""))
    return "\n".join(rows)


def _platform(windows, probes, audio):
    rows = [
        _memory_bank(windows, probes, audio),
        "chip M7FirmwarePlatform(clk, reset, read_enable, write_enable, address, size_code, write_data,",
        "                        reset_base, data, mapped, first_words, platform_status,",
        "                        audio_board, irq_numbers, device_irqs, audio_trace, serial_trace, peripheral_read) {",
        "    clk, reset, read_enable, write_enable, peripheral_read = input;",
        "    address[32], write_data[32], size_code[2] = input;",
        "    reset_base[32], data[32], first_words[352] = output;",
        "    mapped, platform_status[2], audio_board, irq_numbers[10], device_irqs[240], audio_trace[162], serial_trace[384] = output;",
        f"    reset_base = {FLASH_BASE};",
        "    registers_data[32], shared_ram_data[32] = wire;",
        "    registers_mapped, board_led, sysclk_pll, shared_ram_mapped, ram_range_valid = wire;",
        "    qspi_clock, fmc_clock, tim2_clock, rng_clock, qspi_reset, fmc_reset = wire;",
        "    dma1_clock, sai1_clock, codec_reset_n = wire;",
        "    serial_clocks[3], serial_resets[3], serial_data[32], serial_mapped, serial_irqs[240] = wire;",
        "    STM32H750StartupRegisters(clk, reset, address, size_code, write_data, write_enable,",
        "                               registers_data, registers_mapped, board_led, sysclk_pll,",
        "                               qspi_clock, fmc_clock, tim2_clock, rng_clock, qspi_reset, fmc_reset,",
        "                               dma1_clock, sai1_clock, codec_reset_n, serial_clocks, serial_resets);",
        "    STM32H750Serial(clk, reset, peripheral_read, write_enable, address, size_code, write_data,",
        "                    serial_clocks, serial_resets, serial_data, serial_mapped, serial_irqs, serial_trace);",
        "    platform_status[0] = board_led;",
        "    platform_status[1] = sysclk_pll;",
    ]
    if audio:
        rows.extend((
            _audio_devices(),
            "    M7FirmwareMemoryBank(clk, reset, ram_write, ram_address, ram_size, ram_write_data, sdram_ready,",
            "                         shared_ram_data, shared_ram_mapped, ram_range_valid, first_words);",
            "    mapped = registers_mapped || board_mapped || audio_mapped || serial_mapped || shared_ram_mapped;",
            "    data = (registers_mapped ? registers_data : 0) | (board_mapped ? board_data : 0) | (audio_mapped ? audio_data : 0) | (serial_mapped ? serial_data : 0) | shared_ram_data;",
        ))
    else:
        rows.extend((
            "    audio_board = 0; irq_numbers = 32; device_irqs = serial_irqs; audio_trace = 0;",
            "    memory_ready = wire; memory_ready = 1;",
            "    M7FirmwareMemoryBank(clk, reset, write_enable, address, size_code, write_data, memory_ready,",
            "                         shared_ram_data, shared_ram_mapped, ram_range_valid, first_words);",
            "    mapped = registers_mapped || serial_mapped || shared_ram_mapped;",
            "    data = (registers_mapped ? registers_data : 0) | (serial_mapped ? serial_data : 0) | shared_ram_data;",
        ))
    rows.append("}")
    return "\n".join(rows)


def _audio_devices():
    return """    audio_board = 1;
    irq_numbers = 395; // DMA1 Stream 0 is IRQ11, Stream 1 is IRQ12.
    qspi_ready, sdram_ready, board_mapped, audio_mapped = wire;
    board_data[32], audio_data[32] = wire;
    dma_request, dma_write, dma_grant, dma_selected, ram_write = wire;
    dma_irq0, dma_irq1 = wire;
    dma_address[32], dma_write_data[32] = wire;
    input_left[32], input_right[32], output_left[32], output_right[32], frame_count[32] = wire;
    ram_address[32], ram_write_data[32] = wire;
    ram_size[2] = wire;
    STM32H750BoardMemory(clk, reset, address, size_code, write_data, write_enable,
                         qspi_clock, fmc_clock, tim2_clock, rng_clock, qspi_reset, fmc_reset,
                         board_data, board_mapped, qspi_ready, sdram_ready);
    STM32H750DaisyAudio(clk, reset, address, size_code, write_data, write_enable,
                       sai1_clock, dma1_clock, codec_reset_n, dma_grant, shared_ram_data,
                       audio_data, audio_mapped, dma_request, dma_write, dma_address, dma_write_data,
                       dma_irq0, dma_irq1, output_left, output_right, frame_count, input_left, input_right);
    // Instruction fetch uses its own ROM port. CPU data transfers keep priority
    // over DMA on this SRAM bus. DMA1 cannot access the tightly coupled RAM.
    dma_selected = (dma_request && !read_enable && !write_enable && dma_address[1:0] == 0 &&
                    dma_address >= 65536 &&
                    !(dma_address >= 536870912 && dma_address < 537001984));
    dma_grant = dma_selected && shared_ram_mapped;
    ram_address = dma_selected ? dma_address : (read_enable || write_enable ? address : 4294967295);
    ram_size = dma_selected ? 2 : size_code;
    ram_write_data = dma_selected ? dma_write_data : write_data;
    ram_write = ram_range_valid && (write_enable || (dma_grant && dma_write));
    const IRQ_ONE[240] = 1;
    device_irqs = serial_irqs | (dma_irq0 ? IRQ_ONE << 11 : 0) | (dma_irq1 ? IRQ_ONE << 12 : 0);
    audio_trace[31:0] = input_left;
    audio_trace[63:32] = input_right;
    audio_trace[95:64] = output_left;
    audio_trace[127:96] = output_right;
    audio_trace[159:128] = frame_count;
    audio_trace[160] = qspi_ready;
    audio_trace[161] = sdram_ready;"""


def _compact_rom(image):
    """Select 512-byte flash pages without cloning a chip for every page."""
    pages = [image[start:start + 512] for start in range(0, len(image), 512)]
    groups = (len(pages) + 15) // 16
    rows = [
        "chip M7FirmwareRom(address, word, valid) {",
        "    address[32] = input;",
        "    word[32], valid = output;",
        f"    offset[32] = address - {FLASH_BASE};",
        "    const ZERO12[12] = 0;",
        "    shift[12] = (ZERO12 + offset[8:2]) << 5;",
        "    page_select[4] = offset[12:9];",
        "    group_select[4] = offset[16:13];",
        "    selected[4096] = wire;",
        f"    valid = address >= {FLASH_BASE} && address < {FLASH_BASE + len(image)} && address[1:0] == 0;",
    ]
    for index, page in enumerate(pages):
        rows.append(f"    const page{index}[4096] = {int.from_bytes(page, 'little')};")
    for group in range(groups):
        rows.extend((f"    group{group}[4096] = wire;", "    switch (page_select) {"))
        for index in range(group * 16, min(len(pages), (group + 1) * 16)):
            rows.append(f"        case {index % 16}: group{group} = page{index}; break;")
        rows.extend((f"        default: group{group} = 0;", "    }"))
    rows.append("    switch (group_select) {")
    for group in range(groups):
        rows.append(f"        case {group}: selected = group{group}; break;")
    rows.extend(("        default: selected = 0;", "    }", "    word = valid ? selected >> shift : 0;", "}"))
    return "\n".join(rows)


def _replace_region(source, label, body):
    pattern = rf"(?ms)(^// BEGIN {label}\n).*?(^// END {label}$)"
    source, matches = re.subn(pattern, lambda match: match[1] + body.rstrip() + "\n" + match[2], source)
    if matches != 1:
        raise ValueError(f"Expected one {label} region in {CORE.name}.")
    return source


def render_demo(image, ram_regions, stack_top, main, probes, *, audio=False):
    """Render a self-contained viewer circuit from physical ELF load addresses."""
    if not 8 <= len(image) <= FLASH_SIZE or main & 1:
        raise ValueError("Expected a linked internal-flash image and an even Thumb code address for main.")
    stack, reset = struct.unpack_from("<II", image)
    if (stack != stack_top or not reset & 1
            or any(not FLASH_BASE <= address <= FLASH_BASE + len(image) - 2
                   for address in (reset & ~1, main))):
        raise ValueError("The ELF must have an internal-flash vector table and Thumb entry points.")
    if len(probes) > 11 or any(not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) for name in probes):
        raise ValueError("Use up to eleven distinct uppercase signal names for SRAM probes.")
    if audio:
        ram_regions = [(0, RAM_PAGE), *ram_regions]
    windows = _ram_windows(ram_regions, stack_top, probes)
    core_builder = runpy.run_path(str(HERE.parent / "build_firmware.py"))
    banked_rom = core_builder["banked_rom"]
    image += b"\xff" * (-len(image) % 4)
    flash = _compact_rom(image) if audio else banked_rom(image, base=FLASH_BASE)
    source = _replace_region(core_builder["read_model_source"](CORE), "LINKED FIRMWARE", flash)
    registers = "\n".join(
        (HERE / name).read_text(encoding="utf-8")
        for name in (BOARD_SOURCES if audio else (BOARD_SOURCES[0], BOARD_SOURCES[-1]))
    )
    source = _replace_region(source, "FIRMWARE PLATFORM", registers + "\n" + _platform(windows, probes, audio))
    head, sep, assembly = source.partition("main CORTEX_M7_FIRMWARE {")
    if not sep:
        raise ValueError(f"Could not find the firmware assembly in {CORE.name}.")
    reserved = {*re.findall(r"\b[A-Z][A-Z0-9_]*\b", assembly),
                "MAIN_ADDRESS", "AT_MAIN", "BOARD_LED", "SYSCLK_PLL"}
    reserved.update(name for name, _, _ in (*AUDIO_OUTPUTS, *SERIAL_OUTPUTS))
    if reserved.intersection(probes):
        raise ValueError("An SRAM probe name conflicts with an existing firmware signal.")
    probe_names = {f"RAM{index}": name for index, name in enumerate(probes)}
    assembly = re.sub(r"\bRAM\d+\b", lambda match: probe_names.get(match[0], match[0]), assembly)
    audio_chip, audio_ports = _audio_probes() if audio else ("", "")
    serial_chip, serial_ports = _serial_probes()
    return (f"// Generated for STM32H750 internal-flash boot, main at 0x{main:08X}.\n"
            + head[head.index("\n\n") + 2:]
            + "chip STM32H750ViewerProbes(pc, status, entry, at_main, board_led, sysclk_pll) {\n"
            + "    pc[32], status[2] = input;\n    entry[32], at_main, board_led, sysclk_pll = output;\n"
            + f"    entry = {main};\n    at_main = pc == entry;\n"
            + "    board_led = status[0];\n    sysclk_pll = status[1];\n}\n\n"
            + serial_chip + audio_chip + ("main STM32H750_AUDIO {\n" if audio else "main STM32H750_BOOT {\n")
            + "    BOARD_LED, SYSCLK_PLL, MAIN_ADDRESS[32], AT_MAIN = output;\n"
            + "    STM32H750ViewerProbes(PC, platform_status, MAIN_ADDRESS, AT_MAIN, BOARD_LED, SYSCLK_PLL);\n"
            + serial_ports + audio_ports
            + assembly)


def write_circuit(path, source, *, standalone=False):
    """Write either the short viewer assembly with includes or a single file."""
    path = path.resolve()
    files = {}
    core_builder = runpy.run_path(str(HERE.parent / "build_firmware.py"))
    if not standalone:
        def include(target):
            relative = Path(relpath(target, path.parent)).as_posix()
            return f'include("{relative}");\n'

        def linked_region(label):
            match = re.search(rf"(?ms)^// BEGIN {label}\n.*?^// END {label}\n", source)
            if not match:
                raise ValueError(f"Expected the {label} region in generated source.")
            return match[0]

        rom = linked_region("LINKED FIRMWARE")
        registers = linked_region("FIRMWARE PLATFORM")
        probe_start = source.index("chip STM32H750ViewerProbes(")
        main_start = source.index("\nmain ", probe_start) + 1
        probes = source[probe_start:main_start]
        source = source[:probe_start] + source[main_start:]

        rom_path = path.parent / "firmware" / "images" / f"{path.stem}_rom.ckt"
        platform_path = path.parent / "boards" / f"{path.stem}_platform.ckt"
        board_includes = []
        platform = registers
        for name in BOARD_SOURCES:
            board_path = HERE / name
            board_text = board_path.read_text(encoding="utf-8").rstrip()
            if board_text in platform:
                platform = platform.replace(board_text, "", 1)
                board_includes.append(include(board_path))

        for core_path in core_builder["model_source_paths"](CORE):
            if core_path.name == "ThumbBoot.ckt":
                source = source.replace(rom, include(rom_path), 1)
            elif core_path.name == "default_platform.ckt":
                source = source.replace(registers, "".join(board_includes) + include(platform_path), 1)
            else:
                module = core_path.read_text(encoding="utf-8")
                if module not in source:
                    raise ValueError(f"Expected the {core_path.name} module in generated source.")
                source = source.replace(module, include(core_path), 1)
        files[rom_path] = rom
        files[platform_path] = platform + "\n" + probes
    files[path] = source
    for output, content in files.items():
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(core_builder["grouped_ports"](content), encoding="utf-8")


def _trace_probes(name, width, live, outputs):
    pins = ", ".join(pin for pin, _, _ in outputs)
    declarations = ", ".join(f"{pin}[{bits}]" if bits > 1 else pin for pin, bits, _ in outputs)
    rows = [f"chip {name}(trace, {pins}) {{", f"    trace[{width}] = input;",
            f"    {declarations} = output;"]
    for pin, bits, low in outputs:
        selection = str(low) if bits == 1 else f"{low + bits - 1}:{low}"
        rows.append(f"    {pin} = trace[{selection}];")
    rows.extend(("}", ""))
    return "\n".join(rows), f"    {declarations} = output;\n    {name}({live}, {pins});\n"


def _audio_probes():
    return _trace_probes("STM32H750AudioProbes", 162, "audio_trace", AUDIO_OUTPUTS)


def _serial_probes():
    return _trace_probes("STM32H750SerialProbes", 384, "serial_trace", SERIAL_OUTPUTS)
