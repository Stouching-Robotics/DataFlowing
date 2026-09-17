#pragma once

// Included after RawStreamHeader / RawImuPayload definitions. Debug uses its
// own bounded queue and never consumes the formal recording socket/queues.
#include <sys/statvfs.h>
#include <memory>

class FaysDebugCapture {
    struct Sample {
        RawStreamHeader header{};
        RawImuPayload imu{};
        cv::Mat image;  // ref-count the already owned SDK frame; no second copy
    };
    std::atomic<bool> enabled_{false};
    std::mutex mutex_;
    std::condition_variable condition_;
    std::deque<Sample> queue_;
    size_t queued_bytes_ = 0;
    static constexpr size_t kQueueBytes = 32 * 1024 * 1024;
    static constexpr uint64_t kChunkBytes = 512ULL * 1024 * 1024;
    static constexpr uint64_t kReserveBytes = 2ULL * 1024 * 1024 * 1024;
    std::thread thread_;
    std::string directory_, reason_ = "disconnect";
    std::ofstream output_;
    std::vector<char> io_buffer_ = std::vector<char>(1024 * 1024);
    uint64_t bytes_ = 0, chunk_bytes_ = 0, imu_count_ = 0, stereo_count_ = 0;
    std::atomic<uint64_t> imu_sequence_{0};
    unsigned chunk_ = 0;
    double duration_ = 300;
    std::chrono::steady_clock::time_point started_;

    static uint64_t clockNs(clockid_t clock) {
        timespec t{}; clock_gettime(clock, &t);
        return uint64_t(t.tv_sec) * 1000000000ULL + t.tv_nsec;
    }
    static RawStreamHeader header(uint32_t kind, uint32_t size, uint64_t timestamp, int32_t sequence) {
        RawStreamHeader h{};
        h.magic = kRawStreamMagic; h.version = kRawStreamVersion;
        h.kind = kind; h.header_size = sizeof(h); h.payload_size = size;
        h.sensor_timestamp_ns = timestamp; h.host_monotonic_ns = clockNs(CLOCK_MONOTONIC);
        h.host_realtime_ns = clockNs(CLOCK_REALTIME); h.sequence = sequence;
        return h;
    }
    bool diskAvailable() const {
        struct statvfs info{};
        return statvfs(directory_.c_str(), &info) == 0 &&
            uint64_t(info.f_bavail) * info.f_frsize > kReserveBytes;
    }
    void openChunk() {
        std::ostringstream path;
        path << directory_ << "/samples-" << std::setw(4) << std::setfill('0') << chunk_++ << ".bin";
        output_.rdbuf()->pubsetbuf(io_buffer_.data(), io_buffer_.size());
        output_.open(path.str(), std::ios::binary | std::ios::out);
        if (!output_) throw std::runtime_error("cannot open sample file");
        chunk_bytes_ = 0;
    }
    void halt(const std::string& reason) {
        std::lock_guard<std::mutex> lock(mutex_);
        reason_ = reason;
        enabled_.store(false, std::memory_order_release);
        condition_.notify_one();
    }
    void enqueue(Sample&& sample) {
        if (!enabled()) return;
        const size_t size = sizeof(RawStreamHeader) + sample.header.payload_size;
        std::lock_guard<std::mutex> lock(mutex_);
        if (!enabled()) return;
        if (queued_bytes_ + size > kQueueBytes) {
            reason_ = "queue_overflow";
            enabled_.store(false, std::memory_order_release);
            std::cerr << "[Connect-Debug] ERROR queue full; debug capture incomplete, SLAM continues\n";
        } else {
            queued_bytes_ += size;
            queue_.emplace_back(std::move(sample));
        }
        condition_.notify_one();
    }
    void bindWriter() {
        const char* cpus = std::getenv("KSQ_FAYS_DEBUG_CPUS");
        if (!cpus || !*cpus) throw std::runtime_error("missing existing general CPU role");
        cpu_set_t requested; CPU_ZERO(&requested);
        std::istringstream stream(cpus); std::string token;
        while (std::getline(stream, token, ',')) {
            char* end = nullptr; long cpu = std::strtol(token.c_str(), &end, 10);
            if (end == token.c_str() || *end || cpu < 0 || cpu >= CPU_SETSIZE)
                throw std::runtime_error("invalid general CPU role");
            CPU_SET(cpu, &requested);
        }
        if (!CPU_COUNT(&requested) || pthread_setaffinity_np(pthread_self(), sizeof(requested), &requested) != 0)
            throw std::runtime_error("cannot bind debug writer to general CPU role");
        std::cerr << "[Connect-Debug] writer general_cpus=" << cpus << "\n";
    }
    void run() {
        try {
            bindWriter();
            auto disk_check = started_;
            while (true) {
                const auto now = std::chrono::steady_clock::now();
                if (enabled() && std::chrono::duration<double>(now - started_).count() >= duration_) halt("duration_limit");
                if (now - disk_check >= std::chrono::seconds(1)) {
                    disk_check = now;
                    if (!diskAvailable()) throw std::runtime_error("less than 2 GiB disk reserve");
                }
                std::deque<Sample> batch;
                {
                    std::unique_lock<std::mutex> lock(mutex_);
                    condition_.wait_for(lock, std::chrono::milliseconds(100), [&] { return !queue_.empty() || !enabled(); });
                    if (queue_.empty() && !enabled()) break;
                    batch.swap(queue_); queued_bytes_ = 0;
                }
                for (const auto& sample : batch) {
                    const auto size = sizeof(sample.header) + sample.header.payload_size;
                    if (chunk_bytes_ && chunk_bytes_ + size > kChunkBytes) {
                        output_.flush(); if (!output_) throw std::runtime_error("chunk flush failed");
                        output_.close(); if (output_.fail()) throw std::runtime_error("chunk close failed");
                        openChunk();
                    }
                    output_.write(reinterpret_cast<const char*>(&sample.header), sizeof(sample.header));
                    if (sample.header.kind == kRawPacketImu)
                        output_.write(reinterpret_cast<const char*>(&sample.imu), sizeof(sample.imu));
                    else output_.write(reinterpret_cast<const char*>(sample.image.data), sample.header.payload_size);
                    if (!output_) throw std::runtime_error("sample write failed");
                    bytes_ += size; chunk_bytes_ += size;
                    if (sample.header.kind == kRawPacketImu) ++imu_count_; else ++stereo_count_;
                }
            }
            output_.flush(); if (!output_) throw std::runtime_error("sample flush failed");
            output_.close(); if (output_.fail()) throw std::runtime_error("sample close failed");
        } catch (const std::exception& e) {
            halt("io_or_affinity_error");
            std::cerr << "[Connect-Debug] ERROR " << e.what() << "; capture incomplete, SLAM continues\n";
        }
        if (output_.is_open()) output_.close();
        std::string reason;
        { std::lock_guard<std::mutex> lock(mutex_); reason = reason_; queue_.clear(); queued_bytes_ = 0; }
        const bool complete = reason == "disconnect" || reason == "duration_limit";
        const std::string temporary = directory_ + "/capture.json.tmp";
        std::ofstream metadata(temporary);
        metadata << "{\"complete\":" << (complete ? "true" : "false")
                 << ",\"reason\":\"" << reason << "\",\"imu_samples\":" << imu_count_
                 << ",\"stereo_frames\":" << stereo_count_ << ",\"bytes\":" << bytes_
                 << ",\"chunks\":" << chunk_ << ",\"elapsed_seconds\":"
                 << std::chrono::duration<double>(std::chrono::steady_clock::now() - started_).count() << "}\n";
        metadata.close();
        if (!metadata || std::rename(temporary.c_str(), (directory_ + "/capture.json").c_str()) != 0)
            std::cerr << "[Connect-Debug] ERROR cannot save completion metadata\n";
        std::cerr << "[Connect-Debug] finished reason=" << reason << " imu=" << imu_count_
                  << " stereo=" << stereo_count_ << " directory=" << directory_ << "\n";
    }
public:
    bool enabled() const { return enabled_.load(std::memory_order_acquire); }
    void start() {
        const char* path = std::getenv("KSQ_FAYS_DEBUG_DIR");
        if (!path || !*path) return;
        directory_ = path;
        const char* seconds = std::getenv("KSQ_FAYS_DEBUG_SECONDS");
        if (seconds) duration_ = std::strtod(seconds, nullptr);
        if (!(duration_ > 0 && duration_ <= 300)) throw std::runtime_error("invalid debug duration");
        if (!diskAvailable()) throw std::runtime_error("connect_debug needs 2 GiB disk reserve");
        openChunk();
        started_ = std::chrono::steady_clock::now();
        enabled_.store(true, std::memory_order_release);
        thread_ = std::thread([this] { run(); });
        std::cerr << "[Connect-Debug] started directory=" << directory_ << " duration_limit_s=" << duration_ << "\n";
    }
    void imu(const AtrakIMU& imu) {
        if (!enabled()) return;
        Sample s;
        s.header = header(kRawPacketImu, sizeof(RawImuPayload), imu.timestamp, ++imu_sequence_);
        s.imu = {imu.acc[0], imu.acc[1], imu.acc[2], imu.gyro[0], imu.gyro[1], imu.gyro[2]};
        enqueue(std::move(s));
    }
    void stereo(const cv::Mat& image, const AtrakImage& source) {
        if (!enabled()) return;
        if (!image.isContinuous()) { halt("non_contiguous_image"); return; }
        Sample s;
        s.header = header(kRawPacketStereo, image.total() * image.elemSize(), source.timestamp, source.seq);
        s.header.width = image.cols; s.header.height = image.rows; s.header.channels = image.channels();
        s.header.step = image.step; s.header.encoding = static_cast<int16_t>(source.encoding);
        s.image = image;
        enqueue(std::move(s));
    }
    void stop() {
        { std::lock_guard<std::mutex> lock(mutex_); enabled_.store(false, std::memory_order_release); condition_.notify_one(); }
        if (thread_.joinable()) thread_.join();
    }
    ~FaysDebugCapture() { stop(); }
};
