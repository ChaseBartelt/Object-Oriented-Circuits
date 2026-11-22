#include "ARMCM7_SP.h"
#include "FreeRTOS.h"
#include "task.h"

extern volatile uint32_t counter_a, counter_b, demo_status;
extern volatile uint32_t irq0_count, irq1_count, usage_fault_status;
extern volatile uint32_t memmanage_status, memmanage_address, bus_fault_status, bus_fault_address;
extern volatile uint32_t nmi_count;
extern const uint32_t mpu_guard[];
extern void trigger_usage_fault(void);
extern void trigger_memmanage(uintptr_t address);
extern void trigger_bus_fault(uintptr_t address);

#define UNMAPPED_ADDRESS 0x40000000u

static volatile uint32_t startup_cookie = 0x434D5337;
static volatile uint32_t zero_cookie;
static StaticTask_t task_a, task_b, idle_task;
static StackType_t stack_a[configMINIMAL_STACK_SIZE] __attribute__((aligned(8)));
static StackType_t stack_b[configMINIMAL_STACK_SIZE] __attribute__((aligned(8)));
static StackType_t idle_stack[configMINIMAL_STACK_SIZE] __attribute__((aligned(8)));

void vApplicationGetIdleTaskMemory(StaticTask_t **task, StackType_t **stack,
                                  configSTACK_DEPTH_TYPE *words)
{
    *task = &idle_task;
    *stack = idle_stack;
    *words = configMINIMAL_STACK_SIZE;
}

void Interrupt0_Handler(void)
{
    ++irq0_count;
}

void Interrupt1_Handler(void)
{
    ++irq1_count;
}

void NMI_Handler(void)
{
    ++nmi_count;
}

void recover_usage_fault(uint32_t *frame)
{
    uint32_t status = SCB->CFSR;
    if (status != SCB_CFSR_UNDEFINSTR_Msk ||
        frame[6] != ((uintptr_t)trigger_usage_fault & ~1u)) {
        demo_status = 0xE002;
        for (;;) {}
    }
    usage_fault_status = status;
    // This probe deliberately faults on one 16-bit UDF. Resume at its BX LR.
    frame[6] += 2;
    SCB->CFSR = SCB_CFSR_UNDEFINSTR_Msk;
}

void recover_memmanage(uint32_t *frame)
{
    const uint32_t expected = SCB_CFSR_DACCVIOL_Msk | SCB_CFSR_MMARVALID_Msk;
    uint32_t status = SCB->CFSR;
    uint32_t address = SCB->MMFAR;
    if (status != expected || address != (uintptr_t)mpu_guard ||
        frame[6] != ((uintptr_t)trigger_memmanage & ~1u)) {
        demo_status = 0xE005;
        for (;;) {}
    }
    memmanage_status = status;
    memmanage_address = address;
    frame[6] += 2;  // Resume at BX LR after the probe's 16-bit LDR.
    SCB->CFSR = expected;
}

void recover_bus_fault(uint32_t *frame)
{
    const uint32_t expected = SCB_CFSR_PRECISERR_Msk | SCB_CFSR_BFARVALID_Msk;
    uint32_t status = SCB->CFSR;
    uint32_t address = SCB->BFAR;
    if (status != expected || address != UNMAPPED_ADDRESS ||
        frame[6] != ((uintptr_t)trigger_bus_fault & ~1u)) {
        demo_status = 0xE006;
        for (;;) {}
    }
    bus_fault_status = status;
    bus_fault_address = address;
    frame[6] += 2;  // Resume at BX LR after the probe's 16-bit LDR.
    SCB->CFSR = expected;
}

static void count_a(void *parameter)
{
    (void)parameter;
    ++counter_a;
    while (counter_b == 0) {}
    trigger_usage_fault();
    trigger_memmanage((uintptr_t)mpu_guard);
    trigger_bus_fault(UNMAPPED_ADDRESS);

    // IRQ1 has the better preemption group even with its worse subpriority.
    const uint32_t grouping = 5;
    NVIC_SetPriorityGrouping(grouping);
    NVIC_SetPriority(Interrupt0_IRQn, NVIC_EncodePriority(grouping, 3, 0));
    NVIC_SetPriority(Interrupt1_IRQn, NVIC_EncodePriority(grouping, 2, 1));
    if (NVIC_GetPriorityGrouping() != grouping) {
        demo_status = 0xE007;
        for (;;) {}
    }
    demo_status = 2;

    while (irq0_count == 0 || irq1_count == 0) {
        ++counter_a;
    }

    // Put both IRQs in the same preemption group. IRQ1 now has
    // the higher subpriority for selecting between pending requests.
    NVIC_SetPriority(Interrupt0_IRQn, NVIC_EncodePriority(grouping, 2, 1));
    NVIC_SetPriority(Interrupt1_IRQn, NVIC_EncodePriority(grouping, 2, 0));
    demo_status = 3;
    while (irq0_count < 3 || irq1_count < 3) {
        ++counter_a;
    }

    // Restore the FreeRTOS default after the group and subpriority checks.
    NVIC_SetPriorityGrouping(0);
    NVIC_SetPriority(Interrupt0_IRQn, 7);
    NVIC_SetPriority(Interrupt1_IRQn, 6);
    demo_status = 4;
    for (;;) {
        ++counter_a;
    }
}

static void count_b(void *parameter)
{
    (void)parameter;
    for (;;) {
        counter_b += 3;
    }
}

int main(void)
{
    if (startup_cookie != 0x434D5337 || zero_cookie != 0 ||
        *(volatile uint32_t *)&SystemCoreClock != 25000000u) {
        return 0xE001;
    }

    NVIC_SetPriority(MemoryManagement_IRQn, 2);
    NVIC_SetPriority(BusFault_IRQn, 2);
    NVIC_SetPriority(UsageFault_IRQn, 2);
    SCB->SHCSR |= SCB_SHCSR_MEMFAULTENA_Msk | SCB_SHCSR_BUSFAULTENA_Msk | SCB_SHCSR_USGFAULTENA_Msk;
    // Deny access to the guard even from privileged Thread mode. Other
    // privileged addresses keep the background map for code, stacks and MMIO.
    ARM_MPU_SetRegion(ARM_MPU_RBAR(0, (uintptr_t)mpu_guard),
                      ARM_MPU_RASR(1, ARM_MPU_AP_NONE, 0, 0, 0, 0, 0, ARM_MPU_REGION_SIZE_32B));
    ARM_MPU_Enable(MPU_CTRL_PRIVDEFENA_Msk);
    NVIC_SetPriority(Interrupt0_IRQn, 7);
    NVIC_SetPriority(Interrupt1_IRQn, 6);
    NVIC_EnableIRQ(Interrupt0_IRQn);
    NVIC_EnableIRQ(Interrupt1_IRQn);
    demo_status = 1;
    if (xTaskCreateStatic(count_a, "count A", configMINIMAL_STACK_SIZE,
                          NULL, 1, stack_a, &task_a) == NULL ||
        xTaskCreateStatic(count_b, "count B", configMINIMAL_STACK_SIZE,
                          NULL, 1, stack_b, &task_b) == NULL) {
        return 0xE003;
    }
    vTaskStartScheduler();
    return 0xE004;
}

// CMSIS has already initialized .data and .bss before this freestanding handoff.
__attribute__((noreturn)) void _start(void)
{
    demo_status = (uint32_t)main();
    for (;;) {}
}
