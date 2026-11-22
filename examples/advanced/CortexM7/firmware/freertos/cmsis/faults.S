.syntax unified
.cpu cortex-m7
.thumb

// Use the stack selected by EXC_RETURN and preserve LR for exception return.
.macro fault_handler handler, recover
.section .text.\handler, "ax", %progbits
.global \handler
.type \handler, %function
.thumb_func
\handler:
    mov r0, lr
    movs r1, #4
    tst r0, r1
    bne 1f
    mrs r0, msp
    b \recover
1:
    mrs r0, psp
    b \recover
.size \handler, . - \handler
.endm

fault_handler UsageFault_Handler, recover_usage_fault
fault_handler MemManage_Handler, recover_memmanage
fault_handler BusFault_Handler, recover_bus_fault

.section .text.trigger_usage_fault, "ax", %progbits
.global trigger_usage_fault
.type trigger_usage_fault, %function
.thumb_func
trigger_usage_fault:
    udf #42
    bx lr
.size trigger_usage_fault, . - trigger_usage_fault

.macro fault_read trigger
.section .text.\trigger, "ax", %progbits
.global \trigger
.type \trigger, %function
.thumb_func
\trigger:
    ldr r0, [r0]
    bx lr
.size \trigger, . - \trigger
.endm

fault_read trigger_memmanage
fault_read trigger_bus_fault
