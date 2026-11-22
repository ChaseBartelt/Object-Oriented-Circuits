#include "stm32h7xx_hal.h"

volatile uint32_t clock_hz;
volatile uint32_t clock_status;
volatile uint32_t led_transitions;

enum {
    CLOCK_READY = 1,
    HAL_INIT_FAILED,
    SUPPLY_SETUP_FAILED,
    OSCILLATOR_SETUP_FAILED,
    CLOCK_SWITCH_FAILED,
};

typedef void (*initializer)(void);
extern initializer __preinit_array_start[], __preinit_array_end[];
extern initializer __init_array_start[], __init_array_end[];

/* Freestanding C runtime hook called by the ST reset handler. */
void __libc_init_array(void)
{
    for (initializer *init = __preinit_array_start; init != __preinit_array_end; ++init)
        (*init)();
    for (initializer *init = __init_array_start; init != __init_array_end; ++init)
        (*init)();
}

void SysTick_Handler(void)
{
    HAL_IncTick();
}

void HAL_MspInit(void)
{
    __HAL_RCC_SYSCFG_CLK_ENABLE();
}

static void fail(uint32_t reason)
{
    clock_status = reason;
    for (;;)
        __NOP();
}

static void configure_clock(void)
{
    static RCC_OscInitTypeDef oscillator;
    static RCC_ClkInitTypeDef clocks;

    if (HAL_PWREx_ConfigSupply(PWR_LDO_SUPPLY) != HAL_OK)
        fail(SUPPLY_SETUP_FAILED);
    __HAL_PWR_VOLTAGESCALING_CONFIG(PWR_REGULATOR_VOLTAGE_SCALE1);
    while (!__HAL_PWR_GET_FLAG(PWR_FLAG_VOSRDY)) {}

    /* Match the Daisy Seed's 400 MHz core clock using its 16 MHz crystal. */
    oscillator.OscillatorType = RCC_OSCILLATORTYPE_HSE | RCC_OSCILLATORTYPE_HSI48;
    oscillator.HSEState = RCC_HSE_ON;
    oscillator.HSI48State = RCC_HSI48_ON;
    oscillator.PLL.PLLState = RCC_PLL_ON;
    oscillator.PLL.PLLSource = RCC_PLLSOURCE_HSE;
    oscillator.PLL.PLLM = 4;
    oscillator.PLL.PLLN = 200;
    oscillator.PLL.PLLP = 2;
    oscillator.PLL.PLLQ = 5;
    oscillator.PLL.PLLR = 2;
    oscillator.PLL.PLLRGE = RCC_PLL1VCIRANGE_2;
    oscillator.PLL.PLLVCOSEL = RCC_PLL1VCOWIDE;
    if (HAL_RCC_OscConfig(&oscillator) != HAL_OK)
        fail(OSCILLATOR_SETUP_FAILED);

    clocks.ClockType = RCC_CLOCKTYPE_HCLK | RCC_CLOCKTYPE_SYSCLK | RCC_CLOCKTYPE_PCLK1
                     | RCC_CLOCKTYPE_PCLK2 | RCC_CLOCKTYPE_D3PCLK1 | RCC_CLOCKTYPE_D1PCLK1;
    clocks.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;
    clocks.SYSCLKDivider = RCC_SYSCLK_DIV1;
    clocks.AHBCLKDivider = RCC_HCLK_DIV2;
    clocks.APB1CLKDivider = RCC_APB1_DIV2;
    clocks.APB2CLKDivider = RCC_APB2_DIV2;
    clocks.APB3CLKDivider = RCC_APB3_DIV2;
    clocks.APB4CLKDivider = RCC_APB4_DIV2;
    if (HAL_RCC_ClockConfig(&clocks, FLASH_LATENCY_2) != HAL_OK)
        fail(CLOCK_SWITCH_FAILED);
    clock_hz = SystemCoreClock;
    clock_status = CLOCK_READY;
}

int main(void)
{
    if (HAL_Init() != HAL_OK)
        fail(HAL_INIT_FAILED);
    configure_clock();

    GPIO_InitTypeDef led;
    led.Pin = GPIO_PIN_7;
    led.Mode = GPIO_MODE_OUTPUT_PP;
    led.Pull = GPIO_NOPULL;
    led.Speed = GPIO_SPEED_FREQ_LOW;
    led.Alternate = 0;
    __HAL_RCC_GPIOC_CLK_ENABLE();
    HAL_GPIO_WritePin(GPIOC, GPIO_PIN_7, GPIO_PIN_RESET);
    HAL_GPIO_Init(GPIOC, &led);

#ifdef OOC_SIMULATION
    /* HAL selected the normal reload first. Shorten it for interactive steps. */
    SysTick->LOAD = 127;
    SysTick->VAL = 0;
    const uint32_t blink_delay = 4;
#else
    const uint32_t blink_delay = 500;
#endif

    for (;;) {
        HAL_Delay(blink_delay);
        HAL_GPIO_TogglePin(GPIOC, GPIO_PIN_7);
        ++led_transitions;
    }
}
