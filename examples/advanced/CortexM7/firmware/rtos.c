#include <stdint.h>

__attribute__((noinline))
static uint32_t advance_counter(uint32_t count, uint32_t step)
{
    volatile uint32_t next = count + step;
    return next;
}

__attribute__((noreturn))
void rtos_task(volatile uint32_t *counter, uint32_t step)
{
    for (;;) {
        *counter = advance_counter(*counter, step);
    }
}
