#include "FreeRTOS.h"
#include "queue.h"
#include "task.h"

#define QUEUE_LENGTH 2
#define DELAY_TICKS 2
#define NVIC_ISER0 (*(volatile uint32_t *)0xE000E100)
#define NVIC_IPR0 (*(volatile uint8_t *)0xE000E400)

extern volatile uint32_t counter_a;
extern volatile uint32_t counter_b;
extern volatile uint32_t demo_status;

// The linker places these at RAM3 and RAM4 after the common output words.
volatile struct {
    uint32_t receiver_phase;
    uint32_t error;
} blocking_state __attribute__((section(".results.extra")));

static StaticTask_t delay_control;
static StaticTask_t receive_control;
static StaticTask_t idle_control;
static StackType_t delay_stack[configMINIMAL_STACK_SIZE] __attribute__((aligned(8)));
static StackType_t receive_stack[configMINIMAL_STACK_SIZE] __attribute__((aligned(8)));
static StackType_t idle_stack[configMINIMAL_STACK_SIZE] __attribute__((aligned(8)));
static StaticQueue_t queue_control;
static uint8_t queue_storage[QUEUE_LENGTH * sizeof(uint32_t)];
static QueueHandle_t mailbox;

void vApplicationGetIdleTaskMemory(StaticTask_t **task, StackType_t **stack,
                                  configSTACK_DEPTH_TYPE *words)
{
    *task = &idle_control;
    *stack = idle_stack;
    *words = configMINIMAL_STACK_SIZE;
}

static void wait_for_tick(void *parameter)
{
    (void) parameter;
    for (;;) {
        vTaskDelay(DELAY_TICKS);
        ++counter_a;
    }
}

static void receive_payload(void *parameter)
{
    (void) parameter;
    // The Cortex-M7 model exposes IRQ0 through the NVIC's first enable and priority registers.
    NVIC_IPR0 = 0x80;
    NVIC_ISER0 = 1;

    uint32_t phase = 0;
    for (;;) {
        uint32_t payload;
        blocking_state.receiver_phase = phase + 1;
        if (xQueueReceive(mailbox, &payload, portMAX_DELAY) == pdPASS) {
            counter_b = payload;
            phase += 2;
            blocking_state.receiver_phase = phase;
        }
    }
}

void IRQ0_Handler(void)
{
    const uint32_t payload = 1000 + ++demo_status;
    BaseType_t should_switch = pdFALSE;
    if (xQueueSendFromISR(mailbox, &payload, &should_switch) != pdPASS) {
        blocking_state.error = 3;
    }
    portYIELD_FROM_ISR(should_switch);
}

void demo_main(void)
{
    counter_a = 0;
    counter_b = 0;
    demo_status = 0;
    blocking_state.receiver_phase = 0;
    blocking_state.error = 0;
    mailbox = xQueueCreateStatic(QUEUE_LENGTH, sizeof(uint32_t), queue_storage, &queue_control);
    if (mailbox == NULL ||
        xTaskCreateStatic(wait_for_tick, "delay", configMINIMAL_STACK_SIZE,
                          NULL, 1, delay_stack, &delay_control) == NULL ||
        xTaskCreateStatic(receive_payload, "receive", configMINIMAL_STACK_SIZE,
                          NULL, 2, receive_stack, &receive_control) == NULL) {
        blocking_state.error = 1;
        return;
    }
    vTaskStartScheduler();
    blocking_state.error = 2;
}
