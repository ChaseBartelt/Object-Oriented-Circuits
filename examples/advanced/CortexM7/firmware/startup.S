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
    bl firmware_main
.Lidle:
    b.n .Lidle
.size Reset_Handler, . - Reset_Handler
