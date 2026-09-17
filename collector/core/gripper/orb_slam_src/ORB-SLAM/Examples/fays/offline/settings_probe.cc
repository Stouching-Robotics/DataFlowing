// The two fish-eye cameras must consume their own calibration, not camera 1 twice.
#include "Settings.h"
#include "System.h"
#include <cmath>
#include <iostream>
int main(int argc,char**argv) {
    if(argc!=2) return 2;
    cv::FileStorage file(argv[1],cv::FileStorage::READ);
    ORB_SLAM3::Settings settings(argv[1],ORB_SLAM3::System::IMU_STEREO);
    for(int camera=1;camera<=2;++camera) for(int k=1;k<=4;++k) {
        const std::string key="Camera"+std::to_string(camera)+".k"+std::to_string(k);
        const float expected=static_cast<float>(file[key]);
        const float actual=(camera==1?settings.camera1():settings.camera2())->getParameter(k+3);
        if(std::abs(expected-actual)>1e-8f) {
            std::cerr<<"FAIL "<<key<<" expected="<<expected<<" actual="<<actual<<std::endl;
            return 1;
        }
    }
    std::cout<<"PASS independent left/right fisheye distortion coefficients"<<std::endl;
    return 0;
}
