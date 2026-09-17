/**
 * @file fayssense_orb_slam.cc
 * @brief S80M 回调模式 → ORB-SLAM3 (单进程, SDK 内部同步 IMU+图像)
 *
 * 注册 FaysSense SDK 回调获取数据, 直接喂 ORB-SLAM3。
 * 回调收集 IMU → 图像到达时 Track → 清空 IMU。
 */
#include <signal.h>
#include <unistd.h>
#include <iostream>
#include <fstream>
#include <thread>
#include <mutex>
#include <atomic>
#include <vector>
#include <chrono>
#include <iomanip>
#include <sstream>
#include <cmath>
#include <cstring>

#include <opencv2/core.hpp>
#include <opencv2/highgui.hpp>
#include <opencv2/imgproc.hpp>
#include <opencv2/calib3d.hpp>
#include <fcntl.h>

#include "fays_atrak/fays_atrak_types.h"
#include "fays_atrak/fays_vikit.h"
#include "common/print_helpers.h"

#include <System.h>
#include <ImuTypes.h>

// ============================================================================
static volatile bool g_running = true;
static void*     g_handle = nullptr;
static ORB_SLAM3::System* g_pSLAM = nullptr;

static void do_cleanup() {
    if (g_handle) { FAYS_VIK_DestroyHandle(g_handle); g_handle = nullptr; }
}

void sigint_handler(int) {
    const char msg[] = "\n[SLAM] Exit.\n"; write(STDERR_FILENO, msg, sizeof(msg)-1);
    g_running = false;
    if (g_pSLAM) g_pSLAM->Shutdown();
    if (g_handle) { FAYS_VIK_DestroyHandle(g_handle); g_handle = nullptr; }
}

// ============================================================================
// IMU 缓冲区 + 图像就绪标志
// ============================================================================
static std::mutex g_imu_mutex;
static std::vector<ORB_SLAM3::IMU::Point> g_imu_buf;

static std::mutex g_img_mutex;
static bool g_img_ready = false;
static double g_img_ts = 0;
static cv::Mat g_imgL, g_imgR;

// 矫正映射 (预计算)
static cv::Mat g_map1x, g_map1y, g_map2x, g_map2y;
// 调试: 记录第一帧图像信息
static bool g_first_img = true;
static int g_img_w = 0, g_img_h = 0, g_img_ch = 0;

// ============================================================================
// SDK 回调
// ============================================================================
static std::ofstream fout_imu;
// 图像保存
static std::string g_img_save_dir;
static int g_img_frame = 0;
static bool g_save_img = false;
void imuCallback(const AtrakIMU& imu) {
    std::lock_guard<std::mutex> lk(g_imu_mutex);
    double t = imu.timestamp * 1e-9;  // SDK 已对齐, 无需补偿
    if (!std::isfinite(imu.acc[0]) || !std::isfinite(imu.gyro[0])) return;
    g_imu_buf.push_back(ORB_SLAM3::IMU::Point(
        (float)imu.acc[0], (float)imu.acc[1], (float)imu.acc[2],
        (float)imu.gyro[0], (float)imu.gyro[1], (float)imu.gyro[2], t));
    // 保存原始 IMU
    if (fout_imu.is_open())
        fout_imu << std::setprecision(6) << t << " "
                 << imu.acc[0] << " " << imu.acc[1] << " " << imu.acc[2] << " "
                 << imu.gyro[0] << " " << imu.gyro[1] << " " << imu.gyro[2] << "\n";
}

void stereoCallback(AtrakImage* img) {
    if (!img || !img->data) return;
    // 记录第一帧信息
    if (g_first_img) { g_first_img=false; g_img_w=img->width; g_img_h=img->height; g_img_ch=img->channel; }
    // S80M: 上下堆叠 (img->height/2 切分)
    int half_h = img->height / 2;
    int ch = img->channel;
    cv::Mat stitched = (ch==1) ? cv::Mat(img->height, img->width, CV_8UC1, img->data)
                               : cv::Mat(img->height, img->width, CV_8UC3, img->data);
    cv::Mat left  = stitched(cv::Rect(0, 0, img->width, half_h)).clone();
    cv::Mat right = stitched(cv::Rect(0, half_h, img->width, half_h)).clone();
    if (ch != 1) {
        cv::cvtColor(left, left, cv::COLOR_BGR2GRAY);
        cv::cvtColor(right, right, cv::COLOR_BGR2GRAY);
    }

    // 矫正 KB4 → Rectified
    cv::Mat Lr, Rr;
    cv::remap(left,  Lr, g_map1x, g_map1y, cv::INTER_LINEAR);
    cv::remap(right, Rr, g_map2x, g_map2y, cv::INTER_LINEAR);

    std::lock_guard<std::mutex> lk(g_img_mutex);
    g_img_ts = img->timestamp * 1e-9;
    g_imgL = Lr; g_imgR = Rr; g_img_ready = true;
}

// ============================================================================
int main(int argc, char** argv) {
    if (argc < 4) {
        std::cerr << "Usage: " << argv[0] << " <vocab> <orb.yaml> <cam.yaml> [traj.txt]\n";
        return 1;
    }
    std::string vp=argv[1], oc=argv[2], cc=argv[3], tf=(argc>=5)?argv[4]:"";

    signal(SIGINT, sigint_handler); signal(SIGTERM, sigint_handler);
    atexit(do_cleanup);

    // 预计算矫正映射
    cv::Mat K1 = (cv::Mat_<double>(3,3)<<231.513824,0,325.251587,0,231.521408,193.431366,0,0,1);
    cv::Mat D1 = (cv::Mat_<double>(1,4)<<0.054344,0.020572,-0.002152,-0.004231);
    cv::Mat K2 = (cv::Mat_<double>(3,3)<<230.590,0,319.743,0,230.699,188.880,0,0,1);
    cv::Mat D2 = (cv::Mat_<double>(1,4)<<0.0381711,0.0786609,-0.0846951,0.0343996);
    cv::Mat R_ = (cv::Mat_<double>(3,3)<<0.999881,0.001432,0.015391,-0.001406,0.999998,-0.001685,-0.015393,0.001663,0.999880);
    cv::Mat T_ = (cv::Mat_<double>(3,1)<<-0.080087,0.000009,0.000599);
    cv::Size sz(640,400);
    cv::Mat R1,R2,P1,P2,Q;
    cv::fisheye::stereoRectify(K1,D1,K2,D2,sz,R_,T_,R1,R2,P1,P2,Q,cv::CALIB_ZERO_DISPARITY);
    std::cout << "[RECTIFY] P1 fx=" << P1.at<double>(0,0) << " fy=" << P1.at<double>(1,1)
              << " cx=" << P1.at<double>(0,2) << " cy=" << P1.at<double>(1,2) << std::endl;
    cv::fisheye::initUndistortRectifyMap(K1,D1,R1,P1,sz,CV_32FC1,g_map1x,g_map1y);
    cv::fisheye::initUndistortRectifyMap(K2,D2,R2,P2,sz,CV_32FC1,g_map2x,g_map2y);

    // ORB-SLAM3
    ORB_SLAM3::System SLAM(vp, oc, ORB_SLAM3::System::IMU_STEREO, true, 0, tf);
    g_pSLAM = &SLAM;

    // 打开相机
    void* h = nullptr;
    if (FAYS_VIK_CreateHandleWithConfig(&h, cc.c_str()) != EXIT_SUCCESS) {
        std::cerr << "[ERR] Camera open failed\n"; return 1;
    }
    g_handle = h;
    PrintDeviceInfo(h);
    std::cout << "SDK:" << FAYS_VIK_GetVersion(h) << "\n";

    // 非阻塞 stdin (Enter 检测)
    int flags = fcntl(STDIN_FILENO, F_GETFL, 0);
    fcntl(STDIN_FILENO, F_SETFL, flags | O_NONBLOCK);

    // 注册回调
    FAYS_VIK_RegisterStereoImageCallback(h, stereoCallback);
    FAYS_VIK_RegisterImuCallback(h, imuCallback);

    // Enter-to-start
    bool bReady=false, bOriginSet=false;
    double t_first_valid=-1, ts_origin=0;
    Sophus::SE3f Twc_ref;
    std::cout << "\n[INIT] Waiting for IMU initialization...\n"
              << "       Move camera for 2s, then press ENTER.\n\n";

    std::ofstream fout; if(!tf.empty()){fout.open(tf);fout<<std::fixed;}
    std::string tf_raw = tf.empty() ? "" : tf.substr(0,tf.size()-4)+"_raw.txt";
    std::ofstream fout_raw; if(!tf_raw.empty()){fout_raw.open(tf_raw);fout_raw<<std::fixed;}
    std::string tf_imu = tf.empty() ? "" : tf.substr(0,tf.size()-4)+"_imu.txt";
    if(!tf_imu.empty()){fout_imu.open(tf_imu);fout_imu<<std::fixed;}
    std::string tf_img = tf.empty() ? "" : tf.substr(0,tf.size()-4)+"_img";
    if(!tf_img.empty()){g_img_save_dir=tf_img; system(("mkdir -p "+tf_img).c_str());}
    std::string tf_pipe = tf.empty() ? "" : tf.substr(0,tf.size()-4)+"_pipe.txt";
    std::ofstream fout_pipe; if(!tf_pipe.empty()){
        fout_pipe.open(tf_pipe); fout_pipe<<std::fixed;
        fout_pipe << "# frame dt_frame_ms track_ms imu_backlog imu_in" << std::endl;
        fout_pipe << "# dt_frame=相邻帧时间间隔(ms)  track=Track耗时(ms)  imu_backlog=IMU积压数  imu_in=帧间IMU条数" << std::endl;
    }
    std::cout << "[FILE] traj=" << tf << "\n[FILE] raw=" << tf_raw << "\n[FILE] pipe=" << tf_pipe << "\n";
    // 等第一帧到达后打印图像信息 (在 stereoCallback 里记录)
    { int wait=0; while(g_first_img && wait<300) { std::this_thread::sleep_for(std::chrono::milliseconds(10)); wait++; }
      std::cout << "[IMAGE] SDK callback: " << g_img_w << "x" << g_img_h << " ch=" << g_img_ch
                << " (left=" << g_img_w << "x" << (g_img_h/2) << " right=" << g_img_w << "x" << (g_img_h/2) << ")\n"; }
    auto t0 = std::chrono::steady_clock::now();
    int frame_n=0;

    while (!SLAM.isShutDown() && g_running) {
        // 检查新图像
        bool has_img = false;
        double ts; cv::Mat L, R;
        {
            std::lock_guard<std::mutex> lk(g_img_mutex);
            if (g_img_ready) {
                ts = g_img_ts; L = g_imgL; R = g_imgR;
                g_img_ready = false; has_img = true;
            }
        }
        if (!has_img) { std::this_thread::sleep_for(std::chrono::milliseconds(2)); continue; }

        // 取帧间 IMU（交换 + 清空）
        std::vector<ORB_SLAM3::IMU::Point> vImu;
        {
            std::lock_guard<std::mutex> lk(g_imu_mutex);
            vImu.swap(g_imu_buf);
        }

        // 管线诊断: 帧间隔 + IMU 积压 + Track 耗时 (100帧滑动平均)
        static double ts_prev_frame = 0;
        static double acc_dt = 0, acc_track = 0, acc_backlog = 0, acc_imu = 0;
        static int diag_count = 0;
        diag_count++;
        double dt_frame = (ts_prev_frame > 0) ? (ts - ts_prev_frame) : 0;
        ts_prev_frame = ts;
        int imu_backlog = 0;
        {
            std::lock_guard<std::mutex> lk(g_imu_mutex);
            imu_backlog = (int)g_imu_buf.size();
        }
        auto t_track_start = std::chrono::steady_clock::now();

        // Track
        Sophus::SE3f Tcw = SLAM.TrackStereo(L, R, ts, vImu);
        auto t_track_end = std::chrono::steady_clock::now();
        double track_ms = std::chrono::duration<double,std::milli>(t_track_end - t_track_start).count();

        acc_dt += dt_frame; acc_track += track_ms; acc_backlog += imu_backlog; acc_imu += vImu.size();
        if (diag_count % 100 == 0) {
            std::cout << "[PIPE] frame=" << diag_count
                      << "  dt_frame=" << acc_dt/100*1000 << "ms"
                      << "  track=" << acc_track/100 << "ms"
                      << "  imu_backlog=" << acc_backlog/100
                      << "  imu_in=" << acc_imu/100 << std::endl;
            if (fout_pipe.is_open()) {
                fout_pipe << std::setprecision(3)
                          << diag_count << " "
                          << acc_dt/100*1000 << " "
                          << acc_track/100 << " "
                          << acc_backlog/100 << " "
                          << acc_imu/100 << std::endl;
            }
            acc_dt = acc_track = acc_backlog = acc_imu = 0;
        }
        Sophus::SE3f Twc = Tcw.inverse();
        Eigen::Vector3f p_raw = Twc.translation();  // 修正前的原始位姿

        // IMU init + Enter-to-start
        if (!bReady && Twc.translation().norm() > 0.01) {
            if (t_first_valid<0) { t_first_valid=ts;
                std::cout << "[IMU] Init (2s)..." << std::endl; }
            else if (ts-t_first_valid>2.0) { bReady=true; Twc_ref=Twc;
                std::cout << "\n=== READY. Press ENTER to set origin. ===\n\n"; }
        }
        // Non-block stdin
        char c;
        while (read(STDIN_FILENO, &c, 1) > 0 && (c=='\n'||c=='\r')) {
            if (bReady && !bOriginSet) { Twc_ref=Twc; bOriginSet=true; ts_origin=ts;
                g_save_img=true; g_img_frame=0;
                std::cout << ">>> ORIGIN SET <<<\n"; }
            else if (bOriginSet) { Twc_ref=Twc; ts_origin=ts;
                std::cout << ">>> NEW ORIGIN <<<\n"; }
        }

        if (bOriginSet) {
            Sophus::SE3f Twc_out = Twc_ref.inverse() * Twc;
            Eigen::Vector3f p_raw = Twc_out.translation();
            Eigen::Quaternionf q_raw(Twc_out.rotationMatrix());

            // S80M 坐标系修正: R_z(-90°) → R_y(+90°)
            Eigen::Matrix3f R_corr =
                Eigen::AngleAxisf( M_PI/2 + 4.5 *M_PI/180.0 , Eigen::Vector3f::UnitY()).toRotationMatrix()
              * Eigen::AngleAxisf(-M_PI/2, Eigen::Vector3f::UnitZ()).toRotationMatrix();
            Eigen::Vector3f p = R_corr * p_raw;
            Eigen::Quaternionf q = Eigen::Quaternionf(R_corr) * q_raw;

            // 检测位置突变 (>0.5m 或 旋转>30°), 跳变帧不写入
            static Eigen::Vector3f p_prev_clean = p;
            static Eigen::Quaternionf q_prev_clean = q;
            static bool has_prev = false;
            bool skip_frame = false;
            if (has_prev) {
                float dp = (p-p_prev_clean).norm();
                float dq_deg = 2.0f*std::acos(std::min(1.0f,std::abs(q_prev_clean.dot(q))))*180.0f/M_PI;
                if (dp > 0.5f || dq_deg > 30.0f) {
                    skip_frame = true;
                }
            }
            if (!skip_frame) { p_prev_clean=p; q_prev_clean=q; has_prev=true; }

            // 跳变帧: 沿用上一帧位姿,保持连续
            if (skip_frame) {
                std::cout<<"[HOLD] frame at t="<<(ts-ts_origin)<<"s jump, holding prev pose\n";
                p=p_prev_clean; q=q_prev_clean;
            }
            // 保存 raw (修正前) 轨迹
            if (fout_raw.is_open())
                fout_raw<<std::setprecision(6)<<(ts-ts_origin)<<" "<<std::setprecision(9)
                        <<p_raw.x()<<" "<<p_raw.y()<<" "<<p_raw.z()<<" "
                        <<q_raw.x()<<" "<<q_raw.y()<<" "<<q_raw.z()<<" "<<q_raw.w()<<"\n";

            // --- 保存带特征点的图 (每5帧一次, 省空间) ---
            if (g_save_img && (g_img_frame % 10 == 0)) {
                cv::Mat img_l,img_r,side;
                cv::cvtColor(L,img_l,cv::COLOR_GRAY2BGR);
                cv::cvtColor(R,img_r,cv::COLOR_GRAY2BGR);
                cv::hconcat(img_l,img_r,side);
                std::ostringstream ts_text; ts_text<<std::fixed<<std::setprecision(2)<<(ts-ts_origin)<<"s";
                cv::putText(side,ts_text.str(),cv::Point(10,30),cv::FONT_HERSHEY_SIMPLEX,0.6,cv::Scalar(0,255,255),2);
                std::ostringstream fn;
                fn<<g_img_save_dir<<"/"<<std::setfill('0')<<std::setw(6)<<g_img_frame
                  <<"_"<<std::fixed<<std::setprecision(3)<<(ts-ts_origin)<<"s.jpg";
                cv::imwrite(fn.str(),side);
            }
            g_img_frame++;

            // --- 累积帧间旋转, 每 10 帧打印一次 ---
            static Eigen::Quaternionf q_prev = q;
            static Eigen::Quaternionf dq_acc = Eigen::Quaternionf::Identity();
            static int dq_count = 0;
            Eigen::Quaternionf dq = q_prev.conjugate() * q;
            q_prev = q;
            dq_acc = dq * dq_acc;
            dq_count++;

            if (dq_count >= 10) {
                float w_clamped = dq_acc.w() < -1.0f ? -1.0f : (dq_acc.w() > 1.0f ? 1.0f : dq_acc.w());
                float dq_angle_deg = 2.0f * std::acos(w_clamped) * 180.0f / M_PI;
                Eigen::Vector3f dq_axis(dq_acc.x(), dq_acc.y(), dq_acc.z());
                float n = dq_axis.norm();
                if (n > 1e-9f) dq_axis /= n; else dq_axis = Eigen::Vector3f(0,0,1);
                dq_axis = R_corr * dq_axis;

                // 检测 VIBA 导致的累积旋转跳变
                if (dq_angle_deg > 5.0f)
                    std::cout << "[DEBUG-VIBA] Δq(" << dq_count << "f) jump: "
                              << std::setprecision(2) << dq_angle_deg << "° "
                              << "axis=(" << std::setprecision(3)
                              << dq_axis.x() << "," << dq_axis.y() << "," << dq_axis.z() << ")\n";

                std::cout<<std::fixed<<std::setprecision(4)
                         <<"["<<(ts-ts_origin)<<"] XYZ:("<<p.x()<<","<<p.y()<<","<<p.z()
                         <<") Quat:(w="<<q.w()<<",x="<<q.x()<<",y="<<q.y()<<",z="<<q.z()<<")\n"
                         <<"       Δq("<<dq_count<<"f):"<<std::setprecision(2)<<dq_angle_deg<<"° "
                         <<"axis=("<<std::setprecision(3)<<dq_axis.x()<<","<<dq_axis.y()<<","<<dq_axis.z()<<")\n";
                dq_acc = Eigen::Quaternionf::Identity();
                dq_count = 0;
            } else {
                std::cout<<std::fixed<<std::setprecision(4)
                         <<"["<<(ts-ts_origin)<<"] XYZ:("<<p.x()<<","<<p.y()<<","<<p.z()
                         <<") Quat:(w="<<q.w()<<",x="<<q.x()<<",y="<<q.y()<<",z="<<q.z()<<")\n";
            }

            // if (fout.is_open()) fout<<std::setprecision(6)<<(ts-ts_origin)<<" "<<std::setprecision(9)
            //     <<p.x()<<" "<<p.y()<<" "<<p.z()<<" "<<q.x()<<" "<<q.y()<<" "<<q.z()<<" "<<q.w()<<"\n";
            if (fout.is_open()) {
                double wall_now = std::chrono::duration<double>(
                    std::chrono::steady_clock::now().time_since_epoch()).count();
                fout<<std::setprecision(6)<<(ts-ts_origin)<<" "<<std::setprecision(9)
                    <<p.x()<<" "<<p.y()<<" "<<p.z()<<" "<<q.x()<<" "<<q.y()<<" "<<q.z()<<" "<<q.w()
                    <<" "<<std::setprecision(6)<<wall_now<<"\n";
            }
        }

        auto now=std::chrono::steady_clock::now();
        double el=std::chrono::duration<double>(now-t0).count();
        if (++frame_n%100==0)
            std::cerr<<"[STAT] frames="<<frame_n<<" fps="<<std::fixed<<std::setprecision(1)<<(frame_n/el)<<"\n";
    }

    g_running=false; SLAM.Shutdown();
    do_cleanup();
    if (fout.is_open()) fout.close();
    if (fout_pipe.is_open()) fout_pipe.close();
    std::cout<<"[SLAM] Done.\n"; return 0;
}
