/* Actual ST HAL polling and IRQ transfers against OOC's explicit test peers. */
#include "stm32h7xx_hal.h"

volatile uint32_t boot_signature;
volatile uint32_t data_value;
volatile uint32_t bss_value;
volatile uint32_t uwTick;
volatile uint32_t serial_stage;
volatile uint32_t serial_irq_completions;
static SPI_HandleTypeDef spi;
static I2C_HandleTypeDef i2c1, i2c2;
static volatile uint32_t completed;
static uint8_t spi_tx[8] = {0x12, 0x34, 0x56, 0x78, 0x9A, 0xBC, 0xDE, 0xF0};
static uint8_t spi_rx[8];
static uint8_t i2c_tx[4] = {0x31, 0x72, 0xB3, 0xF4};
static uint8_t i2c_rx[4];
static uint8_t irq_tx[3] = {0x30, 0x99, 0x55};

typedef void (*initializer)(void);
extern initializer __preinit_array_start[], __preinit_array_end[];
extern initializer __init_array_start[], __init_array_end[];

void __libc_init_array(void)
{
    for (initializer *init = __preinit_array_start; init != __preinit_array_end; ++init)
        (*init)();
    for (initializer *init = __init_array_start; init != __init_array_end; ++init)
        (*init)();
}

uint32_t HAL_GetTick(void) { return uwTick; }
void SysTick_Handler(void) { ++uwTick; }
void SPI1_IRQHandler(void) { HAL_SPI_IRQHandler(&spi); }
void I2C1_EV_IRQHandler(void) { HAL_I2C_EV_IRQHandler(&i2c1); }
void I2C1_ER_IRQHandler(void) { HAL_I2C_ER_IRQHandler(&i2c1); }
void I2C2_EV_IRQHandler(void) { HAL_I2C_EV_IRQHandler(&i2c2); }
void I2C2_ER_IRQHandler(void) { HAL_I2C_ER_IRQHandler(&i2c2); }

void HAL_SPI_TxRxCpltCallback(SPI_HandleTypeDef *handle)
{
    (void)handle;
    ++serial_irq_completions;
    completed = 1;
}
void HAL_SPI_ErrorCallback(SPI_HandleTypeDef *handle)
{
    (void)handle;
    completed = 0x80000000;
}
void HAL_I2C_MasterTxCpltCallback(I2C_HandleTypeDef *handle)
{
    (void)handle;
    ++serial_irq_completions;
    completed = 1;
}
void HAL_I2C_MasterRxCpltCallback(I2C_HandleTypeDef *handle)
{
    (void)handle;
    ++serial_irq_completions;
    completed = 1;
}
void HAL_I2C_ErrorCallback(I2C_HandleTypeDef *handle)
{
    (void)handle;
    completed = 0x80000000;
}

static void fail(uint32_t reason)
{
    boot_signature = 0xBAD00000 | (serial_stage << 8) | reason;
    for (;;) __NOP();
}
static void check(HAL_StatusTypeDef result)
{
    if (result != HAL_OK) fail((uint32_t)result + 1);
}
static void await_irq(void)
{
    uint32_t start = HAL_GetTick();
    while (!completed && HAL_GetTick() - start < 100) {}
    if (completed != 1) fail(8);
    completed = 0;
}
static void compare(const uint8_t *a, const uint8_t *b, unsigned count)
{
    for (unsigned i = 0; i < count; ++i)
        if (a[i] != b[i]) fail(9);
}
static void init_i2c(I2C_HandleTypeDef *handle, I2C_TypeDef *instance)
{
    handle->Instance = instance;
    handle->Init.Timing = 0x00C0216C;
    handle->Init.AddressingMode = I2C_ADDRESSINGMODE_7BIT;
    handle->Init.DualAddressMode = I2C_DUALADDRESS_DISABLE;
    handle->Init.OwnAddress2Masks = I2C_OA2_NOMASK;
    handle->Init.GeneralCallMode = I2C_GENERALCALL_DISABLE;
    handle->Init.NoStretchMode = I2C_NOSTRETCH_DISABLE;
    check(HAL_I2C_Init(handle));
}

int main(void)
{
    // Keep the normal ST reset/SystemInit code. A short SysTick makes HAL
    // timeout handling observable during interactive simulation.
    NVIC_SetPriority(SysTick_IRQn, 15);
    SysTick->LOAD = 1023;
    SysTick->VAL = 0;
    SysTick->CTRL = 7;
    RCC->APB2ENR |= RCC_APB2ENR_SPI1EN;
    RCC->APB1LENR |= RCC_APB1LENR_I2C1EN | RCC_APB1LENR_I2C2EN;

    serial_stage = 1;
    spi.Instance = SPI1;
    spi.Init.Mode = SPI_MODE_MASTER;
    spi.Init.Direction = SPI_DIRECTION_2LINES;
    spi.Init.DataSize = SPI_DATASIZE_8BIT;
    spi.Init.NSS = SPI_NSS_SOFT;
    spi.Init.BaudRatePrescaler = SPI_BAUDRATEPRESCALER_8;
    spi.Init.FirstBit = SPI_FIRSTBIT_MSB;
    spi.Init.TIMode = SPI_TIMODE_DISABLE;
    spi.Init.CRCCalculation = SPI_CRCCALCULATION_DISABLE;
    spi.Init.CRCPolynomial = 7;
    spi.Init.FifoThreshold = SPI_FIFO_THRESHOLD_01DATA;
    check(HAL_SPI_Init(&spi));
    check(HAL_SPI_TransmitReceive(&spi, spi_tx, spi_rx, 8, 100));
    compare(spi_tx, spi_rx, 8);
    data_value = 0;
    for (unsigned i = 0; i < 8; ++i) data_value += spi_rx[i];

    serial_stage = 2;
    NVIC_EnableIRQ(SPI1_IRQn);
    check(HAL_SPI_TransmitReceive_IT(&spi, spi_tx, spi_rx, 8));
    await_irq();
    compare(spi_tx, spi_rx, 8);

    serial_stage = 3;
    init_i2c(&i2c1, I2C1);
    init_i2c(&i2c2, I2C2);
    check(HAL_I2C_Mem_Write(&i2c1, 0x50 << 1, 0x20, I2C_MEMADD_SIZE_8BIT, i2c_tx, 4, 100));
    check(HAL_I2C_Mem_Read(&i2c1, 0x50 << 1, 0x20, I2C_MEMADD_SIZE_8BIT, i2c_rx, 4, 100));
    compare(i2c_tx, i2c_rx, 4);

    serial_stage = 4;
    if (HAL_I2C_Master_Transmit(&i2c1, 0x51 << 1, i2c_tx, 2, 100) != HAL_ERROR ||
        !(HAL_I2C_GetError(&i2c1) & HAL_I2C_ERROR_AF)) fail(10);
    check(HAL_I2C_Mem_Read(&i2c1, 0x50 << 1, 0x20, I2C_MEMADD_SIZE_8BIT, i2c_rx, 4, 100));
    compare(i2c_tx, i2c_rx, 4);

    serial_stage = 5;
    NVIC_EnableIRQ(I2C1_EV_IRQn);
    NVIC_EnableIRQ(I2C1_ER_IRQn);
    check(HAL_I2C_Master_Transmit_IT(&i2c1, 0x50 << 1, irq_tx, 3));
    await_irq();
    check(HAL_I2C_Master_Transmit(&i2c1, 0x50 << 1, irq_tx, 1, 100));
    check(HAL_I2C_Master_Receive_IT(&i2c1, 0x50 << 1, i2c_rx, 2));
    await_irq();
    compare(&irq_tx[1], i2c_rx, 2);
    bss_value = (uint32_t)i2c_rx[0] | ((uint32_t)i2c_rx[1] << 8);

    serial_stage = 6;
    NVIC_EnableIRQ(I2C2_EV_IRQn);
    NVIC_EnableIRQ(I2C2_ER_IRQn);
    check(HAL_I2C_Master_Receive_IT(&i2c2, 0x50 << 1, i2c_rx, 1));
    await_irq();
    if (i2c_rx[0] != 0x42 || serial_irq_completions != 4) fail(11);
    serial_stage = 7;
    boot_signature = 0x5352494C; // SRIL: all polling, IRQ, and NACK checks passed.
    for (;;) __NOP();
}

