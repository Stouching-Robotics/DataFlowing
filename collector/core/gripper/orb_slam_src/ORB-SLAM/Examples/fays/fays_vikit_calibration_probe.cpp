#include "fays_sdk_shutdown.h"

#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <string>

#include "fays_atrak/fays_atrak_types.h"
#include "fays_atrak/fays_vikit.h"

namespace {

std::string boundedString(const char* value, std::size_t capacity) {
    std::size_t length = 0;
    while (length < capacity && value[length] != '\0') ++length;
    return std::string(value, length);
}

void printFloatArray(const char* key, const float* values, std::size_t count) {
    std::cout << key << "=[";
    for (std::size_t index = 0; index < count; ++index) {
        if (index != 0) std::cout << ",";
        std::cout << values[index];
    }
    std::cout << "]\n";
}

void printExtrinsics(const char* key, const AtrakExtrinsics& extrinsics) {
    const std::string prefix(key);
    printFloatArray((prefix + ".rot_row_major").c_str(), extrinsics.rot, 9);
    printFloatArray((prefix + ".trans").c_str(), extrinsics.trans, 3);
}

}  // namespace

int main(int argc, char** argv) {
    const char* config_path = nullptr;
    bool serial_only_fast = false;
    for (int index = 1; index < argc; ++index) {
        if (std::string(argv[index]) == "--serial-only-fast") {
            serial_only_fast = true;
        } else if (config_path == nullptr) {
            config_path = argv[index];
        } else {
            config_path = nullptr;
            break;
        }
    }
    if (config_path == nullptr) {
        std::cerr << "usage: " << argv[0]
                  << " [--serial-only-fast] <fays_vikit.yaml>\n";
        return EXIT_FAILURE;
    }

    void* handle = nullptr;
    if (FAYS_VIK_CreateHandleWithConfig(&handle, config_path) != EXIT_SUCCESS ||
        handle == nullptr) {
        std::cerr << "FAYS_VIK_CreateHandleWithConfig failed\n";
        return EXIT_FAILURE;
    }

    ViKitDeviceInfo device{};
    if (FAYS_VIK_GetDeviceInfo(handle, &device) != EXIT_SUCCESS) {
        std::cerr << "FAYS_VIK_GetDeviceInfo failed\n";
        ksq_fays::stopOwnedImuStream(config_path);
        FAYS_VIK_DestroyHandle(handle);
        return EXIT_FAILURE;
    }

    if (serial_only_fast) {
        std::cout << "device.serial="
                  << boundedString(device.serial_number,
                                   sizeof(device.serial_number))
                  << "\n";
        std::cout.flush();
        ksq_fays::stopOwnedImuStream(config_path);
        std::cout.flush();
        std::cerr.flush();
        std::_Exit(EXIT_SUCCESS);
    }

    AtrakCalibrationParam calibration{};
    const int result = FAYS_VIK_GetCalibrationParam(handle, &calibration);
    if (result != EXIT_SUCCESS) {
        std::cerr << "FAYS_VIK_GetCalibrationParam failed: " << result << "\n";
        ksq_fays::stopOwnedImuStream(config_path);
        FAYS_VIK_DestroyHandle(handle);
        return EXIT_FAILURE;
    }

    std::cout << std::fixed << std::setprecision(15)
              << "device.model="
              << boundedString(device.device_model, sizeof(device.device_model))
              << "\n"
              << "device.serial="
              << boundedString(device.serial_number, sizeof(device.serial_number))
              << "\n"
              << "device.firmware="
              << boundedString(device.firmware_version,
                               sizeof(device.firmware_version))
              << "\n"
              << "camera.num_of_cams=" << calibration.cameras.num_of_cams
              << "\n"
              << "camera.downsize_ratio=" << calibration.cameras.downsize_ratio
              << "\n";

    for (uint32_t index = 0; index < calibration.cameras.num_of_cams;
         ++index) {
        const AtrakCamParam& camera = calibration.cameras.cameras[index];
        const AtrakIntrinsics& intrinsics = camera.intrinsics;
        const std::string prefix = "camera[" + std::to_string(index) + "]";
        std::cout << prefix << ".cam_id="
                  << static_cast<unsigned int>(camera.cam_id) << "\n"
                  << prefix << ".available_mask="
                  << static_cast<unsigned int>(camera.available_mask) << "\n"
                  << prefix << ".has_intrinsics="
                  << ((camera.available_mask & (1u << 0)) != 0) << "\n"
                  << prefix << ".has_T_cn_cnm1="
                  << ((camera.available_mask & (1u << 1)) != 0) << "\n"
                  << prefix << ".has_T_cn_imu="
                  << ((camera.available_mask & (1u << 2)) != 0) << "\n"
                  << prefix << ".has_timeshift_cam_imu="
                  << ((camera.available_mask & (1u << 3)) != 0) << "\n"
                  << prefix << ".intrinsics.cam_model="
                  << static_cast<unsigned int>(intrinsics.cam_model) << "\n"
                  << prefix << ".intrinsics.resolution=[" << intrinsics.width
                  << "," << intrinsics.height << "]\n"
                  << prefix << ".intrinsics.fxfycxcy=[" << intrinsics.fx
                  << "," << intrinsics.fy << "," << intrinsics.cx << ","
                  << intrinsics.cy << "]\n"
                  << prefix << ".intrinsics.distortion_model="
                  << static_cast<unsigned int>(intrinsics.distortion_model)
                  << "\n";
        printFloatArray((prefix + ".intrinsics.intrinsic_extra").c_str(),
                        intrinsics.intrinsic_extra, 4);
        printFloatArray((prefix + ".intrinsics.distortion").c_str(),
                        intrinsics.distortion, 8);
        printExtrinsics((prefix + ".T_cn_cnm1").c_str(), camera.T_cn_cnm1);
        printExtrinsics((prefix + ".T_cn_imu").c_str(), camera.T_cn_imu);
        std::cout << prefix << ".timeshift_cam_imu="
                  << camera.timeshift_cam_imu << "\n";
    }

    const AtrakImuParam& imu = calibration.imu;
    std::cout << "imu.accelerometer_noise_density="
              << imu.accelerometer_noise_density << "\n"
              << "imu.accelerometer_random_walk="
              << imu.accelerometer_random_walk << "\n"
              << "imu.gyroscope_noise_density="
              << imu.gyroscope_noise_density << "\n"
              << "imu.gyroscope_random_walk="
              << imu.gyroscope_random_walk << "\n"
              << "imu.update_rate=" << imu.update_rate << "\n";

    ksq_fays::stopOwnedImuStream(config_path);
    FAYS_VIK_DestroyHandle(handle);
    std::cerr << "[FAYS-CLEANUP] sdk_destroyed\n";
    return EXIT_SUCCESS;
}
