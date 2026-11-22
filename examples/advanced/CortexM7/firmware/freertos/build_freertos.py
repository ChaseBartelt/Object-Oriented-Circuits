"""Link a FreeRTOS V11.1.0 Cortex-M port and generate an OOC firmware ROM."""

import argparse
from pathlib import Path
import re
import runpy
import subprocess


PROJECT = Path(__file__).resolve().parent
FIRMWARE = PROJECT.parent
VERSION = "V11.1.0"


def assemble(tool, zig, source, output, *, m7, define=None):
    cpu = "cortex-m7" if m7 else "cortex-m3"
    architecture = "thumbv7em" if m7 else "thumbv7m"
    symbols = ("--defsym", define) if define else ()
    tool([
        str(zig), "-cc1as", "-triple", f"{architecture}-none-none-eabi", "-target-cpu", cpu,
        *symbols, "-filetype", "obj", "-o", str(output), str(source),
    ])


def target_assembly(assembly, *, m7):
    """Select Thumb-2 assembly for the chosen port's inline system instructions."""
    source = assembly.read_text(encoding="utf-8")
    cpu = r"\g<1>cortex-m7\n\t.fpu\tfpv5-sp-d16" if m7 else r"\g<1>cortex-m3"
    source, count = re.subn(r"(?m)^([ \t]*\.cpu[ \t]+)cortex-m0\b", cpu, source)
    if count != 1:
        raise ValueError(f"Expected one Clang Cortex-M0 CPU directive in {assembly.name}.")
    # Match the object metadata to the selected ARMv7-M or ARMv7E-M instructions.
    architecture = r"\g<1>13" if m7 else r"\g<1>10"
    source = re.sub(r"(?m)^([ \t]*\.eabi_attribute[ \t]+6,[ \t]*)12\b", architecture, source)
    source = re.sub(r"(?m)^([ \t]*\.eabi_attribute[ \t]+9,[ \t]*)1\b", r"\g<1>2", source)
    assembly.write_text(source, encoding="utf-8")


def build(drivers, zig, kernel, output, *, blocking=False, m7=False, floating_point=False,
          lazy_fp=False, cmsis=None):
    header = (kernel / "include" / "task.h").read_text(encoding="utf-8")
    match = re.search(r'(?m)^#define[ \t]+tskKERNEL_VERSION_NUMBER[ \t]+"([^"]+)"', header)
    if match is None or match[1] != VERSION:
        raise ValueError(f"Use the official FreeRTOS-Kernel {VERSION} source tree.")

    output.mkdir(parents=True, exist_ok=True)
    tool = drivers["tool"]
    floating_point = floating_point or lazy_fp
    m7 = m7 or floating_point or cmsis is not None
    gcc_ports = kernel / "portable" / "GCC"
    port = gcc_ports / "ARM_CM7" / "r0p1" if m7 else gcc_ports / "ARM_CM3"
    headers = zig.parent / "lib"
    libc = headers / "libc" / "include"
    # Narrow C code fits the simulator's instruction subset. Assemble the original
    # inline instructions for the selected port's architecture.
    compiler = [
        str(zig), "-cc1", "-triple", "thumbv6m-none-none-eabi", "-target-cpu", "cortex-m0",
        "-target-feature", "+no-movt", "-target-abi", "aapcs", "-mrelocation-model", "static",
        "-mframe-pointer=none", "-Oz", "-ffreestanding", "-fno-builtin", "-ffunction-sections",
        "-fdata-sections", "-Wno-unknown-attributes", "-no-integrated-as",
    ]
    if m7:
        # The r0p1 port supplies VFP assembly even though its C code uses the integer ABI.
        compiler.extend(("-D", "__VFP_FP__=1"))
    for directory in (headers / "include", libc / "arm-linux-musl", libc / "generic-musl",
                      libc / "arm-linux-any", libc / "any-linux-any"):
        compiler.extend(("-isystem", str(directory)))
    demo = PROJECT / "cmsis" if cmsis is not None else (PROJECT / "blocking" if blocking else PROJECT)
    for directory in dict.fromkeys((demo, PROJECT, kernel / "include", port)):
        compiler.extend(("-I", str(directory)))

    application = demo / "demo.c"
    assembly_sources = [PROJECT / "startup.S"]
    linker = PROJECT / "linker.ld"
    sources = [PROJECT / "runtime.c", kernel / "tasks.c", kernel / "list.c", port / "port.c"]
    if cmsis is not None:
        device = cmsis / "Device" / "ARM" / "ARMCM7"
        # Clang's freestanding frontend selects the CMSIS GCC compiler header.
        compiler.extend((
            "-I", str(device / "Include"), "-I", str(cmsis / "CMSIS" / "Core" / "Include"),
            "-fgnuc-version=4.2.1",
            "-D", "ARMCM7_SP", "-D", "vPortSVCHandler=SVC_Handler",
            "-D", "xPortPendSVHandler=PendSV_Handler", "-D", "xPortSysTickHandler=SysTick_Handler",
        ))
        assembly_sources = [device / "Source" / "GCC" / "startup_ARMCM7.S", demo / "faults.S"]
        sources.append(device / "Source" / "system_ARMCM7.c")
        linker = demo / "linker.ld"
    elif blocking:
        application = demo / "blocking.c"
        assembly_sources.append(demo / "irq_vector.S")
        sources.append(kernel / "queue.c")
    elif floating_point:
        application = PROJECT / "floating_point" / "fp_demo.c"
        assembly_sources.append(application.with_name("fp_task.S"))
    sources.insert(0, application)
    objects = []
    for source in assembly_sources:
        obj = output / source.with_suffix(".o").name
        define = "DEMO_LAZY_FP=1" if lazy_fp and source.name == "fp_task.S" else None
        assemble(tool, zig, source, obj, m7=m7, define=define)
        objects.append(str(obj))
    for source in sources:
        assembly = output / f"{source.stem}.s"
        obj = assembly.with_suffix(".o")
        tool([*compiler, "-S", "-o", str(assembly), str(source)])
        target_assembly(assembly, m7=m7)
        assemble(tool, zig, assembly, obj, m7=m7)
        objects.append(str(obj))

    elf = output / "firmware.elf"
    tool([
        str(zig), "ld.lld", "--gc-sections", "--build-id=none", "-T", str(linker),
        f"-Map={output / 'firmware.map'}", *objects, "-o", str(elf),
    ])
    image = drivers["extract_flash"](elf.read_bytes(), drivers["LARGE_FLASH_LIMIT"])
    (output / "firmware.bin").write_bytes(image)
    return image


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", type=Path, required=True, help=f"official FreeRTOS-Kernel {VERSION} directory")
    parser.add_argument("--zig", type=Path, required=True, help="path to Zig 0.15.2")
    workload = parser.add_mutually_exclusive_group()
    workload.add_argument("--blocking", action="store_true", help="build the task delay and external IRQ queue demo")
    workload.add_argument("--floating-point", action="store_true",
                          help="build two Cortex-M7 floating-point tasks with immediate FP stacking")
    workload.add_argument("--lazy-fp", action="store_true",
                          help="build the same floating-point tasks with lazy FP stacking")
    workload.add_argument("--cmsis", type=Path,
                          help="build the IRQ and configurable fault demo with CMSIS_5 tag 5.9.0")
    parser.add_argument("--m7", action="store_true", help="use the upstream Cortex-M7 r0p1 port")
    parser.add_argument("--output", type=Path, help="directory for build products")
    parser.add_argument("--circuit-output", type=Path, help="write a standalone OOC circuit with the linked firmware")
    args = parser.parse_args()
    m7 = args.m7 or args.floating_point or args.lazy_fp or args.cmsis is not None
    profile = ("-m7" if m7 else "") + ("-blocking" if args.blocking else "")
    if args.floating_point or args.lazy_fp:
        profile += "-fp-lazy" if args.lazy_fp else "-fp"
    if args.cmsis is not None:
        profile += "-cmsis"
    output = (args.output or FIRMWARE / "build" / f"freertos{profile}").resolve()
    drivers = runpy.run_path(str(FIRMWARE / "build_firmware.py"))
    try:
        image = build(drivers, args.zig.resolve(), args.kernel.resolve(), output,
                      blocking=args.blocking, m7=m7, floating_point=args.floating_point, lazy_fp=args.lazy_fp,
                      cmsis=args.cmsis.resolve() if args.cmsis is not None else None)
        body = drivers["rom_body"](image, rtos=True)
        begin, end = drivers["BEGIN"].decode(), drivers["END"].decode()
        block = output / "firmware.rom.ckt"
        block.write_text(f"{begin}\n{body}{end}\n", encoding="ascii")
        if args.circuit_output is not None:
            drivers["replace_rom"](args.circuit_output, body, template=drivers["MODEL"])
    except (OSError, subprocess.CalledProcessError, ValueError) as error:
        parser.exit(1, f"{error}\n")
    print(block)


if __name__ == "__main__":
    main()
