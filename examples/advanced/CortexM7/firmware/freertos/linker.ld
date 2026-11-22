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
        KEEP(*(.vectors.irq0))
    } > FLASH

    .text :
    {
        *(.text.Reset_Handler)
        *(.text .text.* .rodata .rodata.*)
        . = ALIGN(4);
    } > FLASH

    .results (NOLOAD) :
    {
        counter_a = .;
        . += 4;
        counter_b = .;
        . += 4;
        demo_status = .;
        . += 4;
        KEEP(*(.results.extra))
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
        . = ALIGN(8);
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

ASSERT(SIZEOF(.vectors) == 64 || SIZEOF(.vectors) == 68, "Expected the core vectors and optional IRQ0.")
ASSERT(__stack_top - __bss_end >= 768, "Reserve room for the handler stack.")
