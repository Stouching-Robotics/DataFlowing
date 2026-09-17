/**
 * @file fayssense_orb_slam_april.cc
 * @brief S80M → ORB-SLAM3 双目惯导 + AprilTag 重定位修正
 *
 * 基于 fayssense_orb_slam.cc, 新增:
 *   - AprilTag (ArUco DICT_6X6_250) 检测
 *   - PnP 求解 tag→相机绝对位姿
 *   - SE3 指数平滑修正 ORB-SLAM3 输出
 *
 * 用法同 fayssense_orb_slam.cc:
 *   ./fayssense_orb_slam_april <vocab> <orb.yaml> <cam.yaml> [traj.txt]
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
#include <cmath>
#include <cstring>
#include <map>

#include <opencv2/core.hpp>
#include <opencv2/highgui.hpp>
#include <opencv2/imgproc.hpp>
#include <opencv2/calib3d.hpp>
#include <opencv2/aruco.hpp>
#include <fcntl.h>

#include "fays_atrak/fays_atrak_types.h"
#include "fays_atrak/fays_vikit.h"
#include "common/print_helpers.h"

#include <System.h>
#include <ImuTypes.h>

// ============================================================================
// AprilTag 配置 — 修改这里 !
// ============================================================================
// 每个 tag 需要定义:
//   id    : ArUco marker ID (0~249 for DICT_6X6_250)
//   size  : tag 边长, 单位 米 (打印的实际物理尺寸!)
//   pos_x, pos_y, pos_z           : tag 中心在世界系中的位置 (米)
//   angle_deg, axis_x, axis_y, axis_z : tag 法线方向 (角度+轴)
//
//   "angle_deg=0, axis=(0,0,1)"  = tag 面朝上 (贴在水平桌面)
//   "angle_deg=90, axis=(1,0,0)" = tag 面朝前 (贴在墙上, 绕X轴转90°)
//
//   tag 坐标系定义 (OpenCV ArUco 标准):
//     tag 中心=原点, tag 平面=XY, Z 轴垂直 tag 面朝外 (指向相机方向)
//
//   【重要】部署 tag 后, 用卷尺/激光测距精确测量位置填入下面 !
// ============================================================================
struct TagConfig { int id; float size; float px, py, pz; float ang_deg, ax, ay, az; };

static const std::vector<TagConfig> TAG_CFG = {
    // ---- 示例: 3个tag放在操作台上 (请改成你自己的实际测量值!) ----
    { .id=0, .size=0.05f, .px= 0.00f, .py= 0.00f, .pz=0.00f, .ang_deg=0,  .ax=0,.ay=0,.az=1 }, // 桌面原点, 朝上
    { .id=1, .size=0.05f, .px= 0.50f, .py= 0.00f, .pz=0.00f, .ang_deg=0,  .ax=0,.ay=0,.az=1 }, // 桌面 X+50cm, 朝上
    { .id=2, .size=0.05f, .px= 0.00f, .py= 0.50f, .pz=0.00f, .ang_deg=0,  .ax=0,.ay=0,.az=1 }, // 桌面 Y+50cm, 朝上
};

// ============================================================================
// AprilTag 修正参数
// ============================================================================
static const float  APRIL_ALPHA       = 0.08f;   // 平滑系数 (0.05=很慢, 0.15=较快)
static const int    APRIL_MIN_FRAMES  = 3;        // 连续检测多少帧才信任
static const float  APRIL_MAX_CORR_POS = 0.10f;   // 单次位置修正上限 (米), 防误检跳变
static const float  APRIL_MAX_CORR_ANG = 10.0f;   // 单次角度修正上限 (度), 防误检跳变

// ============================================================================
// 全局状态 (同原版)
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

static std::mutex g_imu_mutex;
static std::vector<ORB_SLAM3::IMU::Point> g_imu_buf;
static std::mutex g_img_mutex;
static bool g_img_ready = false;
static double g_img_ts = 0;
static cv::Mat g_imgL, g_imgR;
static cv::Mat g_map1x, g_map1y, g_map2x, g_map2y;

void imuCallback(const AtrakIMU& imu) {
    std::lock_guard<std::mutex> lk(g_imu_mutex);
    double t = imu.timestamp * 1e-9;  // SDK 已对齐
    if (!std::isfinite(imu.acc[0]) || !std::isfinite(imu.gyro[0])) return;
    g_imu_buf.push_back(ORB_SLAM3::IMU::Point(
        (float)imu.acc[0], (float)imu.acc[1], (float)imu.acc[2],
        (float)imu.gyro[0], (float)imu.gyro[1], (float)imu.gyro[2], t));
}

void stereoCallback(AtrakImage* img) {
    if (!img || !img->data) return;
    int half_h = img->height / 2;
    cv::Mat stitched(img->height, img->width, CV_8UC1, img->data);
    cv::Mat left  = stitched(cv::Rect(0, 0, img->width, half_h)).clone();
    cv::Mat right = stitched(cv::Rect(0, half_h, img->width, half_h)).clone();
    cv::Mat Lr, Rr;
    cv::remap(left,  Lr, g_map1x, g_map1y, cv::INTER_LINEAR);
    cv::remap(right, Rr, g_map2x, g_map2y, cv::INTER_LINEAR);
    std::lock_guard<std::mutex> lk(g_img_mutex);
    g_img_ts = img->timestamp * 1e-9;
    g_imgL = Lr; g_imgR = Rr; g_img_ready = true;
}

// ============================================================================
// AprilTag 位姿求解
// ============================================================================
// 预计算 tag 的世界位姿矩阵
struct TagWorldPose { Eigen::Matrix3f R; Eigen::Vector3f t; float size; };
static std::map<int, TagWorldPose> g_tag_map;

static void buildTagMap() {
    for (const auto& cfg : TAG_CFG) {
        Eigen::AngleAxisf aa(cfg.ang_deg * M_PI / 180.0f,
                              Eigen::Vector3f(cfg.ax, cfg.ay, cfg.az).normalized());
        TagWorldPose twp;
        twp.R = aa.toRotationMatrix();
        twp.t = Eigen::Vector3f(cfg.px, cfg.py, cfg.pz);
        twp.size = cfg.size;
        g_tag_map[cfg.id] = twp;
    }
}

// PnP 内参 (矫正后 PinHole, 无畸变)
static cv::Mat g_K_pnp = (cv::Mat_<double>(3,3) <<
    182.542, 0,       329.164,
    0,       182.542, 185.844,
    0,       0,       1);
static cv::Mat g_D_pnp = cv::Mat::zeros(4,1,CV_64F);  // 矫正后无畸变

/**
 * 检测左图中的 AprilTag, 返回相机在世界系中的绝对位姿
 * @return true 如果至少检测到1个已知tag
 */
static bool detectAprilPose(const cv::Mat& gray,
                             std::vector<Sophus::SE3f>& out_poses,
                             cv::Mat* debug_img = nullptr)
{
    static cv::Ptr<cv::aruco::Dictionary> s_dict =
        cv::aruco::getPredefinedDictionary(cv::aruco::DICT_6X6_250);

    std::vector<int> ids;
    std::vector<std::vector<cv::Point2f>> corners;
    cv::aruco::detectMarkers(gray, s_dict, corners, ids);

    if (ids.empty()) return false;

    // 筛选已知 tag 并提取角点
    std::vector<int> valid_ids;
    std::vector<std::vector<cv::Point2f>> valid_corners;
    std::vector<float> valid_sizes;
    for (size_t i = 0; i < ids.size(); i++) {
        auto it = g_tag_map.find(ids[i]);
        if (it != g_tag_map.end()) {
            valid_ids.push_back(ids[i]);
            valid_corners.push_back(corners[i]);
            valid_sizes.push_back(it->second.size);
        }
    }
    if (valid_ids.empty()) return false;

    // 逐 tag 做 PnP
    out_poses.clear();
    for (size_t i = 0; i < valid_ids.size(); i++) {
        // 3D 角点: tag 坐标系, 4 个角 [(-s/2,-s/2,0), (s/2,-s/2,0), (s/2,s/2,0), (-s/2,s/2,0)]
        float hs = valid_sizes[i] * 0.5f;
        std::vector<cv::Point3f> obj_pts = {
            {-hs, -hs, 0}, { hs, -hs, 0}, { hs,  hs, 0}, {-hs,  hs, 0}
        };
        // ArUco detectMarkers 返回的角点顺序: top-left, top-right, bottom-right, bottom-left
        // PnP
        cv::Mat rvec, tvec;
        bool ok = cv::solvePnP(obj_pts, valid_corners[i], g_K_pnp, g_D_pnp, rvec, tvec,
                                false, cv::SOLVEPNP_IPPE_SQUARE);
        if (!ok) continue;

        cv::Mat Rmat;
        cv::Rodrigues(rvec, Rmat);
        // T_cam_tag: tag → camera
        Eigen::Matrix3f R_ct;
        R_ct << Rmat.at<double>(0,0), Rmat.at<double>(0,1), Rmat.at<double>(0,2),
                Rmat.at<double>(1,0), Rmat.at<double>(1,1), Rmat.at<double>(1,2),
                Rmat.at<double>(2,0), Rmat.at<double>(2,1), Rmat.at<double>(2,2);
        Eigen::Vector3f t_ct(tvec.at<double>(0), tvec.at<double>(1), tvec.at<double>(2));
        Sophus::SE3f T_cam_tag(R_ct, t_ct);

        // T_world_cam = T_world_tag * T_cam_tag^(-1)
        const auto& twp = g_tag_map[valid_ids[i]];
        Sophus::SE3f T_world_tag(twp.R, twp.t);
        Sophus::SE3f T_world_cam = T_world_tag * T_cam_tag.inverse();

        out_poses.push_back(T_world_cam);
    }

    // debug 绘制
    if (debug_img) {
        cv::aruco::drawDetectedMarkers(*debug_img, valid_corners, valid_ids);
    }

    return !out_poses.empty();
}

/**
 * 对多个位姿取平均
 */
static Sophus::SE3f averagePoses(const std::vector<Sophus::SE3f>& poses) {
    if (poses.empty()) return Sophus::SE3f();
    // 位置: 直接平均
    Eigen::Vector3f avg_t = Eigen::Vector3f::Zero();
    for (const auto& p : poses) avg_t += p.translation();
    avg_t /= (float)poses.size();
    // 旋转: 四元数平均
    Eigen::Quaternionf avg_q = poses[0].unit_quaternion();
    for (size_t i = 1; i < poses.size(); i++) {
        Eigen::Quaternionf qi = poses[i].unit_quaternion();
        if (avg_q.dot(qi) < 0) qi.coeffs() = -qi.coeffs();
        float alpha = 1.0f / (float)(i + 1);
        avg_q = avg_q.slerp(alpha, qi);
    }
    return Sophus::SE3f(avg_q, avg_t);
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

    // AprilTag 初始化
    buildTagMap();
    std::cout << "[AprilTag] Loaded " << g_tag_map.size() << " tag(s)\n";
    for (auto& kv : g_tag_map) {
        std::cout << "  ID=" << kv.first << " size=" << kv.second.size
                  << "m pos=(" << kv.second.t.transpose() << ")\n";
    }

    // 矫正映射 (同原版)
    cv::Mat K1 = (cv::Mat_<double>(3,3)<<231.513824,0,325.251587,0,231.521408,193.431366,0,0,1);
    cv::Mat D1 = (cv::Mat_<double>(1,4)<<0.054344,0.020572,-0.002152,-0.004231);
    cv::Mat K2 = (cv::Mat_<double>(3,3)<<230.590,0,319.743,0,230.699,188.880,0,0,1);
    cv::Mat D2 = (cv::Mat_<double>(1,4)<<0.0381711,0.0786609,-0.0846951,0.0343996);
    cv::Mat R_ = (cv::Mat_<double>(3,3)<<0.999881,0.001432,0.015391,-0.001406,0.999998,-0.001685,-0.015393,0.001663,0.999880);
    cv::Mat T_ = (cv::Mat_<double>(3,1)<<-0.080087,0.000009,0.000599);
    cv::Size sz(640,400);
    cv::Mat R1,R2,P1,P2,Q;
    cv::fisheye::stereoRectify(K1,D1,K2,D2,sz,R_,T_,R1,R2,P1,P2,Q,cv::CALIB_ZERO_DISPARITY);
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

    int flags = fcntl(STDIN_FILENO, F_GETFL, 0);
    fcntl(STDIN_FILENO, F_SETFL, flags | O_NONBLOCK);

    FAYS_VIK_RegisterStereoImageCallback(h, stereoCallback);
    FAYS_VIK_RegisterImuCallback(h, imuCallback);

    bool bReady=false, bOriginSet=false;
    double t_first_valid=-1, ts_origin=0;
    Sophus::SE3f Twc_ref;
    std::cout << "\n[INIT] Waiting for IMU initialization...\n"
              << "       Move camera for 2s, then press ENTER.\n\n";

    std::ofstream fout; if(!tf.empty()){fout.open(tf);fout<<std::fixed;}
    auto t0 = std::chrono::steady_clock::now();
    int frame_n=0;

    // ---- AprilTag 修正状态 ----
    Sophus::SE3f T_correction;             // 累积修正 (初始化为单位矩阵)
    int april_consecutive = 0;              // 连续检测帧数
    float april_confidence = 0.0f;          // 修正置信度 [0,1]
    Sophus::SE3f T_world_cam_april_last;   // 上一次检测到的 tag 位姿

    while (!SLAM.isShutDown() && g_running) {
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

        std::vector<ORB_SLAM3::IMU::Point> vImu;
        {
            std::lock_guard<std::mutex> lk(g_imu_mutex);
            vImu.swap(g_imu_buf);
        }

        // ---- AprilTag 检测 (左目矫正图) ----
        std::vector<Sophus::SE3f> april_poses;
        bool tag_detected = detectAprilPose(L, april_poses);
        Sophus::SE3f T_world_cam_april;
        bool have_april = false;
        if (tag_detected) {
            T_world_cam_april = averagePoses(april_poses);
            have_april = true;
            april_consecutive++;
        } else {
            april_consecutive = 0;
        }

        // ---- ORB-SLAM3 Track ----
        Sophus::SE3f Tcw = SLAM.TrackStereo(L, R, ts, vImu);
        Sophus::SE3f Twc = Tcw.inverse();

        // ---- AprilTag 修正计算 ----
        if (have_april && april_consecutive >= APRIL_MIN_FRAMES) {
            // 计算本次修正量
            Sophus::SE3f T_corr_new = T_world_cam_april * Twc;  // T_w_c_april * T_c_w = correction from ORB to april

            // 检查修正量是否过大 (误检过滤)
            float d_pos = T_corr_new.translation().norm();
            Eigen::AngleAxisf d_aa(T_corr_new.rotationMatrix());
            float d_ang = std::abs(d_aa.angle()) * 180.0f / M_PI;

            if (d_pos < APRIL_MAX_CORR_POS && d_ang < APRIL_MAX_CORR_ANG) {
                // SE3 指数平滑
                // delta = T_corr_new * T_correction^(-1) → 本次需要额外修正的部分
                Sophus::SE3f delta = T_corr_new * T_correction.inverse();

                float alpha = std::min(APRIL_ALPHA * (1.0f + april_confidence), 0.3f);

                // 分离旋转和平移做平滑
                Eigen::Vector3f d_t = delta.translation();
                Eigen::Quaternionf d_q(delta.rotationMatrix());
                Eigen::Quaternionf I_q = Eigen::Quaternionf::Identity();

                // slerp 旋转, lerp 平移
                Eigen::Quaternionf s_q = I_q.slerp(alpha, d_q);
                Eigen::Vector3f s_t = alpha * d_t;
                Sophus::SE3f delta_smooth(s_q, s_t);

                T_correction = delta_smooth * T_correction;

                // 重新归一化旋转
                Eigen::Quaternionf cq(T_correction.rotationMatrix());
                cq.normalize();
                T_correction = Sophus::SE3f(cq, T_correction.translation());

                april_confidence = std::min(1.0f, april_confidence + 0.02f);

                static int corr_log = 0;
                if (++corr_log % 10 == 0) {
                    std::cout << "[April] corr: pos=" << std::fixed << std::setprecision(3)
                              << T_correction.translation().norm()*100.0f << "cm"
                              << " ang=" << std::setprecision(1)
                              << Eigen::AngleAxisf(T_correction.rotationMatrix()).angle()*180.0/M_PI
                              << "° conf=" << april_confidence
                              << " det=" << april_poses.size() << "tags\n";
                }
            } else {
                static int reject_log = 0;
                if (++reject_log % 30 == 0)
                    std::cout << "[April] rejected large correction: dpos=" << d_pos
                              << "m dang=" << d_ang << "°\n";
            }
        } else if (!have_april) {
            // 没检测到 tag → 保持修正, 置信度缓慢衰减
            april_confidence = std::max(0.0f, april_confidence - 0.005f);
        }

        // ---- 应用修正到输出 ----
        Sophus::SE3f Twc_corrected = T_correction * Twc;

        // ---- Enter-to-start (使用修正后的位姿) ----
        if (!bReady && Twc_corrected.translation().norm() > 0.01) {
            if (t_first_valid<0) { t_first_valid=ts;
                std::cout << "[IMU] Init (2s)..." << std::endl; }
            else if (ts-t_first_valid>2.0) { bReady=true; Twc_ref=Twc_corrected;
                std::cout << "\n=== READY. Press ENTER to set origin. ===\n\n"; }
        }
        char c;
        while (read(STDIN_FILENO, &c, 1) > 0 && (c=='\n'||c=='\r')) {
            if (bReady && !bOriginSet) { Twc_ref=Twc_corrected; bOriginSet=true; ts_origin=ts;
                std::cout << ">>> ORIGIN SET <<<\n"; }
            else if (bOriginSet) { Twc_ref=Twc_corrected; ts_origin=ts;
                std::cout << ">>> NEW ORIGIN <<<\n"; }
        }

        if (bOriginSet) {
            Sophus::SE3f Twc_out = Twc_ref.inverse() * Twc_corrected;
            Eigen::Vector3f p = Twc_out.translation();
            Eigen::Quaternionf q(Twc_out.rotationMatrix());

            Eigen::Matrix3f R_corr =
                Eigen::AngleAxisf( M_PI/2, Eigen::Vector3f::UnitY()).toRotationMatrix()
              * Eigen::AngleAxisf(-M_PI/2, Eigen::Vector3f::UnitZ()).toRotationMatrix();
            p = R_corr * p;
            q = Eigen::Quaternionf(R_corr) * q;

            // 简洁输出 (每帧)
            if (april_confidence > 0.01f) {
                std::cout << std::fixed << std::setprecision(4)
                          << "[" << (ts-ts_origin) << "][A] XYZ:("
                          << p.x() << "," << p.y() << "," << p.z()
                          << ") Quat:(w=" << q.w() << ",x=" << q.x()
                          << ",y=" << q.y() << ",z=" << q.z() << ")\n";
            } else {
                std::cout << std::fixed << std::setprecision(4)
                          << "[" << (ts-ts_origin) << "] XYZ:("
                          << p.x() << "," << p.y() << "," << p.z()
                          << ") Quat:(w=" << q.w() << ",x=" << q.x()
                          << ",y=" << q.y() << ",z=" << q.z() << ")\n";
            }

            if (fout.is_open()) fout<<std::setprecision(6)<<(ts-ts_origin)<<" "<<std::setprecision(9)
                <<p.x()<<" "<<p.y()<<" "<<p.z()<<" "<<q.x()<<" "<<q.y()<<" "<<q.z()<<" "<<q.w()<<"\n";
        }

        auto now=std::chrono::steady_clock::now();
        double el=std::chrono::duration<double>(now-t0).count();
        if (++frame_n%100==0)
            std::cerr<<"[STAT] frames="<<frame_n<<" fps="<<std::fixed<<std::setprecision(1)<<(frame_n/el)<<"\n";
    }

    g_running=false; SLAM.Shutdown();
    do_cleanup();
    if (fout.is_open()) fout.close();
    std::cout<<"[SLAM] Done.\n"; return 0;
}
