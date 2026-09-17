#ifndef __PRINT_HELPERS_H__
#define __PRINT_HELPERS_H__

#include "fays_atrak/fays_vikit.h"

bool PrintDeviceInfo(void *handle) {
    ViKitDeviceInfo info;
    if (FAYS_VIK_GetDeviceInfo(handle, &info) == EXIT_SUCCESS) {
        std::cout << "Device Model: " << info.device_model << std::endl;
        std::cout << "Serial Number: " << info.serial_number << std::endl;
        std::cout << "Firmware Version: " << info.firmware_version << std::endl;
        std::cout << "Number of Cameras: " << info.camera_nums << std::endl;
        std::cout << "Number of IMUs: " << info.imu_nums << std::endl;
        return true;
    }
    return false;
}

void printTransform(const std::string& name, const AtrakExtrinsics& transform) {
    std::cout << std::fixed << std::setprecision(6);
    std::cout << "  " << name << ":" << std::endl;
    
    for (int j = 0; j < 9; ++j) {
        std::cout << std::setw(10) << transform.rot[j] << " ";
        
        if ((j + 1) % 3 == 0) {
            std::cout << std::setw(10) << transform.trans[j/3] << std::endl;
        }
    }
    std::cout.unsetf(std::ios::fixed);
}

bool PrintCalibrationInfo(void *handle) {
    AtrakCalibrationParam calibParam;
    if (EXIT_SUCCESS != FAYS_VIK_GetCalibrationParam(handle, &calibParam)) {
        return false;
    }

    // 设置输出流精度为小数点后6位
    std::cout << std::fixed << std::setprecision(6);

    std::cout << "Calibration camera number: " << calibParam.cameras.num_of_cams << std::endl;
    for (uint32_t i = 0; i < calibParam.cameras.num_of_cams; ++i) {
        std::cout << "==================================camera " << i << "==================================" << std::endl;
        const AtrakCamParam& cam = calibParam.cameras.cameras[i];
        std::cout << "Camera " << static_cast<int>(cam.cam_id) << " Intrinsics:" << std::endl;
        std::cout << "model_type: ";
        if (cam.intrinsics.cam_model == ACM_PINHOLE) {
            std::cout << "Pinhole";
        } else {
            std::cout << "Unknown";
        }
        std::cout << std::endl;
        std::cout << "  Image Size: (" << cam.intrinsics.width << ", " << cam.intrinsics.height << ")" << std::endl;
        std::cout << "  Focal Length: (" << cam.intrinsics.fx << ", " << cam.intrinsics.fy << ")" << std::endl;
        std::cout << "  Principal Point: (" << cam.intrinsics.cx << ", " << cam.intrinsics.cy << ")" << std::endl;
        std::cout << "  intrinsics extra: ";
        for (int j = 0; j < 4; ++j) {
            std::cout << cam.intrinsics.intrinsic_extra[j] << " ";
        }
        std::cout << std::endl;
        std::cout << "  Distortion Model: ";
        if (cam.intrinsics.distortion_model == ADM_NONE) {
            std::cout << "None";
        } else if (cam.intrinsics.distortion_model == ADM_KB4) {
            std::cout << "KB4";
        } else if (cam.intrinsics.distortion_model == ADM_RADTAN) {
            std::cout << "Radtan";
        } else if (cam.intrinsics.distortion_model == ADM_BROWN_CONRADY) {
            std::cout << "Brown-Conrady";
        } else {
            std::cout << "Unknown";
        }
        std::cout << std::endl;
        std::cout << "  Distortion Coefficients: ";
        for (int j = 0; j < 8; ++j) {
            std::cout << cam.intrinsics.distortion[j] << " ";
        }
        std::cout << std::endl;

        printTransform("T_cn_cnm1", cam.T_cn_cnm1);
        printTransform("T_cam_imu", cam.T_cn_imu);
        std::cout << "  timeshift_cam_imu: " << cam.timeshift_cam_imu << " s" << std::endl;
        if (i == calibParam.cameras.num_of_cams - 1) {
            std::cout << "==============================================imu==============================================" << std::endl;
        }
    }

    std::cout << "IMU Calibration Parameters:" << std::endl;
    std::cout << "  Accelerometer Noise Density: " << calibParam.imu.accelerometer_noise_density << std::endl;
    std::cout << "  Accelerometer Random Walk: " << calibParam.imu.accelerometer_random_walk << std::endl;
    std::cout << "  Gyroscope Noise Density: " << calibParam.imu.gyroscope_noise_density << std::endl;
    std::cout << "  Gyroscope Random Walk: " << calibParam.imu.gyroscope_random_walk << std::endl;
    std::cout << "  Update Rate: " << calibParam.imu.update_rate << std::endl;
    std::cout << "==============================================================================================" << std::endl;

    return true;
}

#endif // __PRINT_HELPERS_H__