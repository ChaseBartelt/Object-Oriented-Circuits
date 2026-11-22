ENTRY(Reset_Handler)

MEMORY
{
    FLASH (rx) : ORIGIN = 0x00000000, LENGTH = 4K
    SRAM (rw)  : ORIGIN = 0x20000000, LENGTH = 4K
}

__StackTop = ORIGIN(SRAM) + LENGTH(SRAM);

SECTIONS
{
    .text ORIGIN(FLASH) :
    {
        KEEP(*(.vectors))
        *(.text .text.* .rodata .rodata.*)
        . = ALIGN(4);
    } > FLASH

    .copy.table :
    {
        __copy_table_start__ = .;
        LONG(LOADADDR(.data))
        LONG(ADDR(.data))
        LONG(SIZEOF(.data) / 4)
        __copy_table_end__ = .;
    } > FLASH

    .zero.table :
    {
        __zero_table_start__ = .;
        LONG(ADDR(.results))
        LONG(SIZEOF(.results) / 4)
        LONG(ADDR(.bss))
        LONG(SIZEOF(.bss) / 4)
        __zero_table_end__ = .;
    } > FLASH

    .results (NOLOAD) :
    {
        counter_a = .;
        . += 4;
        counter_b = .;
        . += 4;
        demo_status = .;
        . += 4;
        irq0_count = .;
        . += 4;
        irq1_count = .;
        . += 4;
        usage_fault_status = .;
        . += 4;
        memmanage_status = .;
        . += 4;
        memmanage_address = .;
        . += 4;
        bus_fault_status = .;
        . += 4;
        bus_fault_address = .;
        . += 4;
        nmi_count = .;
        . += 4;
    } > SRAM

    .mpu_guard (NOLOAD) : ALIGN(32)
    {
        mpu_guard = .;
        . += 32;
    } > SRAM

    .data :
    {
        . = ALIGN(4);
        *(.data .data.*)
        . = ALIGN(4);
    } > SRAM AT> FLASH

    .bss (NOLOAD) : ALIGN(8)
    {
        *(.bss .bss.* COMMON)
        . = ALIGN(4);
    } > SRAM

    /DISCARD/ :
    {
        *(.ARM.exidx* .ARM.extab* .comment .note*)
    }
}

ASSERT(__Vectors == ORIGIN(FLASH) && __Vectors_Size == 960, "Expected the CMSIS ARMCM7 vector table.")
ASSERT(__StackTop - ADDR(.bss) - SIZEOF(.bss) >= 768, "Reserve room for the handler stack.")
