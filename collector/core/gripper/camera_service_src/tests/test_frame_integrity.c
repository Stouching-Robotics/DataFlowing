/* Test the actual callback and queue without opening USB devices. */
#undef NDEBUG
#include <assert.h>
#define main camera_service_main
#include "../src/camera_service.c"
#undef main

int main(void) {
    camera_state_t camera = {0};
    uint8_t good[] = {0xff, 0xd8, 0x01, 0x02, 0xff, 0xd9};
    uint8_t short_frame[] = {0xff};
    uvc_frame_t frame = {0};
    pthread_mutex_init(&camera.mutex, NULL);
    pthread_cond_init(&camera.condition, NULL);
    frame.frame_format = UVC_FRAME_FORMAT_MJPEG;
    frame.data = good;
    frame.data_bytes = sizeof(good);
    frame.sequence = 1;
    frame.capture_time_finished.tv_sec = 2;
    enqueue_frame(&frame, &camera);
    assert(camera.count == 1);
    assert(camera.queue[0].sequence == 1);
    assert(camera.queue[0].capture_timestamp_ns == 2000000000ull);
    frame.data = short_frame;
    frame.data_bytes = sizeof(short_frame);
    frame.sequence = 2;
    frame.capture_time_finished.tv_sec = 3;
    enqueue_frame(&frame, &camera);
    assert(camera.count == 1); /* No old image under a new timestamp. */
    assert(camera.bad_jpeg_frames == 1);
    assert(camera.frames_dropped == 1);
    assert(camera.bad_jpeg_replaced == 0);
    assert(camera.queue[0].capture_timestamp_ns == 2000000000ull);
    frame.data = good;
    frame.data_bytes = sizeof(good);
    frame.sequence = 3;
    enqueue_frame(&frame, &camera);
    assert(camera.count == 2);
    assert(camera.queue[1].sequence == 3);
    assert(camera.queue[1].capture_timestamp_ns == 3000000000ull);
    for (size_t i = 0; i < FRAME_QUEUE_DEPTH; ++i) free(camera.queue[i].data);
    pthread_mutex_destroy(&camera.mutex);
    pthread_cond_destroy(&camera.condition);
    return 0;
}
