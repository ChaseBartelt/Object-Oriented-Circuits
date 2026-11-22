#include <stdint.h>

volatile uint32_t data_value = 0x12345678;
volatile uint32_t bss_value;
volatile uint32_t boot_signature;

typedef void (*initializer)(void);

extern initializer __preinit_array_start[], __preinit_array_end[];
extern initializer __init_array_start[], __init_array_end[];

/* Freestanding C runtime hook called by ST's Reset_Handler. */
void __libc_init_array(void)
{
    for (initializer *init = __preinit_array_start; init != __preinit_array_end; ++init)
        (*init)();
    for (initializer *init = __init_array_start; init != __init_array_end; ++init)
        (*init)();
}

int main(void)
{
    if (data_value == 0x12345678 && bss_value == 0) {
        bss_value = 0xA5;
        boot_signature = 0x44414953;
    } else {
        boot_signature = 0xBAD00000;
    }

    for (;;)
        __asm__ volatile ("nop");
}
