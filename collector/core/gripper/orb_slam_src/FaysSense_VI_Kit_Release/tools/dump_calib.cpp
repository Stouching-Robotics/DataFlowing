#include <string>
#include <iostream>
#include <opencv2/opencv.hpp>
#include "fays_atrak/fays_atrak_types.h"
#include "fays_atrak/fays_vikit.h"

int main(int argc, char** argv) {
    if (argc < 2) {
        std::cerr << "Usage: " << argv[0] << " <config_path>" << std::endl;
        return EXIT_FAILURE;
    }

    // 创建SDK句柄
    void* handle = nullptr;
    if (FAYS_VIK_CreateHandleWithConfig(&handle, argv[1]) == EXIT_FAILURE) {
        std::cerr << "Failed to create SDK handle." << std::endl;
        return EXIT_FAILURE;
    }

    // 导出校准参数到当前目录
    if (EXIT_SUCCESS == FAYS_VIK_DumpCalib(handle, "./")) {
        std::cout << "Calibration parameters dumped successfully." << std::endl;
    } else {
        std::cerr << "Failed to dump calibration parameters." << std::endl;
    }

    // 销毁SDK句柄
    FAYS_VIK_DestroyHandle(handle);
    
    return EXIT_SUCCESS;
}