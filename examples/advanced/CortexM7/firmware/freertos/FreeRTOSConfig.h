#ifndef FREERTOS_CONFIG_H
#define FREERTOS_CONFIG_H

#define configCPU_CLOCK_HZ                      10000
#define configTICK_RATE_HZ                       25
#define configTICK_TYPE_WIDTH_IN_BITS             TICK_TYPE_WIDTH_32_BITS
#define configUSE_PREEMPTION                     1
#define configUSE_TIME_SLICING                   1
#define configUSE_PORT_OPTIMISED_TASK_SELECTION   0
#define configMAX_PRIORITIES                     3
#define configMINIMAL_STACK_SIZE                 64
#define configMAX_TASK_NAME_LEN                  8

#define configSUPPORT_STATIC_ALLOCATION          1
#define configSUPPORT_DYNAMIC_ALLOCATION         0
#define configUSE_IDLE_HOOK                      0
#define configUSE_TICK_HOOK                      0
#define configUSE_TIMERS                         0
#define configUSE_MUTEXES                        0
#define configUSE_TASK_NOTIFICATIONS             0

#define configKERNEL_INTERRUPT_PRIORITY          240
#define configMAX_SYSCALL_INTERRUPT_PRIORITY     80

#endif
