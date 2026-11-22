#include "FreeRTOS.h"
#include "task.h"

extern volatile uint32_t counter_a;
extern volatile uint32_t counter_b;
extern volatile uint32_t demo_status;
extern void fp_task(void *parameter);

static StaticTask_t task_control[2];
static StaticTask_t idle_task;
static StackType_t task_stacks[2][configMINIMAL_STACK_SIZE] __attribute__((aligned(8)));
static StackType_t idle_stack[configMINIMAL_STACK_SIZE] __attribute__((aligned(8)));
static volatile uint32_t sums[2] __attribute__((section(".results.extra")));

// fp_task loads the argument block as eight words, starting with two pointers.
struct task_pattern {
    volatile uint32_t *counter;
    volatile uint32_t *sum;
    uint32_t s0, s15, s16, s31, fpscr, s1;
};

_Static_assert(sizeof(struct task_pattern) == 8 * sizeof(uint32_t), "FP task argument must fit eight words");

static const struct task_pattern patterns[2] = {
    // Task A uses 1 + 4 = 5, with 6 and 7 at the ends of the two saved register banks.
    {&counter_a, &sums[0], 0x3f800000, 0x40c00000, 0x40800000, 0x40e00000, 1, 0x40a00000},
    // Task B uses 2 + 8 = 10, with 12 and 14 at those same positions.
    {&counter_b, &sums[1], 0x40000000, 0x41400000, 0x41000000, 0x41600000, 2, 0x41200000},
};

void vApplicationGetIdleTaskMemory(StaticTask_t **task, StackType_t **stack,
                                  configSTACK_DEPTH_TYPE *words)
{
    *task = &idle_task;
    *stack = idle_stack;
    *words = configMINIMAL_STACK_SIZE;
}

void demo_main(void)
{
    counter_a = 0;
    counter_b = 0;
    demo_status = 0;
    sums[0] = 0;
    sums[1] = 0;
    if (xTaskCreateStatic(fp_task, "FP A", configMINIMAL_STACK_SIZE, (void *)&patterns[0],
                          1, task_stacks[0], &task_control[0]) == NULL ||
        xTaskCreateStatic(fp_task, "FP B", configMINIMAL_STACK_SIZE, (void *)&patterns[1],
                          1, task_stacks[1], &task_control[1]) == NULL) {
        demo_status = 1;
        return;
    }
    vTaskStartScheduler();
    demo_status = 2;
}
