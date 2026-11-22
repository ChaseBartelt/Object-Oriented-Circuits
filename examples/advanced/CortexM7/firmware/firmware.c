#include <stdint.h>

extern uint32_t nested_add(uint32_t value);
extern volatile uint32_t firmware_result;

void firmware_main(void)
{
    firmware_result = nested_add(4);
}
