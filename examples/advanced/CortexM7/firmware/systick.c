#include <stdint.h>

extern volatile uint32_t systick_ctrl;
extern volatile uint32_t systick_load;
extern volatile uint32_t systick_value;
extern volatile uint32_t systick_ticks;

void systick_main(void)
{
    systick_ticks = 0;
    systick_load = 31;
    systick_value = 0;
    systick_ctrl = 7; // Core clock, interrupt, and counter enable.
}

void SysTick_Handler(void)
{
    ++systick_ticks;
}
