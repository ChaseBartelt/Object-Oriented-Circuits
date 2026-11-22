#include <stdint.h>

__attribute__((noinline))
uint32_t leaf_add(uint32_t value)
{
    return value + 7;
}

__attribute__((noinline))
uint32_t nested_add(uint32_t value)
{
    return leaf_add(value) + 2;
}
