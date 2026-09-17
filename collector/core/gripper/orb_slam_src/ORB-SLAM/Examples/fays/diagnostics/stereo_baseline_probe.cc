// Regression diagnostic: construct a stereo Frame in controlled storage.
// Usage: stereo_baseline_probe LEFT_RECTIFIED.png RIGHT_RECTIFIED.png SEED_MB
// No SDK/USB access. The old constructor consumed the seeded bytes before
// initializing mb; a fixed constructor must give identical depths for all seeds.
// Calibration below belongs only to the captured regression fixture.
#include <Frame.h>
#include <ORBextractor.h>
#include <CameraModels/Pinhole.h>
#include <opencv2/imgcodecs.hpp>
#include <iostream>
#include <cstring>
#include <type_traits>
#include <cmath>
int main(int argc,char** argv) {
 if(argc!=4)return 2;
 cv::Mat l=cv::imread(argv[1],0),r=cv::imread(argv[2],0);
 cv::Mat k=(cv::Mat_<float>(3,3)<<184.1735081,0,310.5946142,0,184.1735081,189.5770474,0,0,1);
 cv::Mat d=cv::Mat::zeros(4,1,CV_32F);
 ORB_SLAM3::ORBextractor el(1400,1.1f,5,20,7),er(1400,1.1f,5,20,7);
 ORB_SLAM3::Pinhole camera({184.1735081f,184.1735081f,310.5946142f,189.5770474f});
 using F=ORB_SLAM3::Frame;
 typename std::aligned_storage<sizeof(F),alignof(F)>::type storage;
 std::memset(&storage,0,sizeof(storage));
 const float seed=std::stof(argv[3]);
 std::memcpy(reinterpret_cast<char*>(&storage)+offsetof(F,mb),&seed,sizeof(seed));
 F* f=new(&storage) F(l,r,0.,&el,&er,nullptr,k,d,184.1735081f*0.080700041f,40*0.080700041f,&camera);
 int valid=0;for(auto depth:f->mvDepth)if(std::isfinite(depth)&&depth>0)++valid;
 std::cout<<"RESULT seed_mb="<<seed<<" final_mb="<<f->mb<<" left="<<f->mvKeys.size()<<" right="<<f->mvKeysRight.size()<<" valid_depth="<<valid<<std::endl;
 f->~Frame();
}
