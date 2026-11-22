ENTRY(Reset_Handler)

MEMORY
{
    FLASH (rx) : ORIGIN = 0x00000000, LENGTH = 4K
    SRAM (rw)  : ORIGIN = 0x20000000, LENGTH = 4K
}

__stack_top = ORIGIN(SRAM) + LENGTH(SRAM);

SECTIONS
{
    .vectors ORIGIN(FLASH) :
    {
        KEEP(*(.vectors))
    } > FLASH

    .text :
    {
        *(.text.Reset_Handler)
        *(.text.large_main)
        *(.text .text.* .rodata .rodata.*)
        . = ALIGN(4);
    } > FLASH

    .results (NOLOAD) :
    {
        large_result = .;
        . += 4;
        large_status = .;
        . += 4;
    } > SRAM

    .data :
    {
        . = ALIGN(4);
        __data_start = .;
        *(.data .data.*)
        . = ALIGN(4);
        __data_end = .;
    } > SRAM AT> FLASH
    __data_load = LOADADDR(.data);

    .bss (NOLOAD) :
    {
        . = ALIGN(4);
        __bss_start = .;
        *(.bss .bss.* COMMON)
        . = ALIGN(4);
        __bss_end = .;
    } > SRAM

    /DISCARD/ :
    {
        *(.ARM.exidx* .ARM.extab* .comment .note*)
    }
}

ASSERT(SIZEOF(.vectors) == 8, "Expected the initial MSP and Thumb reset vector.")
ASSERT(LOADADDR(.data) + SIZEOF(.data) > 256, "Expected a workload larger than the small ROM.")
