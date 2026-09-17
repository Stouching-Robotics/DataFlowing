#pragma once

#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <dirent.h>
#include <fcntl.h>
#include <fstream>
#include <iostream>
#include <linux/videodev2.h>
#include <sstream>
#include <string>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <unistd.h>

namespace ksq_fays {

inline std::string imuPortFromConfig(const std::string& config) {
    std::ifstream input(config);
    std::string line;
    while (std::getline(input, line)) {
        const auto colon = line.find(':');
        if (colon == std::string::npos) continue;
        std::istringstream key_stream(line.substr(0, colon));
        std::string key, port;
        key_stream >> key;
        if (key != "imu_dev_port") continue;
        std::istringstream value_stream(line.substr(colon + 1));
        value_stream >> port;
        if (port.size() >= 2 && (port.front() == '\'' || port.front() == '"') &&
                port.back() == port.front()) port = port.substr(1, port.size() - 2);
        if (port.compare(0, 10, "/dev/video") != 0 || port.size() == 10 ||
                port.find_first_not_of("0123456789", 10) != std::string::npos) return {};
        return port;
    }
    return {};
}

// VI Kit 3.5.2 joins ImuOnlineCapture before unblocking its VIDIOC_DQBUF.
// After input has stopped, STREAMOFF on this process's *existing SDK fd*
// wakes that read so DestroyHandle can join the thread. Never open a device,
// reset USB, or touch another rig's stream. Call only in normal thread context.
inline void stopOwnedImuStream(const std::string& config) {
    const std::string port = imuPortFromConfig(config);
    struct stat expected{};
    if (port.empty() || stat(port.c_str(), &expected) != 0 || !S_ISCHR(expected.st_mode)) return;
    DIR* directory = opendir("/proc/self/fd");
    if (!directory) return;
    while (dirent* entry = readdir(directory)) {
        char* end = nullptr;
        const long number = std::strtol(entry->d_name, &end, 10);
        if (end == entry->d_name || *end != '\0' || number < 0) continue;
        struct stat actual{};
        const int fd = static_cast<int>(number);
        if (fstat(fd, &actual) != 0 || !S_ISCHR(actual.st_mode) ||
                actual.st_rdev != expected.st_rdev) continue;
        const int owned = fcntl(fd, F_DUPFD_CLOEXEC, 0);
        if (owned < 0) continue;
        v4l2_buf_type type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
        int result;
        do { result = ioctl(owned, VIDIOC_STREAMOFF, &type); } while (result < 0 && errno == EINTR);
        const int error = result < 0 ? errno : 0;
        close(owned);
        std::cerr << "[FAYS-CLEANUP] imu_streamoff port=" << port
                  << " result=" << result << " errno=" << error << "\n";
        break;
    }
    closedir(directory);
}

}  // namespace ksq_fays
