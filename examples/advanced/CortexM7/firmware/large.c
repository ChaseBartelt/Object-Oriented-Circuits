#include <stdint.h>

extern volatile uint32_t large_result;
extern volatile uint32_t large_status;

volatile uint32_t checksum_seed = 0x12345678;
volatile uint32_t sensor_zero = 1000;
volatile uint32_t calibration_trim = 3;
volatile uint32_t checksum_bias;
volatile uint32_t processed_samples;

// Fixed 12-bit sensor samples stored in flash. Each load is visible on the bus.
static const volatile uint32_t sensor_samples[] = {
    1202, 1217, 1236, 1250, 1269, 1285, 1300, 1314,
    1326, 1338, 1343, 1351, 1356, 1357, 1359, 1354,
    1348, 1343, 1335, 1321, 1309, 1293, 1278, 1261,
    1246, 1229, 1210, 1194, 1176, 1161, 1145, 1133,
    1121, 1110, 1100, 1091, 1086, 1085, 1083, 1086,
    1093, 1098, 1108, 1121, 1135, 1151, 1168, 1186,
    1203, 1220, 1236, 1255, 1272, 1285, 1303, 1315,
    1327, 1337, 1345, 1350, 1357, 1360, 1358, 1355,
};

__attribute__((noinline))
static uint32_t remove_sensor_zero(uint32_t sample)
{
    return sample - sensor_zero;
}

__attribute__((noinline))
static uint32_t calibrate(uint32_t sample)
{
    return remove_sensor_zero(sample) + calibration_trim;
}

void large_main(void)
{
    uint32_t checksum = checksum_seed + checksum_bias;
    const volatile uint32_t *end = sensor_samples + sizeof sensor_samples / sizeof *sensor_samples;
    for (const volatile uint32_t *sample = sensor_samples; sample != end; ++sample) {
        checksum += calibrate(*sample);
        ++processed_samples;
    }
    large_result = checksum;
    large_status = processed_samples;
}
