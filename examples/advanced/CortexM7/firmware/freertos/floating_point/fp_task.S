.syntax unified
.cpu cortex-m7
.fpu fpv5-sp-d16
.thumb

.section .text.fp_task, "ax", %progbits
.global fp_task
.type fp_task, %function
.thumb_func
fp_task:
    // Select the preservation mode before this task first uses the FPU.
    ldr r1, =0xe000ef34
    .ifdef DEMO_LAZY_FP
    ldr r2, =0xc0000000
    .else
    ldr r2, =0x80000000
    .endif
    str r2, [r1]
    dsb
    isb

    // The task pattern is read only. Keep its expectations in core registers
    // and initialize each FP canary only once, before all preemptions.
    ldmia r0!, {r1-r7}
    ldr r0, [r0]
    vmov s0, r3
    vmov s15, r4
    vmov s16, r5
    vmov s31, r6
    vmsr fpscr, r7
    vadd.f32 s1, s0, s16
    vmov r12, s1
    cmp r12, r0
    bne .Lfail
    str r0, [r2]

.Lverify:
    vmov r2, s0
    cmp r2, r3
    bne .Lfail
    vmov r2, s1
    cmp r2, r0
    bne .Lfail
    vmov r2, s15
    cmp r2, r4
    bne .Lfail
    vmov r2, s16
    cmp r2, r5
    bne .Lfail
    vmov r2, s31
    cmp r2, r6
    bne .Lfail
    vmrs r2, fpscr
    cmp r2, r7
    bne .Lfail
    ldr r2, [r1]
    adds r2, #1
    str r2, [r1]
    b.n .Lverify

.Lfail:
    ldr r1, =demo_status
    movs r2, #3
    str r2, [r1]
.Lhalt:
    b.n .Lhalt
.size fp_task, . - fp_task
.ltorg
