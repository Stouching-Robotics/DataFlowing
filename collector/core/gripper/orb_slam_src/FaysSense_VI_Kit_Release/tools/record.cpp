#include <string>
#include <thread>
#include <memory>
#include <iostream>
#include <fstream>
#include <iomanip>
#include <chrono>
#include <sstream>
#include <filesystem>
#include <opencv2/opencv.hpp>
#include <atomic>
#include <condition_variable>
#include <queue>
#include <mutex>
#include "fays_atrak/fays_atrak_types.h"
#include "fays_atrak/fays_vikit.h"

namespace fs = std::filesystem;

class FaysExample {
public:
    FaysExample(const char* configPath,const double hz)
        : mptrHandle_{nullptr}, mbIsRunning_{true},
      mTotalLeftFrames_{0}, mTotalRightFrames_{0}, mTotalImuSamples_{0},
      mLastLeftTime_{0}, mLastRightTime_{0}, mLeftDropCount_{0}, mRightDropCount_{0},
      mFirstLeftTime_{0}, mFirstRightTime_{0}, mFirstImuTime_{0}, mLastImuTime_{0},
      mImuDropCount_{0},hz_(hz)
    {
        // 初始化数据缓冲区
        mImgData_.data = new uchar[FAYS_ATRAK_MONO_MAX_BYTES * 3];
        mRgbData_.data = new uchar[FAYS_ATRAK_RGB_MAX_BYTES];
        // 创建数据目录
        CreateDataDirectory();
        
        // 打开IMU CSV文件
        mImuCsvFile_.open(mImuCsvPath_, std::ios::out);
        if (mImuCsvFile_.is_open()) {
            mImuCsvFile_ << "#timestamp,omega_x,omega_y,omega_z,alpha_x,alpha_y,alpha_z\n";
            mImuCsvFile_.flush();
        }
        
        FAYS_VIK_CreateHandleWithConfig(&mptrHandle_, configPath);
        
        std::cout << "Data will be saved to: " << mDataDir_ << std::endl;
        std::cout << "Press 'q' or ESC to exit." << std::endl;

        mptrImuProducerThr_ = std::thread(&FaysExample::ImuProducer, this);
        mptrImuConsumerThr_ = std::thread(&FaysExample::ImuConsumer, this);
        mptrKeyThr_ = std::thread(&FaysExample::KeyMonitor, this);
        mptrImgProducerThr_ = std::thread(&FaysExample::ImgProducer, this);   // 新增图像生产者
        mptrImgConsumerThr_ = std::thread(&FaysExample::ImgConsumer, this);   // 新增图像消费者
    }

    ~FaysExample() {
        mbIsRunning_ = false;
        mBufferCond_.notify_all();
        mImgBufferCond_.notify_all();
        
        // 回收所有线程
        if (mptrImgProducerThr_.joinable()) mptrImgProducerThr_.join();
        if (mptrImgConsumerThr_.joinable()) mptrImgConsumerThr_.join();
        if (mptrImuProducerThr_.joinable()) mptrImuProducerThr_.join();
        if (mptrImuConsumerThr_.joinable()) mptrImuConsumerThr_.join();
        if (mptrKeyThr_.joinable()) mptrKeyThr_.join();
            
        // 清理不完整数据
        CleanIncompleteData();
        
        // 关闭文件
        if (mImuCsvFile_.is_open()) {
            mImuCsvFile_.close();
        }

        FAYS_VIK_DestroyHandle(mptrHandle_);
        if ( mImgData_.data ) {
            delete[] mImgData_.data;
            mImgData_.data = nullptr;
        }
        if ( mRgbData_.data ) {
            delete[] mRgbData_.data;
            mRgbData_.data = nullptr;
        }
        // 打印统计信息
        PrintStatistics();
    }

    bool IsRunning() const { return mbIsRunning_; }

    // 获取最新的图像（主线程调用）
    bool GetLatestImage(cv::Mat& out) {
        std::lock_guard<std::mutex> lock(mImgMutex_);
        if (!mLatestImg_.empty()) {
            mLatestImg_.copyTo(out);
            return true;
        }
        return false;
    }

private:
    void CreateDataDirectory() {
        // 使用当前时间戳创建目录
        auto now = std::chrono::system_clock::now();
        auto now_time_t = std::chrono::system_clock::to_time_t(now);
        auto now_ms = std::chrono::duration_cast<std::chrono::milliseconds>(
            now.time_since_epoch()) % 1000;
        
        std::stringstream ss;
        ss << std::put_time(std::localtime(&now_time_t), "%Y%m%d_%H%M%S");
        ss << "_" << std::setfill('0') << std::setw(3) << now_ms.count();
        
        mDataDir_ = ss.str();
        
        // 创建主目录
        fs::create_directory(mDataDir_);
        
        // 创建相机子目录
        mCam0Dir_ = mDataDir_ + "/cam0";
        mCam1Dir_ = mDataDir_ + "/cam1";
        fs::create_directory(mCam0Dir_);
        fs::create_directory(mCam1Dir_);
        
        // 设置IMU CSV文件路径
        mImuCsvPath_ = mDataDir_ + "/imu.csv";
    }
    
    void SaveImageToFile(const cv::Mat& img, const std::string& dir, uint64_t timestamp_ns, 
                     std::atomic<int>& frameCounter, std::atomic<uint64_t>& firstTimestamp,
                     std::atomic<uint64_t>& lastTimestamp, std::atomic<int>& dropCounter) {
    
        // 如果是第一帧，记录第一个时间戳
        if (frameCounter.load() == 0) {
            firstTimestamp.store(timestamp_ns);
        }
        // 检查帧间隔（用于掉帧检测）
        uint64_t last_ts = lastTimestamp.load();
        if (last_ts > 0) {
            double interval_ms = (timestamp_ns - last_ts) / 1e6;
            double interval=1000/hz_;
            double expected_interval = interval; // 
            
            if (interval_ms > expected_interval * 1.5) {
                dropCounter.fetch_add(1);
                std::cout << "[Warning] Possible frame drop detected! Interval: " 
                         << interval_ms << "ms (expected: " << expected_interval << "ms)" << std::endl;
            }
        }
        
        lastTimestamp.store(timestamp_ns);
        
        // 生成文件名
        std::string filename = std::to_string(timestamp_ns) + ".bmp";
        std::string filepath = dir + "/" + filename;
        
        // 保存图像
        if (cv::imwrite(filepath, img)) {
            frameCounter.fetch_add(1);
        }
    }
    
    void CleanIncompleteData() {
        // 检查并删除可能不完整的最后一对图像
        std::vector<std::string> left_files, right_files;
        
        // 获取cam0目录下的图像文件
        for (const auto& entry : fs::directory_iterator(mCam0Dir_)) {
            if (entry.is_regular_file() && entry.path().extension() == ".bmp") {
                left_files.push_back(entry.path().filename().string());
            }
        }
        
        // 获取cam1目录下的图像文件
        for (const auto& entry : fs::directory_iterator(mCam1Dir_)) {
            if (entry.is_regular_file() && entry.path().extension() == ".bmp") {
                right_files.push_back(entry.path().filename().string());
            }
        }
        
        // 排序确保按时间戳顺序
        std::sort(left_files.begin(), left_files.end());
        std::sort(right_files.begin(), right_files.end());
        
        // 如果左右图像数量不同，删除多余的最后一张
        if (left_files.size() > right_files.size()) {
            std::string last_file = mCam0Dir_ + "/" + left_files.back();
            fs::remove(last_file);
            mTotalLeftFrames_.fetch_sub(1);
            std::cout << "Removed incomplete left image: " << last_file << std::endl;
        } else if (right_files.size() > left_files.size()) {
            std::string last_file = mCam1Dir_ + "/" + right_files.back();
            fs::remove(last_file);
            mTotalRightFrames_.fetch_sub(1);
            std::cout << "Removed incomplete right image: " << last_file << std::endl;
        }
    }
    
    void PrintStatistics() {
        // 获取统计值
        int total_left = mTotalLeftFrames_.load();
        int total_right = mTotalRightFrames_.load();
        int total_imu = mTotalImuSamples_.load();
        
        // 获取时间戳范围
        uint64_t first_left_time = mFirstLeftTime_.load();
        uint64_t last_left_time = mLastLeftTime_.load();
        uint64_t first_right_time = mFirstRightTime_.load();
        uint64_t last_right_time = mLastRightTime_.load();
        uint64_t first_imu_time = mFirstImuTime_.load();
        uint64_t last_imu_time = mLastImuTime_.load();
        
        std::cout << "\n========== Data Collection Statistics ==========" << std::endl;
        std::cout << "Data directory: " << mDataDir_ << std::endl;
        std::cout << "Left images saved: " << total_left << " (cam0)" << std::endl;
        std::cout << "Right images saved: " << total_right << " (cam1)" << std::endl;
        std::cout << "IMU samples saved: " << total_imu << std::endl;
        
        // 计算并显示频率
        if (total_left > 1 && last_left_time > first_left_time) {
            double left_duration = (last_left_time - first_left_time) / 1e9; // 转换为秒
            double left_fps = (total_left - 1) / left_duration; // 计算平均频率
            std::cout << "Left camera average FPS: " << std::fixed << std::setprecision(2) 
                    << left_fps << " Hz" << std::endl;
        }
        
        if (total_right > 1 && last_right_time > first_right_time) {
            double right_duration = (last_right_time - first_right_time) / 1e9;
            double right_fps = (total_right - 1) / right_duration;
            std::cout << "Right camera average FPS: " << std::fixed << std::setprecision(2) 
                    << right_fps << " Hz" << std::endl;
        }
        
        if (total_imu > 1 && last_imu_time > first_imu_time) {
            double imu_duration = (last_imu_time - first_imu_time) / 1e9;
            double imu_fps = (total_imu - 1) / imu_duration;
            std::cout << "IMU average frequency: " << std::fixed << std::setprecision(2) 
                    << imu_fps << " Hz" << std::endl;
        }
        
        std::cout << "Left frame drops detected: " << mLeftDropCount_.load() << std::endl;
        std::cout << "Right frame drops detected: " << mRightDropCount_.load() << std::endl;
        std::cout << "IMU drop events: " << mImuDropCount_.load() << std::endl;
        std::cout << "=============================================\n" << std::endl;
    }

    // 生产者线程：从SDK取数据放入缓冲区
    void ImuProducer() {
        while (mbIsRunning_) {
            AtrakIMU imuData;
            if (FAYS_VIK_GetImuData(mptrHandle_, &imuData) == EXIT_SUCCESS) {
                std::unique_lock<std::mutex> lock(mBufferMutex_);
                
                // 如果缓冲区满，丢弃最旧的数据
                if (mImuBuffer_.size() >= MAX_BUFFER_SIZE) {
                    mImuBuffer_.pop();
                }
                
                mImuBuffer_.push(imuData);
                lock.unlock();
                mBufferCond_.notify_one(); // 通知消费者
            }
            std::this_thread::yield(); // 让出CPU
        }
    }

    // 消费者线程：从缓冲区取数据并保存
    void ImuConsumer() {
        uint64_t last_imu_time = 0;
        
        while (mbIsRunning_) {
            AtrakIMU imuData;
            bool gotData = false;
            
            // 从缓冲区获取数据
            {
                std::unique_lock<std::mutex> lock(mBufferMutex_);
                if (mBufferCond_.wait_for(lock, std::chrono::milliseconds(10),
                    [this]() { return !mImuBuffer_.empty() || !mbIsRunning_; })) {
                    if (!mImuBuffer_.empty()) {
                        imuData = mImuBuffer_.front();
                        mImuBuffer_.pop();
                        gotData = true;
                    }
                }
            }
            
            // 处理获取到的数据（原ImuOnlineCapture的保存逻辑）
            if (gotData) {
                // 检查间隔（原逻辑）
                if (last_imu_time > 0) {
                    double interval_ms = (imuData.timestamp - last_imu_time) / 1e6;
                    if (interval_ms > 0.97 * 1.5) { // 注意：这个阈值需要根据ARM实际频率调整
                        mImuDropCount_.fetch_add(1);
                    }
                }
                last_imu_time = imuData.timestamp;
                
                // 保存数据（原逻辑）
                if (mImuCsvFile_.is_open()) {
                    mImuCsvFile_ << imuData.timestamp << ","
                                << imuData.gyro[0] << ","
                                << imuData.gyro[1] << ","
                                << imuData.gyro[2] << ","
                                << imuData.acc[0] << ","
                                << imuData.acc[1] << ","
                                << imuData.acc[2] << "\n";
                    
                    if (mTotalImuSamples_.fetch_add(1) % 100 == 99) {
                        mImuCsvFile_.flush();
                    }
                }
            }
        }
    }

    // 图像生产者线程：从SDK获取图像放入缓冲区
    void ImgProducer() {
        while (mbIsRunning_) {
            // 使用成员变量 mImgData_，其 data 指针已在构造函数中分配
            if (EXIT_SUCCESS == FAYS_VIK_GetStereoFrames(mptrHandle_, &mImgData_)) {
                std::unique_lock<std::mutex> lock(mImgBufferMutex_);
                
                if (mImgBuffer_.size() >= MAX_IMG_BUFFER_SIZE) {
                    mImgBuffer_.pop();
                }
                
                mImgBuffer_.push(mImgData_); // 将有效数据入队
                lock.unlock();
                mImgBufferCond_.notify_one();
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
    }

    // 图像消费者线程：从缓冲区取图像并处理
    void ImgConsumer() {
        while (mbIsRunning_) {
            AtrakImage imgData;
            bool gotData = false;
            
            {
                std::unique_lock<std::mutex> lock(mImgBufferMutex_);
                if (mImgBufferCond_.wait_for(lock, std::chrono::milliseconds(10),
                    [this]() { return !mImgBuffer_.empty() || !mbIsRunning_; })) {
                    if (!mImgBuffer_.empty()) {
                        imgData = mImgBuffer_.front();
                        mImgBuffer_.pop();
                        gotData = true;
                    }
                }
            }
            
            if (gotData) {
                // 处理图像（原ImgOnlineCapture的逻辑）
                cv::Mat img(imgData.height, imgData.width, 
                           imgData.channel == 1 ? CV_8UC1 : CV_8UC3, imgData.data);
                
                cv::Mat left = img(cv::Rect(0, 0, imgData.width, imgData.height / 2));
                cv::Mat right = img(cv::Rect(0, imgData.height / 2, imgData.width, imgData.height / 2));
                
                std::lock_guard<std::mutex> lock(mImgMutex_);
                img.copyTo(mLatestImg_);
                
                SaveImageToFile(left, mCam0Dir_, imgData.timestamp,
                              mTotalLeftFrames_, mFirstLeftTime_, mLastLeftTime_, mLeftDropCount_);
                
                SaveImageToFile(right, mCam1Dir_, imgData.timestamp,
                              mTotalRightFrames_, mFirstRightTime_, mLastRightTime_, mRightDropCount_);
            }
        }
    }

    void KeyMonitor() {
        while (mbIsRunning_) {
            char key = std::cin.get();
            if (key == 'q' || key == 'Q') {
                PrintStatistics();
                mbIsRunning_ = false;
                break;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
    }

private:
    void* mptrHandle_;
     std::thread mptrImgProducerThr_;  // 图像生产者
    std::thread mptrImgConsumerThr_;  // 图像消费者
    std::thread mptrImuProducerThr_;  // 改为生产者线程
    std::thread mptrImuConsumerThr_;  // 新增消费者线程
    std::thread mptrKeyThr_;

    AtrakIMU mImuData_;
    AtrakImage mImgData_;
    AtrakImage mRgbData_;  // 保留但不使用

    std::atomic<bool> mbIsRunning_;
    
    // 第一个时间戳记录
    std::atomic<uint64_t> mFirstLeftTime_;
    std::atomic<uint64_t> mFirstRightTime_;
    std::atomic<uint64_t> mFirstImuTime_;
    std::atomic<uint64_t> mLastImuTime_;
    std::atomic<int> mImuDropCount_;

    // 数据保存相关
    std::string mDataDir_;
    std::string mCam0Dir_;
    std::string mCam1Dir_;
    std::string mImuCsvPath_;
    std::ofstream mImuCsvFile_;
    
    // 统计信息
    std::atomic<int> mTotalLeftFrames_;
    std::atomic<int> mTotalRightFrames_;
    std::atomic<int> mTotalImuSamples_;
    std::atomic<uint64_t> mLastLeftTime_;
    std::atomic<uint64_t> mLastRightTime_;
    std::atomic<int> mLeftDropCount_;
    std::atomic<int> mRightDropCount_;

    std::mutex mImgMutex_;
    cv::Mat mLatestImg_;
    double hz_;
    // IMU 缓冲区相关
    std::queue<AtrakIMU> mImuBuffer_;
    std::mutex mBufferMutex_;
    std::condition_variable mBufferCond_;
    const size_t MAX_BUFFER_SIZE = 1000; // 缓冲区最大容量

     // 图像缓冲区相关
    std::queue<AtrakImage> mImgBuffer_;
    std::mutex mImgBufferMutex_;
    std::condition_variable mImgBufferCond_;
    const size_t MAX_IMG_BUFFER_SIZE = 30; // 图像缓冲区最大容量
};

int main(int argc, char** argv) {
   if (argc < 3) {
        std::cerr << "Usage: ./app <config_path> <hz>" << std::endl;
        std::cerr << "Example: ./app config.yaml 30.0" << std::endl;
        return 1;
    }
    double hz = 30.0; // 默认值
    hz = std::stod(argv[2]);
    FaysExample faysVIKit(argv[1],hz);

    cv::namedWindow("Stereo Image", cv::WINDOW_NORMAL);

    while (faysVIKit.IsRunning()) {
        cv::Mat img;
        if (faysVIKit.GetLatestImage(img)) {
            cv::imshow("Stereo Image", img);
        }

        if (cv::waitKey(1) == 27) {  // ESC键退出
            break;
        }
    }

    return 0;
}
