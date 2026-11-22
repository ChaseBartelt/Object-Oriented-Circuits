.syntax unified
.cpu cortex-m7
.thumb

.section .vectors, "a", %progbits
.word __stack_top
.word Reset_Handler
.rept 9
.word 0
.endr
.word SVC_Handler
.word 0
.word 0
.word PendSV_Handler
.word SysTick_Handler

.section .text.rtos, "ax", %progbits
.global Reset_Handler
.type Reset_Handler, %function
.thumb_func
Reset_Handler:
    // Initialize registers and stack frames for the two tasks.
    ldr r0, =__task_a_top
    ldr r1, =rtos_counter_a
    movs r2, #1
    movs r3, #0x40
    bl make_initial_frame
    ldr r1, =rtos_contexts
    str r0, [r1]

    ldr r0, =__task_b_top
    ldr r1, =rtos_counter_b
    movs r2, #3
    movs r3, #0x80
    bl make_initial_frame
    ldr r1, =rtos_contexts
    str r0, [r1, #4]

    ldr r0, =rtos_counter_a
    movs r1, #0
    str r1, [r0]
    str r1, [r0, #4]
    str r1, [r0, #8]
    str r1, [r0, #12]

    // PendSV has the lowest priority. SysTick can request it from Handler mode.
    ldr r0, =__shpr3
    ldr r1, =__shpr3_config
    str r1, [r0]
    ldr r0, =__systick_ctrl
    movs r1, #191
    str r1, [r0, #4]
    movs r1, #0
    str r1, [r0, #8]
    movs r1, #7
    str r1, [r0]
    svc #0
.Lboot_stop:
    b.n .Lboot_stop
.size Reset_Handler, . - Reset_Handler

.type make_initial_frame, %function
.thumb_func
make_initial_frame:
    // A saved PSP points to r4-r11. The hardware frame starts 32 bytes later.
    subs r0, #64
    str r3, [r0]
    adds r3, #1
    str r3, [r0, #4]
    adds r3, #1
    str r3, [r0, #8]
    adds r3, #1
    str r3, [r0, #12]
    adds r3, #1
    str r3, [r0, #16]
    adds r3, #1
    str r3, [r0, #20]
    adds r3, #1
    str r3, [r0, #24]
    adds r3, #1
    str r3, [r0, #28]
    str r1, [r0, #32]
    str r2, [r0, #36]
    movs r3, #0
    str r3, [r0, #40]
    str r3, [r0, #44]
    str r3, [r0, #48]
    str r3, [r0, #52]
    ldr r1, =rtos_task
    subs r1, #1
    str r1, [r0, #56]
    ldr r1, =__xpsr_thumb
    str r1, [r0, #60]
    bx lr
.size make_initial_frame, . - make_initial_frame

.global SVC_Handler
.type SVC_Handler, %function
.thumb_func
SVC_Handler:
    // Reset entered SVC on an eight-byte aligned MSP. Discard that frame.
    add sp, #32
    ldr r0, =rtos_contexts
    ldr r0, [r0]
    b.n restore_context
.size SVC_Handler, . - SVC_Handler

.global PendSV_Handler
.type PendSV_Handler, %function
.thumb_func
PendSV_Handler:
    // Hardware stacked r0-r3, r12, LR, PC and xPSR; save the other task registers.
    mrs r0, psp
    subs r0, #32
    str r4, [r0]
    str r5, [r0, #4]
    str r6, [r0, #8]
    str r7, [r0, #12]
    mov r2, r8
    str r2, [r0, #16]
    mov r2, r9
    str r2, [r0, #20]
    mov r2, r10
    str r2, [r0, #24]
    mov r2, r11
    str r2, [r0, #28]

    ldr r1, =rtos_switches
    ldr r2, [r1]
    adds r2, #1
    str r2, [r1]
    ldr r2, [r1, #4]
    cmp r2, #0
    bne .Lselect_task_a
    str r0, [r1, #8]
    movs r2, #1
    str r2, [r1, #4]
    ldr r0, [r1, #12]
    b.n restore_context
.Lselect_task_a:
    str r0, [r1, #12]
    movs r2, #0
    str r2, [r1, #4]
    ldr r0, [r1, #8]
.size PendSV_Handler, . - PendSV_Handler

.type restore_context, %function
.thumb_func
restore_context:
    ldr r4, [r0]
    ldr r5, [r0, #4]
    ldr r6, [r0, #8]
    ldr r7, [r0, #12]
    ldr r1, [r0, #16]
    mov r8, r1
    ldr r1, [r0, #20]
    mov r9, r1
    ldr r1, [r0, #24]
    mov r10, r1
    ldr r1, [r0, #28]
    mov r11, r1
    adds r0, #32
    msr psp, r0
    ldr r1, =__thread_psp_return
    mov lr, r1
    bx lr
.size restore_context, . - restore_context

.global SysTick_Handler
.type SysTick_Handler, %function
.thumb_func
SysTick_Handler:
    ldr r0, =__icsr
    ldr r1, =__pendsv_set
    str r1, [r0]
    bx lr
.size SysTick_Handler, . - SysTick_Handler
.ltorg
