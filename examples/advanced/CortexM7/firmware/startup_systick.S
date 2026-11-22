.syntax unified
.cpu cortex-m7
.thumb

.section .vectors, "a", %progbits
.word __stack_top
.word Reset_Handler
// SysTick is exception 15. The other exception entries are unused here.
.rept 13
.word 0
.endr
.word SysTick_Handler

.section .text.Reset_Handler, "ax", %progbits
.global Reset_Handler
.type Reset_Handler, %function
.thumb_func
Reset_Handler:
    bl systick_main
.Lidle:
    b.n .Lidle
.size Reset_Handler, . - Reset_Handler
