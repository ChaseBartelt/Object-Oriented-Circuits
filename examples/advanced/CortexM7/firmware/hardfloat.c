#include <stdint.h>

extern volatile uint32_t large_result;
extern volatile uint32_t large_status;

struct calibration {
    int32_t reading;
    uint32_t offset;
    uint32_t accumulated;
    uint32_t sample_count;
    float gain;
    float range;
    float threshold;
};

// Model four sensor channels. Volatile reads exercise the compiled data path.
static const volatile struct calibration channels[] = {
    {12, 5, 27, 4, 1.5f, 4.0f, 4.25f},
    {-13, 8, 100, 9, 0.5f, 2.0f, -2.0f},
    {9, 11, 249, 11, 2.0f, 8.0f, 2.5f},
    {28, 3, 411, 16, 0.75f, 4.0f, 5.25f},
};

static volatile struct {
    float product;
    float ratio;
    int32_t signed_total;
    uint32_t processed;
} live_results;

// The call keeps the compiled single-precision argument and return ABI observable.
__attribute__((noinline))
static float calibrate(float product, float range)
{
    return product / range;
}

void large_main(void)
{
    uint32_t checksum = 0;
    uint32_t threshold_mask = 0;
    int32_t signed_total = 0;
    for (uint32_t i = 0; i < sizeof channels / sizeof *channels; ++i) {
        const volatile struct calibration *channel = &channels[i];
        float product = (float)channel->reading * channel->gain;
        float ratio = calibrate(product, channel->range);
        checksum += (uint32_t)(ratio + (float)channel->offset);
        checksum += channel->accumulated / channel->sample_count;
        signed_total += (int32_t)ratio;
        if (ratio >= channel->threshold) {
            threshold_mask |= 1u << i;
        }
        live_results.product = product;
        live_results.ratio = ratio;
        live_results.signed_total = signed_total;
        live_results.processed = i + 1;
    }
    large_result = checksum;
    large_status = threshold_mask;
}
