#include <stddef.h>
#include <stdint.h>

void *memset(void *destination, int value, size_t count)
{
    unsigned char *output = destination;
    while (count--) {
        *output++ = (unsigned char)value;
    }
    return destination;
}

void *memcpy(void *destination, const void *source, size_t count)
{
    unsigned char *output = destination;
    const unsigned char *input = source;
    while (count--) {
        *output++ = *input++;
    }
    return destination;
}

uint64_t __aeabi_lmul(uint64_t left, uint64_t right)
{
    // Multiply in 16-bit pieces so Thumb-1 only needs its native 32-bit multiply.
    uint32_t a = (uint32_t)left;
    uint32_t b = (uint32_t)right;
    uint32_t low_product = (a & 0xFFFF) * (b & 0xFFFF);
    uint32_t left_cross = (a >> 16) * (b & 0xFFFF);
    uint32_t right_cross = (a & 0xFFFF) * (b >> 16);
    uint32_t middle = (low_product >> 16) + (left_cross & 0xFFFF) + (right_cross & 0xFFFF);
    uint32_t low = (low_product & 0xFFFF) | (middle << 16);
    uint32_t high = (a >> 16) * (b >> 16) + (left_cross >> 16) + (right_cross >> 16)
        + (middle >> 16) + (uint32_t)(left >> 32) * b + (uint32_t)(right >> 32) * a;
    return ((uint64_t)high << 32) | low;
}
