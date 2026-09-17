// Opt-in x86_64 SDK diagnostic shim, never linked into the production bridge.
// Build: c++ -std=c++17 -O2 -shared -fPIC input_trace.cc -I<SDK>/include -ldl -o input_trace.so
// Enable ONLY for a Fays bridge: LD_PRELOAD=<so> KSQ_FAYS_TRACE_DIR=<existing dir>.
// No SDK setup/identity calls, capture changes, affinity changes or image copies.
// Events go into a bounded shared mmap (4 MiB), not per-frame log writes. Analyze
// only after stopping the process. A full trace stops recording events, not capture.
#include "fays_atrak/fays_atrak_types.h"
#include <atomic>
#include <cerrno>
#include <cstdarg>
#include <sys/ioctl.h>
#include <linux/videodev2.h>
#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <dlfcn.h>
#include <fcntl.h>
#include <functional>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>
#include <time.h>
#include <vector>

namespace {
constexpr uint64_t kCapacity = 65536;
struct Event {
    uint64_t start_ns, end_ns, cpu_ns, sensor_ns, object;
    int32_t tid, fd, seq, status;
    uint32_t kind, ready; // 1 = ReadFrame; 2 = app callback; 3 = SDK DQBUF
};
static_assert(sizeof(Event) == 64, "Trace ABI changed");
struct Header { uint64_t magic, version, capacity, reserved[5]; };
static_assert(sizeof(Header) == 64, "Trace ABI changed");
struct Trace {
    Event* events = nullptr;
    std::atomic<uint64_t> next{0};
    Trace() {
        const char* dir = std::getenv("KSQ_FAYS_TRACE_DIR");
        if (!dir || !*dir) return;
        char name[4096];
        if (std::snprintf(name, sizeof(name), "%s/fays-input-%ld.bin", dir,
                          static_cast<long>(getpid())) >= int(sizeof(name))) return;
        int fd = open(name, O_RDWR | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
        if (fd < 0) { std::perror("Fays input trace open"); return; }
        const size_t bytes = sizeof(Header) + kCapacity * sizeof(Event);
        // Reserve backing storage before mmap: never risk SIGBUS on ENOSPC.
        int allocated = posix_fallocate(fd, 0, bytes);
        void* p = allocated == 0 ? mmap(nullptr, bytes, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0)
                                : MAP_FAILED;
        close(fd);
        if (p == MAP_FAILED) { unlink(name); return; }
        *static_cast<Header*>(p) = {0x4b53514641595331ULL, 1, kCapacity, {0}};
        events = reinterpret_cast<Event*>(static_cast<Header*>(p) + 1);
        std::fprintf(stderr, "[FAYS-INPUT-TRACE] %s capacity=%llu\n", name,
                     static_cast<unsigned long long>(kCapacity));
        // Mapping intentionally lives to process exit, including forced exit.
    }
    void put(Event e) {
        const uint64_t i = next.fetch_add(1, std::memory_order_relaxed);
        if (i >= kCapacity) return;
        events[i] = e;
        __atomic_store_n(&events[i].ready, 1u, __ATOMIC_RELEASE);
    }
};
Trace& trace() { static Trace value; return value; }
thread_local bool inside_read = false;
struct Reading {
    bool previous = inside_read;
    Reading() { inside_read = true; }
    ~Reading() { inside_read = previous; }
};
uint64_t ns(clockid_t clock) {
    timespec t{}; clock_gettime(clock, &t);
    return uint64_t(t.tv_sec) * 1000000000ULL + t.tv_nsec;
}
template<class T> T next_symbol(const char* name) {
    auto result = reinterpret_cast<T>(dlsym(RTLD_NEXT, name));
    if (!result) { std::fprintf(stderr, "[FAYS-INPUT-TRACE] missing SDK symbol %s\n", name); std::abort(); }
    return result;
}
class Scope {
    Trace& t;
    Event e{};
    uint64_t cpu_start;
public:
    Scope(Trace& target, unsigned kind, const void* object, int fd, const AtrakImage* img)
        : t(target) {
        const int saved_errno = errno;
        e.kind = kind; e.object = reinterpret_cast<uintptr_t>(object); e.fd = fd;
        e.tid = static_cast<int32_t>(syscall(SYS_gettid));
        if (img) { e.seq = img->seq; e.sensor_ns = img->timestamp; }
        e.start_ns = ns(CLOCK_MONOTONIC); cpu_start = ns(CLOCK_THREAD_CPUTIME_ID);
        errno = saved_errno;
    }
    void result(bool ok, const AtrakImage* image) {
        e.status = ok ? 1 : 0;
        if (ok && image) { e.seq = image->seq; e.sensor_ns = image->timestamp; }
    }
    void buffer_result(int rc, const v4l2_buffer* buffer) {
        e.status = rc == 0 ? 1 : 0;
        if (rc == 0 && buffer) {
            e.seq = static_cast<int32_t>(buffer->sequence);
            e.object = buffer->flags;
            e.sensor_ns = uint64_t(buffer->timestamp.tv_sec) * 1000000000ULL
                + uint64_t(buffer->timestamp.tv_usec) * 1000ULL;
        }
    }
    ~Scope() {
        const int saved_errno = errno;
        e.cpu_ns = ns(CLOCK_THREAD_CPUTIME_ID) - cpu_start;
        e.end_ns = ns(CLOCK_MONOTONIC);
        t.put(e);
        errno = saved_errno;
    }
};
}

// Exact Itanium ABI symbol from the shipped SDK; this is diagnostic-only and
// must be retested against any replacement SDK. The real call and its result
// are preserved, including the image pointer and SDK-owned image lifetime.
extern "C" bool traced_read(void* self, int fd, std::vector<void*>& buffers,
                             AtrakImage* image, unsigned long& timestamp)
    asm("_ZN17FaysActiveTracker5ViKit9ReadFrameEiRSt6vectorIPvSaIS2_EEP10AtrakImageRm");
extern "C" bool traced_read(void* self, int fd, std::vector<void*>& buffers,
                             AtrakImage* image, unsigned long& timestamp) {
    const int saved_errno = errno;
    using Fn = bool(*)(void*, int, std::vector<void*>&, AtrakImage*, unsigned long&);
    static Fn real = next_symbol<Fn>("_ZN17FaysActiveTracker5ViKit9ReadFrameEiRSt6vectorIPvSaIS2_EEP10AtrakImageRm");
    auto& t = trace();
    errno = saved_errno;
    if (!t.events) return real(self, fd, buffers, image, timestamp);
    Scope event(t, 1, self, fd, nullptr);
    Reading reading;
    bool ok = real(self, fd, buffers, image, timestamp);
    event.result(ok, image);
    return ok;
}

int FAYS_VIK_RegisterStereoImageCallback(void* handle, std::function<void(AtrakImage*)> callback) {
    const int saved_errno = errno;
    using Fn = int(*)(void*, std::function<void(AtrakImage*)>);
    static Fn real = next_symbol<Fn>("_Z36FAYS_VIK_RegisterStereoImageCallbackPvSt8functionIFvP10AtrakImageEE");
    auto& t = trace();
    errno = saved_errno;
    if (!t.events || !callback) return real(handle, std::move(callback));
    return real(handle, [handle, callback = std::move(callback)](AtrakImage* image) {
        Scope event(trace(), 2, handle, -1, image);
        callback(image);
        event.result(true, nullptr);
    });
}

// Linux ioctl's third argument is conventionally passed as a pointer/word.
// Forward every request unchanged; inspect only VIDIOC_DQBUF issued inside the
// actual SDK ReadFrame call. Never issue additional ioctls or touch the pixels.
extern "C" int ioctl(int fd, unsigned long request, ...) noexcept {
    const int saved_errno = errno;
    va_list args;
    va_start(args, request);
    void* argument = va_arg(args, void*);
    va_end(args);
    using Fn = int(*)(int, unsigned long, ...);
    static Fn real = next_symbol<Fn>("ioctl");
    errno = saved_errno;
    if (!inside_read || request != VIDIOC_DQBUF)
        return real(fd, request, argument);
    Scope event(trace(), 3, nullptr, fd, nullptr);
    int rc = real(fd, request, argument);
    event.buffer_result(rc, static_cast<v4l2_buffer*>(argument));
    return rc;
}
