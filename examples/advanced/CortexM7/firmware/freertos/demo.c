#include "FreeRTOS.h"
#include "task.h"

extern volatile uint32_t counter_a;
extern volatile uint32_t counter_b;
extern volatile uint32_t demo_status;

static StaticTask_t task_a;
static StaticTask_t task_b;
static StaticTask_t idle_task;
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

static void count_a(void *parameter)
{
    (void)parameter;
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

void demo_main(void)
{
    counter_a = 0;
    counter_b = 0;
    demo_status = 0;
    if (xTaskCreateStatic(count_a, "count A", configMINIMAL_STACK_SIZE,
                          NULL, 1, stack_a, &task_a) == NULL ||
        xTaskCreateStatic(count_b, "count B", configMINIMAL_STACK_SIZE,
                          NULL, 1, stack_b, &task_b) == NULL) {
        demo_status = 1;
        return;
    }
    vTaskStartScheduler();
    demo_status = 2;
}
