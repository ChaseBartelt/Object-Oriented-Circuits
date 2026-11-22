#ifndef OOC_HAL_SERIAL_CONF_H
#define OOC_HAL_SERIAL_CONF_H

#define HAL_MODULE_ENABLED
#define HAL_DMA_MODULE_ENABLED
#define HAL_SPI_MODULE_ENABLED
#define HAL_I2C_MODULE_ENABLED
#define HSE_VALUE 16000000U
#define HSI_VALUE 64000000U
#define CSI_VALUE 4000000U
#define HSI48_VALUE 48000000U
#define LSI_VALUE 32000U
#define LSE_VALUE 32768U
#define EXTERNAL_CLOCK_VALUE 12288000U
#define VDD_VALUE 3300U
#define TICK_INT_PRIORITY 15U
#define USE_RTOS 0U
#define USE_HAL_SPI_REGISTER_CALLBACKS 0U
#define USE_HAL_I2C_REGISTER_CALLBACKS 0U
#define assert_param(expression) ((void)0U)

#include "stm32h7xx_hal_dma.h"
#include "stm32h7xx_hal_spi.h"
#include "stm32h7xx_hal_i2c.h"
#endif
