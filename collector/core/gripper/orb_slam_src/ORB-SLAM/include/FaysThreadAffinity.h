#ifndef FAYS_THREAD_AFFINITY_H
#define FAYS_THREAD_AFFINITY_H

#include <cerrno>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <pthread.h>
#include <sched.h>
#include <sys/syscall.h>
#include <unistd.h>

namespace ORB_SLAM3
{

// Generic ORB-SLAM3 callers do not set this variable and retain the upstream
// scheduler behavior. The Fays bridge sets it from the device_setup CPU
// policy so native backend threads do not inherit the whole Fays cpuset.
inline bool BindFaysBackgroundThread(
        const char* threadName, const char* role)
{
    const char* configured = std::getenv("KSQ_FAYS_BACKGROUND_CPU");
    if(!configured || !*configured)
        return true;

    errno = 0;
    char* end = nullptr;
    const long parsed = std::strtol(configured, &end, 10);
    if(errno != 0 || end == configured || *end != '\0'
            || parsed < 0 || parsed >= CPU_SETSIZE)
    {
        std::cerr << "[FAYS-AFFINITY] " << role
                  << " invalid KSQ_FAYS_BACKGROUND_CPU=" << configured
                  << std::endl;
        return false;
    }

    pthread_setname_np(pthread_self(), threadName);
    cpu_set_t requested;
    CPU_ZERO(&requested);
    CPU_SET(static_cast<int>(parsed), &requested);
    const int setResult = sched_setaffinity(
        0, sizeof(requested), &requested);

    cpu_set_t actual;
    CPU_ZERO(&actual);
    const bool verified = setResult == 0
        && sched_getaffinity(0, sizeof(actual), &actual) == 0
        && CPU_COUNT(&actual) == 1
        && CPU_ISSET(static_cast<int>(parsed), &actual);

    std::cerr << "[FAYS-AFFINITY] " << role << " thread: tid="
              << static_cast<long>(syscall(SYS_gettid))
              << " cpu=" << parsed
              << " verified=" << (verified ? "yes" : "no");
    if(!verified)
        std::cerr << " error=" << std::strerror(errno);
    std::cerr << std::endl;
    return verified;
}

} // namespace ORB_SLAM3

#endif // FAYS_THREAD_AFFINITY_H
