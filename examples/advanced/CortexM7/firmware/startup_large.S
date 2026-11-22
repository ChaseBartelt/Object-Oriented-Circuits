.syntax unified
.cpu cortex-m7
.thumb

.section .vectors, "a", %progbits
.word __stack_top
.word Reset_Handler

.section .text.Reset_Handler, "ax", %progbits
.global Reset_Handler
.type Reset_Handler, %function
.thumb_func
Reset_Handler:
    // Copy the linked initial values from flash into the data region in SRAM.
    ldr r0, =__data_load
    ldr r1, =__data_start
    ldr r2, =__data_end
.Lcopy_data:
    cmp r1, r2
    beq .Lstart_bss
    ldr r3, [r0]
    str r3, [r1]
    adds r0, #4
    adds r1, #4
    b.n .Lcopy_data

.Lstart_bss:
    ldr r1, =__bss_start
    ldr r2, =__bss_end
    movs r3, #0
.Lzero_bss:
    cmp r1, r2
    beq .Lrun
    str r3, [r1]
    adds r1, #4
    b.n .Lzero_bss

.Lrun:
    .ifdef OOC_HARDFLOAT
    // Enable CP10/CP11 before optimized C can spill callee-saved FP registers.
    ldr r0, =0xe000ed88
    ldr r1, =0x00f00000
    str r1, [r0]
    dsb sy
    isb sy
    .endif
    bl large_main
.Lidle:
    b.n .Lidle
.size Reset_Handler, . - Reset_Handler
.ltorg
