.syntax unified
.cpu cortex-m3
.thumb

.section .vectors, "a", %progbits
.word __stack_top
.word Reset_Handler
.rept 9
.word 0
.endr
.word vPortSVCHandler
.word 0
.word 0
.word xPortPendSVHandler
.word xPortSysTickHandler

.section .text.Reset_Handler, "ax", %progbits
.global Reset_Handler
.type Reset_Handler, %function
.thumb_func
Reset_Handler:
    ldr r0, =__data_load
    ldr r1, =__data_start
    ldr r2, =__data_end
.Lcopy:
    cmp r1, r2
    beq .Lload_bss
    ldr r3, [r0]
    str r3, [r1]
    adds r0, #4
    adds r1, #4
    b.n .Lcopy

.Lload_bss:
    ldr r1, =__bss_start
    ldr r2, =__bss_end
    movs r3, #0
.Lzero:
    cmp r1, r2
    beq .Lrun
    str r3, [r1]
    adds r1, #4
    b.n .Lzero

.Lrun:
    bl demo_main
.Lhalt:
    b.n .Lhalt
.size Reset_Handler, . - Reset_Handler
.ltorg
