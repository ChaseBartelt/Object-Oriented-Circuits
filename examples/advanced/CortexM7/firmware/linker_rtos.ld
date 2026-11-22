ENTRY(Reset_Handler)

MEMORY
{
    FLASH (rx) : ORIGIN = 0x00000000, LENGTH = 4K
    SRAM (rw)  : ORIGIN = 0x20000000, LENGTH = 4K
}

__stack_top = ORIGIN(SRAM) + LENGTH(SRAM);
__systick_ctrl = 0xE000E010;
__icsr = 0xE000ED04;
__shpr3 = 0xE000ED20;
__shpr3_config = 0x80FF0000;
__xpsr_thumb = 0x01000000;
__thread_psp_return = 0xFFFFFFFD;
__pendsv_set = 0x10000000;

SECTIONS
{
    .vectors ORIGIN(FLASH) :
    {
        KEEP(*(.vectors))
    } > FLASH

    .text :
    {
        *(.text.rtos)
        *(.text.rtos_task)
        *(.text .text.* .rodata .rodata.*)
        . = ALIGN(4);
    } > FLASH

    .scheduler_state ORIGIN(SRAM) (NOLOAD) :
    {
        rtos_counter_a = .;
        . += 4;
        rtos_counter_b = .;
        . += 4;
        rtos_switches = .;
        . += 4;
        rtos_active = .;
        . += 4;
        rtos_contexts = .;
        . += 8;
    } > SRAM

    .task_stacks ORIGIN(SRAM) + 1K (NOLOAD) :
    {
        . += 1K;
        __task_a_top = .;
        . += 1K;
        __task_b_top = .;
    } > SRAM

    /DISCARD/ :
    {
        *(.ARM.exidx* .ARM.extab* .comment .note*)
    }
}

ASSERT(SIZEOF(.vectors) == 64, "Expected the 16 core vectors.")
