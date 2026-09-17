// Hardware-free stereo/IMU replay against the checkout's existing Fays ORB core.
// Never opens the SDK, USB, serial, GUI, or production recording destinations.
#include "System.h"
#include <opencv2/imgcodecs.hpp>
#include <opencv2/imgproc.hpp>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <thread>
#include <vector>
#include <sched.h>
#include <sys/resource.h>

using Clock = std::chrono::steady_clock;
struct Image { long long ns; std::string file; };
struct Imu { long long ns; double gx,gy,gz,ax,ay,az; };
std::vector<std::string> fields(std::string line) {
    if (!line.empty() && line.back()=='\r') line.pop_back();
    std::replace(line.begin(),line.end(),',',' ');
    std::istringstream in(line); std::vector<std::string> result; std::string v;
    while(in>>v) result.push_back(v);
    return result;
}
std::vector<Image> images(const std::string& path) {
    std::ifstream in(path); if(!in) throw std::runtime_error("Cannot read "+path);
    std::vector<Image> rows; std::string line;
    while(std::getline(in,line)) {
        if(line.empty() || line[0]=='#') continue;
        auto f=fields(line); if(f.size()!=2) throw std::runtime_error("Bad image CSV row");
        rows.push_back({std::stoll(f[0]),f[1]});
        if(rows.size()>1 && rows.back().ns<=rows[rows.size()-2].ns) throw std::runtime_error("Non-monotonic image timestamps");
    }
    return rows;
}
std::vector<Imu> imu(const std::string& path) {
    std::ifstream in(path); if(!in) throw std::runtime_error("Cannot read "+path);
    std::vector<Imu> rows; std::string line;
    while(std::getline(in,line)) {
        if(line.empty() || line[0]=='#') continue;
        auto f=fields(line); if(f.size()!=7) throw std::runtime_error("Bad IMU CSV row");
        rows.push_back({std::stoll(f[0]),std::stod(f[1]),std::stod(f[2]),std::stod(f[3]),std::stod(f[4]),std::stod(f[5]),std::stod(f[6])});
        if(rows.size()>1 && rows.back().ns<=rows[rows.size()-2].ns) throw std::runtime_error("Non-monotonic IMU timestamps");
    }
    return rows;
}
int role(const char* key) {
    const char* text=std::getenv(key); if(!text) throw std::runtime_error(std::string("Missing CPU policy: ")+key);
    int n=std::stoi(text); if(n<0 || n>=CPU_SETSIZE) throw std::runtime_error("Invalid CPU policy");
    return n;
}
void bind(int cpu) {
    cpu_set_t set;CPU_ZERO(&set);CPU_SET(cpu,&set);
    if(sched_setaffinity(0,sizeof(set),&set)) throw std::runtime_error("CPU affinity rejected");
}
double ms(Clock::time_point a,Clock::time_point b) {
    return std::chrono::duration<double,std::milli>(b-a).count();
}
int main(int argc,char** argv) {
    if(argc!=9) {
        std::cerr<<"usage: replay VOC SETTINGS MAV0 OUTPUT_DIR MODE FAULT_START_S FAULT_DURATION_S REALTIME\n"
                 <<"MODE: normal | blank_stereo | imu_gap | imu_zero | imu_one | imu_stale | imu_delayed (no disk data altered)\n";
        return 2;
    }
    try {
        const std::string root=argv[3],out=argv[4],mode=argv[5];
        if(mode!="normal" && mode!="blank_stereo" && mode!="imu_gap" && mode!="imu_zero" && mode!="imu_one" && mode!="imu_stale" && mode!="imu_delayed") throw std::runtime_error("Unknown mode");
        const double fault_start=std::stod(argv[6]),fault_duration=std::stod(argv[7]);
        const bool realtime=std::stoi(argv[8])!=0;
        const int input_cpu=role("KSQ_FAYS_INPUT_CPU"),prepare_cpu=role("KSQ_FAYS_PREPARE_CPU"),track_cpu=role("KSQ_FAYS_TRACK_CPU");
        bind(input_cpu);cv::setNumThreads(1);cv::setUseOptimized(true);
        auto left=images(root+"/cam0/data.csv"),right=images(root+"/cam1/data.csv");auto inertial=imu(root+"/imu0/data.csv");
        if(left.empty() || left.size()!=right.size() || inertial.size()<2) throw std::runtime_error("Incomplete stereo/IMU input");
        for(size_t i=0;i<left.size();++i) if(left[i].ns!=right[i].ns) throw std::runtime_error("Stereo timestamp mismatch");
        if(inertial.front().ns>left.front().ns || inertial.back().ns<left.back().ns) throw std::runtime_error("IMU does not cover image interval");
        cv::FileStorage config(argv[2],cv::FileStorage::READ);
        cv::Mat Tbc;config["IMU.T_b_c1"]>>Tbc;
        if(Tbc.rows!=4 || Tbc.cols!=4) throw std::runtime_error("Missing body/camera extrinsics");
        Tbc.convertTo(Tbc,CV_32F);
        Eigen::Matrix4f m;for(int r=0;r<4;++r)for(int c=0;c<4;++c)m(r,c)=Tbc.at<float>(r,c);
        const Sophus::SE3f body_from_camera(m);
        std::ofstream metrics(out+"/frames.csv"),poses(out+"/online_body.txt");
        if(!metrics || !poses) throw std::runtime_error("Cannot create output files");
        metrics<<"index,timestamp_ns,state,imu_count,read_ms,prepare_ms,track_ms,max_rss_kib,injected\n";
        poses<<std::fixed<<std::setprecision(9);
        std::cout<<"REPLAY frames="<<left.size()<<" duration_s="<<(left.back().ns-left.front().ns)/1e9
                 <<" mode="<<mode<<" input_cpu="<<input_cpu<<" prepare_cpu="<<prepare_cpu<<" track_cpu="<<track_cpu<<std::endl;
        bind(track_cpu);
        ORB_SLAM3::System slam(argv[1],argv[2],ORB_SLAM3::System::IMU_STEREO,false);
        auto clahe=cv::createCLAHE(3.0,cv::Size(8,8));
        size_t j=0;while(j+1<inertial.size() && inertial[j+1].ns<=left[0].ns)++j;
        auto beginning=Clock::now();size_t ok=0,injections=0;
        std::vector<ORB_SLAM3::IMU::Point> delayed;
        for(size_t i=0;i<left.size();++i) {
            double elapsed=(left[i].ns-left[0].ns)/1e9;
            if(realtime) std::this_thread::sleep_until(beginning+std::chrono::duration_cast<Clock::duration>(std::chrono::duration<double>(elapsed)));
            auto start=Clock::now();bind(input_cpu);
            cv::Mat l=cv::imread(root+"/cam0/data/"+left[i].file,cv::IMREAD_GRAYSCALE);
            cv::Mat r=cv::imread(root+"/cam1/data/"+right[i].file,cv::IMREAD_GRAYSCALE);
            if(l.empty()||r.empty()) throw std::runtime_error("Image decode failed at "+std::to_string(i));
            auto read=Clock::now();bind(prepare_cpu);
            const float scale=slam.GetImageScale();
            if(scale!=1.f){cv::resize(l,l,cv::Size(),scale,scale);cv::resize(r,r,cv::Size(),scale,scale);}
            // Fays production receives rectified gray without CLAHE. Keep the
            // TUM-VI harness default, but allow codec comparisons to match Fays.
            if (!std::getenv("KSQ_OFFLINE_NO_CLAHE")) {
                clahe->apply(l,l);clahe->apply(r,r);
            }
            bool injected=false;
            if(mode=="blank_stereo" && elapsed>=fault_start && elapsed<fault_start+fault_duration) {l.setTo(0);r.setTo(0);injected=true;}
            std::vector<ORB_SLAM3::IMU::Point> packet;
            if(i>0) while(j<inertial.size() && inertial[j].ns<=left[i].ns) {
                const auto& v=inertial[j++]; double it=(v.ns-left[0].ns)/1e9;
                if(mode=="imu_gap" && it>=fault_start && it<fault_start+fault_duration) {injected=true;continue;}
                packet.emplace_back(v.ax,v.ay,v.az,v.gx,v.gy,v.gz,v.ns/1e9);
            }
            if (elapsed>=fault_start && elapsed<fault_start+fault_duration) {
                if (mode=="imu_zero") {packet.clear();injected=true;}
                if (mode=="imu_one") {
                    if (packet.size()>1) packet.erase(packet.begin(),packet.end()-1);
                    injected=true;
                }
                if (mode=="imu_stale") {
                    if (packet.size()>1) packet.erase(packet.begin()+1,packet.end());
                    for(auto& point:packet) point.t -= 1.;
                    injected=true;
                }
                if (mode=="imu_delayed") {
                    delayed.insert(delayed.end(),packet.begin(),packet.end());
                    packet.clear();injected=true;
                }
            } else if (!delayed.empty()) {
                packet.insert(packet.begin(),delayed.begin(),delayed.end());delayed.clear();
            }
            if(injected) std::cout<<"INJECT frame="<<i<<" sensor_ns="<<left[i].ns
                                  <<" input_imu="<<packet.size()<<" mode="<<mode<<std::endl;
            auto prepared=Clock::now();bind(track_cpu);
            const auto Tcw=slam.TrackStereo(l,r,left[i].ns/1e9,packet);
            auto tracked=Clock::now();const int state=slam.GetTrackingState();
            struct rusage usage;getrusage(RUSAGE_SELF,&usage);
            metrics<<i<<','<<left[i].ns<<','<<state<<','<<packet.size()<<','<<ms(start,read)<<','<<ms(read,prepared)<<','<<ms(prepared,tracked)<<','<<usage.ru_maxrss<<','<<injected<<'\n';
            if(state==2) {
                ++ok;
                const Sophus::SE3f Twb=Tcw.inverse()*body_from_camera.inverse();
                auto p=Twb.translation();auto q=Twb.unit_quaternion();
                poses<<left[i].ns/1e9<<' '<<p.x()<<' '<<p.y()<<' '<<p.z()<<' '<<q.x()<<' '<<q.y()<<' '<<q.z()<<' '<<q.w()<<'\n';
            }
            if(injected)++injections;
            if(i%100==0) {metrics.flush();poses.flush();std::cout<<"PROGRESS "<<i<<'/'<<left.size()<<" state="<<state<<" track_ms="<<ms(prepared,tracked)<<" rss_kib="<<usage.ru_maxrss<<std::endl;}
        }
        metrics.flush();poses.flush();
        std::cout<<"REPLAY_FINISHED ok="<<ok<<" total="<<left.size()<<" injected_frames="<<injections<<std::endl;
        slam.Shutdown();
        slam.SaveTrajectoryEuRoC(out+"/optimized_body_ns.txt");
        slam.SaveKeyFrameTrajectoryEuRoC(out+"/keyframes_body_ns.txt");
        std::ofstream done(out+"/completed.txt");done<<"frames="<<left.size()<<"\nok="<<ok<<"\ninjected_frames="<<injections<<"\n";
        std::cout<<"CLEAN_EXIT"<<std::endl;
        return 0;
    } catch(const std::exception& e) {std::cerr<<"REPLAY_ERROR "<<e.what()<<std::endl;return 2;}
}
