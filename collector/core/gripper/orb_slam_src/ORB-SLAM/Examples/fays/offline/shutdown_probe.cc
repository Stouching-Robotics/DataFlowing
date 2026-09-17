// Deterministic regression for headless System's previously uninitialized thread.
#include "System.h"
#include <cstring>
#include <cstdlib>
#include <iostream>
#include <new>
#include <type_traits>
#include <sched.h>
int main(int argc,char**argv) {
    if(argc!=3) return 2;
    const char* cpu=std::getenv("KSQ_FAYS_TRACK_CPU");
    if(!cpu) return 2;
    cpu_set_t affinity;CPU_ZERO(&affinity);CPU_SET(std::stoi(cpu),&affinity);
    if(sched_setaffinity(0,sizeof(affinity),&affinity)) return 2;
    cv::setNumThreads(1);
    std::aligned_storage<sizeof(ORB_SLAM3::System),alignof(ORB_SLAM3::System)>::type storage;
    // A valid constructor must initialize every pointer read by Shutdown,
    // regardless of the allocator's prior contents (stack or heap).
    std::memset(&storage,0xA5,sizeof(storage));
    auto* system=new(&storage) ORB_SLAM3::System(argv[1],argv[2],ORB_SLAM3::System::IMU_STEREO,false);
    system->Shutdown();
    // Serial repeated shutdown must not join an already joined thread.
    system->Shutdown();
    system->~System();
    std::cout<<"PASS headless poisoned-storage System shutdown"<<std::endl;
    return 0;
}
