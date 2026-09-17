/**
 * @file reset_usb.c — 重置 FTDI USB 设备 (模拟物理插拔)
 * 编译: gcc -o reset_usb reset_usb.c -lusb-1.0
 */
#include <stdio.h>
#include <stdlib.h>
#include <libusb-1.0/libusb.h>

int main(void) {
    libusb_context *ctx = NULL;
    libusb_init(&ctx);

    libusb_device **devs;
    ssize_t cnt = libusb_get_device_list(ctx, &devs);
    if (cnt < 0) { fprintf(stderr, "No USB devices\n"); return 1; }

    int found = 0;
    for (ssize_t i = 0; i < cnt; i++) {
        struct libusb_device_descriptor desc;
        libusb_get_device_descriptor(devs[i], &desc);
        if (desc.idVendor == 0x0403 && desc.idProduct == 0x602e) {  // FTDI Superspeed
            libusb_device_handle *h;
            int r = libusb_open(devs[i], &h);
            if (r == 0) {
                r = libusb_reset_device(h);
                libusb_close(h);
                printf("[USB] FTDI device reset: %s\n",
                       r == 0 ? "OK" : libusb_error_name(r));
                found = 1;
            } else if (r == LIBUSB_ERROR_ACCESS) {
                fprintf(stderr, "[USB] Permission denied. Run: sudo ./scripts/setup_ftdi_permissions.sh\n");
            }
        }
    }

    libusb_free_device_list(devs, 1);
    libusb_exit(ctx);
    return found ? 0 : 1;
}
