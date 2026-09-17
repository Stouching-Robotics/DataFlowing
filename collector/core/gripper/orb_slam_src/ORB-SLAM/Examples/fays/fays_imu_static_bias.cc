/**
 * @file fays_imu_static_bias.cc
 * @brief Read-only stationary IMU bias/noise diagnostic for FaysSense S80M.
 *
 * Keep the camera completely still while this program runs.  Per-axis
 * accelerometer bias cannot be separated from gravity without a known sensor
 * orientation, so the report gives the mean acceleration vector and its norm
 * error.  Gyroscope means are direct stationary zero-bias estimates.
 */
#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <limits>
#include <mutex>
#include <thread>
#include <vector>

#include "fays_atrak/fays_atrak_types.h"
#include "fays_atrak/fays_vikit.h"

namespace {

struct RunningStats {
    unsigned long long count = 0;
    double mean = 0.0;
    double m2 = 0.0;
    double minimum = std::numeric_limits<double>::infinity();
    double maximum = -std::numeric_limits<double>::infinity();

    void add(double value) {
        ++count;
        const double delta = value - mean;
        mean += delta / static_cast<double>(count);
        m2 += delta * (value - mean);
        minimum = std::min(minimum, value);
        maximum = std::max(maximum, value);
    }

    double stddev() const {
        return count > 1 ? std::sqrt(m2 / static_cast<double>(count - 1)) : 0.0;
    }
};

struct ImuStats {
    std::array<RunningStats, 3> acc;
    std::array<RunningStats, 3> gyro;
    RunningStats timestamp_dt;
    unsigned long long first_timestamp = 0;
    unsigned long long last_timestamp = 0;
    unsigned long long nonmonotonic_timestamps = 0;
};

std::mutex g_mutex;
ImuStats g_stats;
bool g_collecting = false;

void imuCallback(const AtrakIMU& imu) {
    for (int axis = 0; axis < 3; ++axis) {
        if (!std::isfinite(imu.acc[axis]) || !std::isfinite(imu.gyro[axis]))
            return;
    }

    std::lock_guard<std::mutex> lock(g_mutex);
    if (!g_collecting)
        return;

    if (g_stats.first_timestamp == 0)
        g_stats.first_timestamp = imu.timestamp;
    if (g_stats.last_timestamp != 0) {
        if (imu.timestamp > g_stats.last_timestamp) {
            g_stats.timestamp_dt.add(
                static_cast<double>(imu.timestamp - g_stats.last_timestamp) * 1e-9);
        } else {
            ++g_stats.nonmonotonic_timestamps;
        }
    }
    g_stats.last_timestamp = imu.timestamp;

    for (int axis = 0; axis < 3; ++axis) {
        g_stats.acc[axis].add(imu.acc[axis]);
        g_stats.gyro[axis].add(imu.gyro[axis]);
    }
}

double vectorNorm(const std::array<RunningStats, 3>& values) {
    return std::sqrt(values[0].mean * values[0].mean +
                     values[1].mean * values[1].mean +
                     values[2].mean * values[2].mean);
}

void printVector(const char* key, const std::array<RunningStats, 3>& values,
                 bool stddev) {
    std::cout << "  \"" << key << "\": [";
    for (int axis = 0; axis < 3; ++axis) {
        if (axis)
            std::cout << ", ";
        std::cout << (stddev ? values[axis].stddev() : values[axis].mean);
    }
    std::cout << "]";
}

void destroyHandle(void* handle, int exit_code) {
    std::atomic<bool> finished{false};
    std::thread destroy_thread([&finished, handle]() {
        FAYS_VIK_DestroyHandle(handle);
        finished.store(true, std::memory_order_release);
    });
    const auto deadline = std::chrono::steady_clock::now() +
                          std::chrono::seconds(3);
    while (!finished.load(std::memory_order_acquire) &&
           std::chrono::steady_clock::now() < deadline) {
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
    }
    if (finished.load(std::memory_order_acquire)) {
        destroy_thread.join();
        return;
    }

    // VIKit 3.5.2 can wait indefinitely while joining its image worker when a
    // short-lived diagnostic closes.  The report is already flushed; process
    // teardown closes the device descriptors without resetting the USB device.
    std::cerr << "Warning: VIKit DestroyHandle did not finish in 3 s; "
                 "ending the diagnostic process to release device handles.\n";
    std::cout.flush();
    std::cerr.flush();
    destroy_thread.detach();
    std::_Exit(exit_code);
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2 || argc > 5) {
        std::cerr << "Usage: " << argv[0]
                  << " <fays_config.yaml> [sample_seconds=10] [warmup_seconds=2]"
                     " [gravity_m_s2=9.7946]\n";
        return EXIT_FAILURE;
    }

    const double sample_seconds = argc >= 3 ? std::atof(argv[2]) : 10.0;
    const double warmup_seconds = argc >= 4 ? std::atof(argv[3]) : 2.0;
    const double gravity = argc >= 5 ? std::atof(argv[4]) : 9.7946;
    if (!(sample_seconds > 0.0) || warmup_seconds < 0.0 || !(gravity > 0.0)) {
        std::cerr << "Durations and gravity must be valid positive values.\n";
        return EXIT_FAILURE;
    }

    void* handle = nullptr;
    if (FAYS_VIK_CreateHandleWithConfig(&handle, argv[1]) != EXIT_SUCCESS || !handle) {
        std::cerr << "Failed to create Fays VIKit handle.\n";
        return EXIT_FAILURE;
    }
    // VIKit 3.5.2 starts synchronized S80M delivery only while the stereo
    // transport is being consumed.  Drain frames without decoding or saving
    // them so the IMU test exercises the same SDK path as production SLAM.
    AtrakImage image{};
    std::vector<unsigned char> image_buffer(FAYS_ATRAK_MONO_MAX_BYTES * 3u);
    image.data = image_buffer.data();
    std::atomic<bool> drain_images{true};
    std::thread image_thread([&]() {
        while (drain_images.load(std::memory_order_acquire)) {
            FAYS_VIK_GetStereoFrames(handle, &image);
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
    });
    std::cerr << "Keep the S80M stationary: warmup=" << warmup_seconds
              << " s, sample=" << sample_seconds << " s\n";
    AtrakIMU imu{};
    const auto warmup_deadline = std::chrono::steady_clock::now() +
        std::chrono::duration<double>(warmup_seconds);
    while (std::chrono::steady_clock::now() < warmup_deadline) {
        FAYS_VIK_GetImuData(handle, &imu);
        std::this_thread::sleep_for(std::chrono::microseconds(100));
    }
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        g_stats = ImuStats{};
        g_collecting = true;
    }
    const auto sample_deadline = std::chrono::steady_clock::now() +
        std::chrono::duration<double>(sample_seconds);
    while (std::chrono::steady_clock::now() < sample_deadline) {
        if (FAYS_VIK_GetImuData(handle, &imu) == EXIT_SUCCESS)
            imuCallback(imu);
        std::this_thread::sleep_for(std::chrono::microseconds(100));
    }

    ImuStats result;
    {
        std::lock_guard<std::mutex> lock(g_mutex);
        g_collecting = false;
        result = g_stats;
    }
    drain_images.store(false, std::memory_order_release);
    image_thread.join();
    if (result.acc[0].count < 2) {
        std::cerr << "Insufficient IMU samples: " << result.acc[0].count << "\n";
        destroyHandle(handle, EXIT_FAILURE);
        return EXIT_FAILURE;
    }

    const double acc_norm = vectorNorm(result.acc);
    const double gyro_norm = vectorNorm(result.gyro);
    const double timestamp_span = result.last_timestamp > result.first_timestamp
        ? static_cast<double>(result.last_timestamp - result.first_timestamp) * 1e-9
        : 0.0;
    const double sample_hz = timestamp_span > 0.0
        ? static_cast<double>(result.acc[0].count - 1) / timestamp_span
        : 0.0;
    const double radians_to_degrees = 180.0 / std::acos(-1.0);

    std::cout << std::fixed << std::setprecision(9) << "{\n";
    std::cout << "  \"samples\": " << result.acc[0].count << ",\n";
    std::cout << "  \"sample_hz\": " << sample_hz << ",\n";
    std::cout << "  \"timestamp_dt_mean_s\": " << result.timestamp_dt.mean << ",\n";
    std::cout << "  \"timestamp_dt_std_s\": " << result.timestamp_dt.stddev() << ",\n";
    std::cout << "  \"nonmonotonic_timestamps\": "
              << result.nonmonotonic_timestamps << ",\n";
    printVector("acc_mean_m_s2", result.acc, false);
    std::cout << ",\n";
    printVector("acc_std_m_s2", result.acc, true);
    std::cout << ",\n";
    std::cout << "  \"acc_mean_norm_m_s2\": " << acc_norm << ",\n";
    std::cout << "  \"acc_norm_error_m_s2\": " << acc_norm - gravity << ",\n";
    printVector("gyro_bias_rad_s", result.gyro, false);
    std::cout << ",\n";
    printVector("gyro_std_rad_s", result.gyro, true);
    std::cout << ",\n";
    std::cout << "  \"gyro_bias_norm_rad_s\": " << gyro_norm << ",\n";
    std::cout << "  \"gyro_bias_norm_deg_s\": "
              << gyro_norm * radians_to_degrees << ",\n";
    std::cout << "  \"gyro_bias_drift_deg_per_min\": "
              << gyro_norm * radians_to_degrees * 60.0 << "\n";
    std::cout << "}\n";
    std::cout.flush();
    destroyHandle(handle, EXIT_SUCCESS);
    return EXIT_SUCCESS;
}
