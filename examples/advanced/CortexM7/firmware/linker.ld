ENTRY(Reset_Handler)

MEMORY
{
    FLASH (rx) : ORIGIN = 0x00000000, LENGTH = 256
    SRAM (rw)  : ORIGIN = 0x20000000, LENGTH = 1K
}

__stack_top = ORIGIN(SRAM) + LENGTH(SRAM);
firmware_result = ORIGIN(SRAM);
systick_ticks = ORIGIN(SRAM) + 4;
systick_ctrl = 0xE000E010;
systick_load = 0xE000E014;
systick_value = 0xE000E018;

SECTIONS
{
    .vectors ORIGIN(FLASH) :
    {
        KEEP(*(.vectors))
    } > FLASH

    .text :
    {
        *(.text.Reset_Handler)
        *(.text.firmware_main)
        *(.text.nested_add)
        *(.text.leaf_add)
        *(.text.systick_main)
        *(.text.SysTick_Handler)
        *(.text .text.* .rodata .rodata.*)
        . = ALIGN(4);
    } > FLASH

    /DISCARD/ :
    {
        *(.ARM.exidx* .ARM.extab* .comment .note*)
    }
}

ASSERT(SIZEOF(.vectors) == 8 || SIZEOF(.vectors) == 64, "Expected two boot words or all 16 core vectors.")
