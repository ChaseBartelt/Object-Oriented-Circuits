.syntax unified
.cpu cortex-m3
.thumb

.section .vectors.irq0, "a", %progbits
.word IRQ0_Handler
