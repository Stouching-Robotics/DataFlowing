/*
 * Minimal user-space discovery demo for Sightac + DECXIN.
 *
 * Scope is intentionally limited to:
 *   Sightac: 0c45:636f, 640x480 MJPEG @ 30
 *   DECXIN : 1bcf:2d4f, 1280x960 MJPEG @ 30
 *
 * Fays is not enumerated as a supported candidate and is never opened by
 * this program.  The demo uses libuvc for UVC enumeration/negotiation and
 * libusb for USB topology/descriptors and the read-only Sonix XU6 probe.
 * It does not start a video stream and it does not write/erase Flash.
 */
#include <ctype.h>
#include <errno.h>
#include <libusb-1.0/libusb.h>
#include <libuvc/libuvc.h>
#include <limits.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define MAX_CANDIDATES 32
#define MAX_GROUPS 16
#define MAX_PORT_PATH 64
#define MAX_TEXT 128

#define SIGHTAC_VID 0x0c45
#define SIGHTAC_PID 0x636f
#define DECXIN_VID 0x1bcf
#define DECXIN_PID 0x2d4f

#define XU_GET_CUR 0x81
#define XU_SET_CUR 0x01
#define XU_CTRL_GET 0xa1
#define XU_CTRL_SET 0x21
#define XU_ASIC_SELECTOR 0x01
#define XU_FLASH_XU6_SELECTOR 0x06
#define CTRL_TIMEOUT_MS 2000
#define FLASH_PROBE_ATTEMPTS 3
#define FLASH_RETRY_DELAY_US 25000
#define FLASH_MAX_ADDRESS 0x4ffff
#define SF_XU64_EU_ADDR 0x335
#define SF_XU64_RW_START_ADDR 0x337
#define SF_XU64_RW_END_ADDR 0x33b

typedef struct {
    int vid;
    int pid;
    int width;
    int height;
    int fps;
    int expected_altsetting;
    int forced_payload;
    const char *kind;
} mode_profile_t;

typedef struct {
    uvc_device_t *uvc_device;
    libusb_device *usb_device;
    int vid;
    int pid;
    int bus;
    char port[MAX_PORT_PATH];
    char usb_serial[MAX_TEXT];
    char device_type[MAX_TEXT];
    char side[16];
    char flash_serial[MAX_TEXT];
    int control_interface;
    int streaming_interface;
    int altsetting;
    unsigned int descriptor_payload;
    unsigned int negotiated_payload;
    int mode_ok;
    int flash_ok;
    int flash_params_ok;
    double fx_params[5];
    double fy_params[5];
    double fz_params[21];
    double ft_params[20];
    double fw_params[9];
    int opened_ok;
    char error[MAX_TEXT];
} camera_candidate_t;

typedef struct {
    int sightac[2];
    int decxin;
    int bus;
    char outer_path[MAX_PORT_PATH];
} camera_group_t;

typedef enum {
    FLASH_MODE_FULL = 0,
    FLASH_MODE_IDENTITY = 1,
    FLASH_MODE_NONE = 2,
    FLASH_MODE_TOPOLOGY_ONLY = 3,
} flash_mode_t;

static const mode_profile_t *profile_for(int vid, int pid) {
    static const mode_profile_t sightac = {
        SIGHTAC_VID, SIGHTAC_PID, 640, 480, 30, 3, 800, "sightac"
    };
    /* The scanner reports the camera's *preferred* altsetting -- the one the
     * descriptor advertises, which gives frames the most room: alt7/1280
     * (2 transactions per microframe) fits 8 x 32 x 1280 = 327680 B per frame,
     * while alt6/944 fits only 241664 B against a measured worst case of
     * 234148 B.
     *
     * This value is *reported*, not used as the starting rung. Measured
     * 2026-09-15: alt7 stalls every 45-130 s while all three cameras stream,
     * on both host controllers tried, and it delivers the same 30.00 fps as
     * alt6 -- so the extra per-frame headroom buys nothing and the app starts
     * at the most conservative pair instead (uvc_camera_service.py,
     * _starting_mode). Keeping the scanner honest about the descriptor matters
     * even so: the app checks this pair against the service's mode table, and a
     * mismatch means scanner and service were built apart from each other. */
    static const mode_profile_t decxin = {
        DECXIN_VID, DECXIN_PID, 1280, 960, 30, 7, 1280, "decxin"
    };
    if (vid == SIGHTAC_VID && pid == SIGHTAC_PID) {
        return &sightac;
    }
    if (vid == DECXIN_VID && pid == DECXIN_PID) {
        return &decxin;
    }
    return NULL;
}

static const char *usb_error_text(int result) {
    return result < 0 ? libusb_strerror((enum libusb_error)result) :
                        libusb_strerror((enum libusb_error)result);
}

static void copy_text(char *destination, size_t size, const char *source) {
    size_t length;
    if (size == 0) {
        return;
    }
    if (source == NULL) {
        destination[0] = '\0';
        return;
    }
    length = strlen(source);
    if (length >= size) {
        length = size - 1;
    }
    memcpy(destination, source, length);
    destination[length] = '\0';
}

static void write_json_string(FILE *out, const char *value) {
    fputc('"', out);
    for (const unsigned char *cursor = (const unsigned char *)(value ? value : "");
         *cursor != '\0'; ++cursor) {
        switch (*cursor) {
        case '"':
            fputs("\\\"", out);
            break;
        case '\\':
            fputs("\\\\", out);
            break;
        case '\b':
            fputs("\\b", out);
            break;
        case '\f':
            fputs("\\f", out);
            break;
        case '\n':
            fputs("\\n", out);
            break;
        case '\r':
            fputs("\\r", out);
            break;
        case '\t':
            fputs("\\t", out);
            break;
        default:
            if (*cursor < 0x20) {
                fprintf(out, "\\u%04x", (unsigned int)*cursor);
            } else {
                fputc(*cursor, out);
            }
            break;
        }
    }
    fputc('"', out);
}

static int format_uvc_port_path(uvc_device_t *device, char *out,
                                size_t out_size) {
    uint8_t ports[8];
    int count = uvc_get_port_numbers(device, ports, sizeof(ports));
    size_t used = 0;
    if (count < 0 || out_size == 0) {
        return count < 0 ? count : LIBUSB_ERROR_INVALID_PARAM;
    }
    out[0] = '\0';
    for (int index = 0; index < count; ++index) {
        int written = snprintf(out + used, out_size - used, "%s%u",
                               index == 0 ? "" : ".", ports[index]);
        if (written < 0 || (size_t)written >= out_size - used) {
            return LIBUSB_ERROR_OVERFLOW;
        }
        used += (size_t)written;
    }
    return 0;
}

static int format_libusb_port_path(libusb_device *device, char *out,
                                   size_t out_size) {
    uint8_t ports[8];
    int count = libusb_get_port_numbers(device, ports, sizeof(ports));
    size_t used = 0;
    if (count < 0 || out_size == 0) {
        return count < 0 ? count : LIBUSB_ERROR_INVALID_PARAM;
    }
    out[0] = '\0';
    for (int index = 0; index < count; ++index) {
        int written = snprintf(out + used, out_size - used, "%s%u",
                               index == 0 ? "" : ".", ports[index]);
        if (written < 0 || (size_t)written >= out_size - used) {
            return LIBUSB_ERROR_OVERFLOW;
        }
        used += (size_t)written;
    }
    return 0;
}

static int find_control_interface(libusb_device *device) {
    struct libusb_config_descriptor *config = NULL;
    int result = libusb_get_active_config_descriptor(device, &config);
    if (result != 0) {
        return result;
    }
    int found = LIBUSB_ERROR_NOT_FOUND;
    for (int i = 0; i < config->bNumInterfaces && found < 0; ++i) {
        for (int j = 0; j < config->interface[i].num_altsetting; ++j) {
            const struct libusb_interface_descriptor *alt =
                &config->interface[i].altsetting[j];
            if (alt->bInterfaceClass == LIBUSB_CLASS_VIDEO &&
                alt->bInterfaceSubClass == 1) {
                found = alt->bInterfaceNumber;
                break;
            }
        }
    }
    libusb_free_config_descriptor(config);
    return found;
}

static unsigned int endpoint_payload(const struct libusb_endpoint_descriptor *ep) {
    unsigned int payload = ep->wMaxPacketSize & 0x07ffu;
    payload *= 1u + ((ep->wMaxPacketSize >> 11) & 0x03u);
    return payload;
}

static int find_stream_mode(libusb_device *device, int expected_altsetting,
                            unsigned int minimum_payload,
                            int *interface_number, int *altsetting,
                            unsigned int *payload) {
    struct libusb_config_descriptor *config = NULL;
    int result = libusb_get_active_config_descriptor(device, &config);
    if (result != 0) {
        return result;
    }
    int found = LIBUSB_ERROR_NOT_FOUND;
    unsigned int selected_payload = 0;
    int selected_interface = -1;
    for (int i = 0; i < config->bNumInterfaces; ++i) {
        for (int j = 0; j < config->interface[i].num_altsetting; ++j) {
            const struct libusb_interface_descriptor *alt =
                &config->interface[i].altsetting[j];
            if (alt->bInterfaceClass != LIBUSB_CLASS_VIDEO ||
                alt->bInterfaceSubClass != 2) {
                continue;
            }
            if (alt->bAlternateSetting != expected_altsetting) {
                continue;
            }
            for (int k = 0; k < alt->bNumEndpoints; ++k) {
                const struct libusb_endpoint_descriptor *ep = &alt->endpoint[k];
                unsigned int transfer_type = ep->bmAttributes &
                                              LIBUSB_TRANSFER_TYPE_MASK;
                if ((ep->bEndpointAddress & LIBUSB_ENDPOINT_DIR_MASK) !=
                        LIBUSB_ENDPOINT_IN ||
                    (transfer_type != LIBUSB_TRANSFER_TYPE_ISOCHRONOUS &&
                     transfer_type != LIBUSB_TRANSFER_TYPE_BULK)) {
                    continue;
                }
                unsigned int current_payload = endpoint_payload(ep);
                if (current_payload >= minimum_payload &&
                    current_payload > selected_payload) {
                    selected_payload = current_payload;
                    selected_interface = alt->bInterfaceNumber;
                    found = 0;
                }
            }
        }
    }
    libusb_free_config_descriptor(config);
    if (found == 0) {
        *interface_number = selected_interface;
        *altsetting = expected_altsetting;
        *payload = selected_payload;
    }
    if (found == 0 && *interface_number < 0) {
        found = LIBUSB_ERROR_NOT_FOUND;
    }
    return found;
}

static libusb_device *find_raw_device(libusb_device **devices, ssize_t count,
                                      int vid, int pid, int bus,
                                      const char *wanted_port) {
    for (ssize_t index = 0; index < count; ++index) {
        struct libusb_device_descriptor descriptor;
        char port[MAX_PORT_PATH];
        if (libusb_get_device_descriptor(devices[index], &descriptor) != 0 ||
            descriptor.idVendor != (uint16_t)vid ||
            descriptor.idProduct != (uint16_t)pid ||
            (int)libusb_get_bus_number(devices[index]) != bus ||
            format_libusb_port_path(devices[index], port, sizeof(port)) != 0 ||
            strcmp(port, wanted_port) != 0) {
            continue;
        }
        return devices[index];
    }
    return NULL;
}

static int xu_control(libusb_device_handle *handle, int interface_number,
                      uint8_t unit, uint8_t selector, int get,
                      uint8_t *data, uint16_t size) {
    uint8_t request_type = get ? XU_CTRL_GET : XU_CTRL_SET;
    uint8_t request = get ? XU_GET_CUR : XU_SET_CUR;
    uint16_t value = (uint16_t)selector << 8;
    uint16_t index = ((uint16_t)unit << 8) | (uint16_t)interface_number;
    return libusb_control_transfer(handle, request_type, request, value, index,
                                   data, size, CTRL_TIMEOUT_MS);
}

static int asic_read(libusb_device_handle *handle, int interface_number,
                     uint8_t unit, uint16_t address, uint8_t *value) {
    uint8_t data[4] = {
        (uint8_t)(address & 0xff), (uint8_t)(address >> 8), 0, 0xff
    };
    int result = xu_control(handle, interface_number, unit,
                            XU_ASIC_SELECTOR, 0, data, sizeof(data));
    if (result != (int)sizeof(data)) {
        return result < 0 ? result : LIBUSB_ERROR_IO;
    }
    data[3] = 0;
    result = xu_control(handle, interface_number, unit, XU_ASIC_SELECTOR, 1,
                        data, sizeof(data));
    if (result != (int)sizeof(data)) {
        return result < 0 ? result : LIBUSB_ERROR_IO;
    }
    *value = data[2];
    return 0;
}

static int asic_write(libusb_device_handle *handle, int interface_number,
                      uint8_t unit, uint16_t address, uint8_t value) {
    uint8_t data[4] = {
        (uint8_t)(address & 0xff), (uint8_t)(address >> 8), value, 0
    };
    int result = xu_control(handle, interface_number, unit,
                            XU_ASIC_SELECTOR, 0, data, sizeof(data));
    if (result != (int)sizeof(data)) {
        return result < 0 ? result : LIBUSB_ERROR_IO;
    }
    return 0;
}

static int known_chip(uint8_t chip) {
    switch (chip) {
    case 0x15: case 0x16: case 0x22: case 0x23: case 0x25:
    case 0x32: case 0x33: case 0x56: case 0x70: case 0x71:
    case 0x75: case 0x76: case 0x83: case 0x85: case 0x86:
    case 0x87: case 0x88: case 0x89: case 0x90: case 0x92:
    case 0x98: case 0x99:
        return 1;
    default:
        return 0;
    }
}

static int find_xu_unit(libusb_device_handle *handle, int interface_number,
                        uint8_t *unit) {
    for (uint8_t candidate = 3; candidate <= 4; ++candidate) {
        uint8_t raw[4] = {0x1f, 0x10, 0, 0xff};
        int set_result = xu_control(handle, interface_number, candidate,
                                    XU_ASIC_SELECTOR, 0, raw, sizeof(raw));
        int get_result = -1;
        if (set_result == (int)sizeof(raw)) {
            raw[3] = 0;
            get_result = xu_control(handle, interface_number, candidate,
                                    XU_ASIC_SELECTOR, 1, raw, sizeof(raw));
        }
        uint8_t value = get_result == (int)sizeof(raw) ? raw[2] : 0;
        if (value == 0xaa && get_result == (int)sizeof(raw)) {
            uint8_t id80f0 = 0;
            uint8_t id80f1 = 0;
            uint8_t id80f3 = 0;
            int r0 = asic_read(handle, interface_number, candidate, 0x80f0,
                               &id80f0);
            int r1 = asic_read(handle, interface_number, candidate, 0x80f1,
                               &id80f1);
            int r3 = asic_read(handle, interface_number, candidate, 0x80f3,
                               &id80f3);
            if (r0 == 0 && r1 == 0 && r3 == 0 &&
                (known_chip(id80f0) ||
                 (id80f0 == 0x00 && id80f1 == 0x02 && id80f3 == 0x24) ||
                 (id80f0 == 0x01 && id80f1 == 0x02 && id80f3 == 0x23) ||
                 (id80f0 == 0x04 && id80f1 == 0x02 && id80f3 == 0x24))) {
                value = id80f0;
            }
        }
        if (set_result == (int)sizeof(raw) &&
            get_result == (int)sizeof(raw) &&
            (known_chip(value) || value == 0x00 || value == 0x01 ||
             value == 0x04)) {
            *unit = candidate;
            return 0;
        }
    }
    return LIBUSB_ERROR_NOT_FOUND;
}

static int sf_xu6_write_config(libusb_device_handle *handle,
                               int interface_number, uint8_t unit,
                               uint32_t start_address, uint32_t data_size,
                               uint8_t buffer_size) {
    uint8_t data[64] = {0};
    data[0] = (uint8_t)(start_address & 0xff);
    data[1] = (uint8_t)((start_address >> 8) & 0xff);
    data[2] = (uint8_t)((start_address >> 16) & 0xff);
    data[3] = (uint8_t)((start_address >> 24) & 0xff);
    data[4] = 2u << 6;
    data[5] = buffer_size;
    uint32_t end_address = start_address + data_size;
    data[6] = (uint8_t)(end_address & 0xff);
    data[7] = (uint8_t)((end_address >> 8) & 0xff);
    data[8] = (uint8_t)((end_address >> 16) & 0xff);
    data[9] = (uint8_t)((end_address >> 24) & 0xff);
    data[10] = 0x0b;
    data[11] = 0x01;
    int result = xu_control(handle, interface_number, unit,
                            XU_FLASH_XU6_SELECTOR, 0, data, sizeof(data));
    return result == (int)sizeof(data) ? 0 :
           (result < 0 ? result : LIBUSB_ERROR_IO);
}

static int sf_xu6_stop(libusb_device_handle *handle, int interface_number,
                       uint8_t unit) {
    for (uint16_t i = 0; i < 4; ++i) {
        int result = asic_write(handle, interface_number, unit,
                                SF_XU64_RW_START_ADDR + i, 0);
        if (result != 0) {
            return result;
        }
        result = asic_write(handle, interface_number, unit,
                            SF_XU64_RW_END_ADDR + i, 0);
        if (result != 0) {
            return result;
        }
    }
    return asic_write(handle, interface_number, unit, SF_XU64_EU_ADDR, 0);
}

static int sf_xu6_read_block(libusb_device_handle *handle, int interface_number,
                             uint8_t unit, uint32_t address, uint8_t *out,
                             uint8_t length) {
    if (length == 0 || length > 64 || address > FLASH_MAX_ADDRESS ||
        address + length < address) {
        return LIBUSB_ERROR_INVALID_PARAM;
    }
    int result = sf_xu6_stop(handle, interface_number, unit);
    if (result != 0) {
        return result;
    }
    result = sf_xu6_write_config(handle, interface_number, unit, address,
                                 length, length);
    if (result != 0) {
        return result;
    }
    uint8_t data[64] = {0};
    result = xu_control(handle, interface_number, unit,
                        XU_FLASH_XU6_SELECTOR, 1, data, sizeof(data));
    if (result != (int)sizeof(data)) {
        return result < 0 ? result : LIBUSB_ERROR_IO;
    }
    memcpy(out, data, length);
    return 0;
}

static int sf_xu6_read(libusb_device_handle *handle, int interface_number,
                       uint8_t unit, uint32_t address, uint8_t *out,
                       size_t length) {
    while (length > 0) {
        uint8_t chunk = length > 64 ? 64 : (uint8_t)length;
        int result = sf_xu6_read_block(handle, interface_number, unit, address,
                                       out, chunk);
        if (result != 0) {
            return result;
        }
        address += chunk;
        out += chunk;
        length -= chunk;
    }
    return 0;
}

static int sf_xu6_prepare_290(libusb_device_handle *handle,
                              int interface_number, uint8_t unit) {
    int result = asic_write(handle, interface_number, unit, 0x8045, 10);
    if (result != 0) {
        return result;
    }
    return asic_write(handle, interface_number, unit, 0x8e10, 1);
}

static void sleep_for_probe(size_t attempt) {
    useconds_t delay = FLASH_RETRY_DELAY_US *
                       (useconds_t)(attempt + 1U);
    usleep(delay);
}

static void decode_flash_text(const uint8_t *raw, size_t length,
                              char *out, size_t out_size) {
    size_t used = 0;
    if (out_size == 0) {
        return;
    }
    for (size_t i = 0; i < length && used + 1 < out_size; ++i) {
        if (raw[i] == 0 || raw[i] == 0xff) {
            break;
        }
        out[used++] = isprint(raw[i]) ? (char)raw[i] : '.';
    }
    while (used > 0 && isspace((unsigned char)out[used - 1])) {
        --used;
    }
    out[used] = '\0';
}

static void decode_flash_serial(const uint8_t *raw, size_t length,
                                char *out, size_t out_size) {
    size_t used = 0;
    if (out_size == 0) {
        return;
    }
    /* Sonix parameter-table strings use the same UTF-16LE-like layout as
     * the vendor SDK: length in bytes at [0], characters at [2], [4]... . */
    if (length >= 2 && raw[0] >= 2 && raw[0] <= length * 2 &&
        (raw[0] & 1u) == 0) {
        size_t chars = (raw[0] - 2) / 2;
        for (size_t index = 0; index < chars &&
             2 + index * 2 < length && used + 1 < out_size; ++index) {
            uint8_t value = raw[2 + index * 2];
            if (value == 0 || value == 0xff) {
                break;
            }
            out[used++] = isprint(value) ? (char)value : '.';
        }
    } else {
        decode_flash_text(raw, length, out, out_size);
        return;
    }
    while (used > 0 && isspace((unsigned char)out[used - 1])) {
        --used;
    }
    out[used] = '\0';
}

static void reset_flash_identity(camera_candidate_t *candidate) {
    memset(candidate->device_type, 0, sizeof(candidate->device_type));
    memset(candidate->side, 0, sizeof(candidate->side));
    memset(candidate->flash_serial, 0, sizeof(candidate->flash_serial));
    memset(candidate->fx_params, 0, sizeof(candidate->fx_params));
    memset(candidate->fy_params, 0, sizeof(candidate->fy_params));
    memset(candidate->fz_params, 0, sizeof(candidate->fz_params));
    memset(candidate->fw_params, 0, sizeof(candidate->fw_params));
    memset(candidate->ft_params, 0, sizeof(candidate->ft_params));
    candidate->flash_ok = 0;
    candidate->flash_params_ok = 0;
    candidate->error[0] = '\0';
}

static int read_flash_identity_once(camera_candidate_t *candidate,
                                    libusb_device_handle *handle,
                                    int read_calibration) {
    uint8_t unit = 0;
    int result = find_xu_unit(handle, candidate->control_interface, &unit);
    if (result != 0) {
        snprintf(candidate->error, sizeof(candidate->error),
                 "XU unit: %s", usb_error_text(result));
        return result;
    }
    result = sf_xu6_prepare_290(handle, candidate->control_interface, unit);
    if (result != 0) {
        snprintf(candidate->error, sizeof(candidate->error),
                 "XU6 prepare: %s", usb_error_text(result));
        return result;
    }

    char params[5][MAX_TEXT];
    memset(params, 0, sizeof(params));
    for (size_t index = 0; index < 5; ++index) {
        uint8_t raw[8] = {0};
        result = sf_xu6_read(handle, candidate->control_interface, unit,
                             0x45000 + (uint32_t)(index * sizeof(raw)), raw,
                             sizeof(raw));
        if (result != 0) {
            snprintf(candidate->error, sizeof(candidate->error),
                     "Flash device_params[%zu]: %s", index,
                     usb_error_text(result));
            return result;
        }
        decode_flash_text(raw, sizeof(raw), params[index], sizeof(params[index]));
    }
    copy_text(candidate->device_type, sizeof(candidate->device_type), params[0]);
    copy_text(candidate->side, sizeof(candidate->side), params[1]);
    copy_text(candidate->flash_serial, sizeof(candidate->flash_serial), params[2]);
    if (!(strcmp(candidate->device_type, "planar") == 0 ||
          strcmp(candidate->device_type, "curved") == 0 ||
          strcmp(candidate->device_type, "bevel") == 0)) {
        char message[MAX_TEXT + 32];
        snprintf(message, sizeof(message), "Flash device_type is %s",
                 candidate->device_type[0] ? candidate->device_type : "empty");
        copy_text(candidate->error, sizeof(candidate->error), message);
        return LIBUSB_ERROR_INVALID_PARAM;
    }
    uint32_t parameter_start = 0x8000;
    uint8_t table[0x2b] = {0};
    int table_result = sf_xu6_read(handle, candidate->control_interface,
                                   unit, 0x160, table, sizeof(table));
    if (table_result != 0) {
        snprintf(candidate->error, sizeof(candidate->error),
                 "Flash parameter_table: %s", usb_error_text(table_result));
        return table_result;
    }
    if (table[0x0f] == 0xff) {
        snprintf(candidate->error, sizeof(candidate->error),
                 "Flash parameter start address is blank");
        return LIBUSB_ERROR_INVALID_PARAM;
    }
        parameter_start = ((uint32_t)table[0x0f] << 24) |
                          ((uint32_t)table[0x10] << 16) |
                          ((uint32_t)table[0x11] << 8) | table[0x12];

    uint8_t serial_raw[64] = {0};
    int serial_result = sf_xu6_read(handle, candidate->control_interface,
                                    unit, parameter_start + 0xc0, serial_raw,
                                    sizeof(serial_raw));
    if (serial_result != 0) {
        snprintf(candidate->error, sizeof(candidate->error),
                 "Flash device_serial: %s", usb_error_text(serial_result));
        return serial_result;
    }
    char flash_serial[MAX_TEXT] = {0};
    decode_flash_serial(serial_raw, sizeof(serial_raw), flash_serial,
                        sizeof(flash_serial));
    if (flash_serial[0] == '\0') {
        snprintf(candidate->error, sizeof(candidate->error),
                 "Flash device_serial is empty");
        return LIBUSB_ERROR_INVALID_PARAM;
    }
    copy_text(candidate->flash_serial, sizeof(candidate->flash_serial),
              flash_serial);
    for (char *cursor = candidate->side; *cursor != '\0'; ++cursor) {
        *cursor = (char)tolower((unsigned char)*cursor);
    }
    if (strcmp(candidate->side, "left") != 0 &&
        strcmp(candidate->side, "right") != 0) {
        snprintf(candidate->error, sizeof(candidate->error),
                 "Flash side is %s", candidate->side[0] ? candidate->side : "empty");
        return LIBUSB_ERROR_INVALID_PARAM;
    }
    candidate->flash_ok = 1;
    if (!read_calibration) {
        return 0;
    }

    struct {
        uint32_t address;
        double *values;
        size_t count;
        const char *name;
    } calibration[] = {
        {0x41000, candidate->fx_params, 5, "fx"},
        {0x42000, candidate->fy_params, 5, "fy"},
        {0x43000, candidate->fz_params, 21, "fz"},
        {0x47000, candidate->ft_params, 20, "ft"},
        {0x44000, candidate->fw_params, 9, "fw"},
    };
    for (size_t table_index = 0;
         table_index < sizeof(calibration) / sizeof(calibration[0]);
         ++table_index) {
        /* Each XU6 read pays the setup cost of stop/config/control transfers.
         * Read each contiguous calibration table in 64-byte blocks instead
         * of issuing one transfer per double. */
        uint8_t raw[21 * sizeof(double)] = {0};
        size_t raw_size = calibration[table_index].count * sizeof(double);
        result = sf_xu6_read(
            handle, candidate->control_interface, unit,
            calibration[table_index].address, raw, raw_size);
        if (result != 0) {
            snprintf(candidate->error, sizeof(candidate->error),
                     "Flash %s: %s", calibration[table_index].name,
                     usb_error_text(result));
            return result;
        }
        for (size_t value_index = 0;
             value_index < calibration[table_index].count; ++value_index) {
            const uint8_t *slot = raw + value_index * sizeof(double);
            int erased = 1;
            for (size_t byte = 0; byte < sizeof(double); ++byte) {
                if (slot[byte] != 0xff) erased = 0;
            }
            if (erased && (strcmp(calibration[table_index].name, "ft") == 0 ||
                           (strcmp(calibration[table_index].name, "fz") == 0 && value_index >= 8))) {
                /* Match 0902 _default_force_calibration_params for erased
                 * extension slots only. Required coefficients and I/O errors
                 * remain fail-closed. FT's first/fourth defaults are 1, not 0. */
                calibration[table_index].values[value_index] =
                    (strcmp(calibration[table_index].name, "ft") == 0 &&
                     (value_index == 0 || value_index == 3)) ? 1.0 : 0.0;
                continue;
            }
            memcpy(&calibration[table_index].values[value_index], slot,
                   sizeof(double));
            if (!isfinite(calibration[table_index].values[value_index]) &&
                !(strcmp(calibration[table_index].name, "fw") == 0 &&
                  value_index >= 5)) {
                snprintf(candidate->error, sizeof(candidate->error),
                         "Flash %s[%zu] is not finite",
                         calibration[table_index].name, value_index);
                return LIBUSB_ERROR_INVALID_PARAM;
            }
        }
    }
    candidate->flash_params_ok = 1;
    candidate->flash_ok = 1;
    return 0;
}

static int flash_double_equal(double first, double second) {
    if (isnan(first) && isnan(second)) {
        return 1;
    }
    return first == second;
}

static int flash_double_array_equal(const double *first, const double *second,
                                    size_t count) {
    for (size_t index = 0; index < count; ++index) {
        if (!flash_double_equal(first[index], second[index])) {
            return 0;
        }
    }
    return 1;
}

static int flash_readout_equal(const camera_candidate_t *first,
                               const camera_candidate_t *second,
                               int compare_calibration) {
    if (strcmp(first->device_type, second->device_type) != 0 ||
        strcmp(first->side, second->side) != 0 ||
        strcmp(first->flash_serial, second->flash_serial) != 0) {
        return 0;
    }
    if (!compare_calibration) {
        return 1;
    }
    return
           strcmp(first->side, second->side) == 0 &&
           strcmp(first->flash_serial, second->flash_serial) == 0 &&
           flash_double_array_equal(first->fx_params, second->fx_params, 5) &&
           flash_double_array_equal(first->fy_params, second->fy_params, 5) &&
           flash_double_array_equal(first->fz_params, second->fz_params, 21) &&
           flash_double_array_equal(first->ft_params, second->ft_params, 20) &&
           flash_double_array_equal(first->fw_params, second->fw_params, 9);
}

static int read_flash_identity_verified(camera_candidate_t *candidate,
                                        libusb_device_handle *handle,
                                        int read_calibration) {
    char last_error[MAX_TEXT] = "";
    int last_result = LIBUSB_ERROR_IO;

    for (int attempt = 0; attempt < FLASH_PROBE_ATTEMPTS; ++attempt) {
        camera_candidate_t verified;
        int result;

        reset_flash_identity(candidate);
        result = read_flash_identity_once(candidate, handle,
                                          read_calibration);
        if (result != 0) {
            copy_text(last_error, sizeof(last_error), candidate->error);
            last_result = result;
            if (attempt + 1 < FLASH_PROBE_ATTEMPTS) {
                sleep_for_probe((size_t)attempt);
            }
            continue;
        }

        /* A single XU6 transfer can complete while its small DMA/control
         * state was disturbed by USB arbitration.  Read the immutable tables
         * into an independent structure and require bit-stable identity and
         * calibration values before accepting them as complete. */
        memset(&verified, 0, sizeof(verified));
        verified.control_interface = candidate->control_interface;
        result = read_flash_identity_once(&verified, handle,
                                          read_calibration);
        if (result == 0) {
            if (flash_readout_equal(candidate, &verified,
                                    read_calibration)) {
                return 0;
            }
            snprintf(candidate->error, sizeof(candidate->error),
                     "Flash readback verification mismatch");
        } else {
            char message[MAX_TEXT + 40];
            snprintf(message, sizeof(message),
                     "Flash readback verification: %s", verified.error);
            copy_text(candidate->error, sizeof(candidate->error), message);
        }
        copy_text(last_error, sizeof(last_error), candidate->error);
        last_result = result == 0 ? LIBUSB_ERROR_IO : result;
        if (attempt + 1 < FLASH_PROBE_ATTEMPTS) {
            sleep_for_probe((size_t)attempt);
        }
    }

    candidate->error[0] = '\0';
    copy_text(candidate->error, sizeof(candidate->error),
              last_error[0] ? last_error : "Flash probe exhausted retries");
    candidate->flash_ok = 0;
    candidate->flash_params_ok = 0;
    return last_result;
}

static int inspect_candidate(camera_candidate_t *candidate,
                             flash_mode_t flash_mode) {
    const mode_profile_t *profile = profile_for(candidate->vid, candidate->pid);
    if (candidate->usb_device == NULL) {
        copy_text(candidate->error, sizeof(candidate->error),
                  "libusb device disappeared");
        candidate->mode_ok = 0;
        return -1;
    }
    candidate->control_interface = find_control_interface(candidate->usb_device);
    if (candidate->control_interface < 0) {
        snprintf(candidate->error, sizeof(candidate->error),
                 "control interface: %s", usb_error_text(candidate->control_interface));
        candidate->mode_ok = 0;
        return -1;
    }
    int descriptor_interface = -1;
    int descriptor_alt = -1;
    unsigned int descriptor_payload = 0;
    int descriptor_result = find_stream_mode(
        candidate->usb_device, profile->expected_altsetting,
        (unsigned int)profile->forced_payload, &descriptor_interface,
        &descriptor_alt, &descriptor_payload);
    if (descriptor_result != 0) {
        snprintf(candidate->error, sizeof(candidate->error),
                 "stream altsetting: %s", usb_error_text(descriptor_result));
        candidate->mode_ok = 0;
        return -1;
    }
    candidate->streaming_interface = descriptor_interface;
    candidate->altsetting = descriptor_alt;
    candidate->descriptor_payload = descriptor_payload;

    uvc_device_handle_t *uvc_handle = NULL;
    uvc_error_t uvc_result = uvc_open(candidate->uvc_device, &uvc_handle);
    if (uvc_result != UVC_SUCCESS || uvc_handle == NULL) {
        snprintf(candidate->error, sizeof(candidate->error),
                 "uvc_open: %s", uvc_strerror(uvc_result));
        candidate->mode_ok = -1;
        return -1;
    }
    candidate->opened_ok = 1;
    libusb_device_handle *usb_handle = uvc_get_libusb_handle(uvc_handle);

    uvc_stream_ctrl_t control;
    memset(&control, 0, sizeof(control));
    uvc_result = uvc_get_stream_ctrl_format_size(
        uvc_handle, &control, UVC_FRAME_FORMAT_MJPEG,
        profile->width, profile->height, profile->fps);
    if (uvc_result != UVC_SUCCESS) {
        snprintf(candidate->error, sizeof(candidate->error),
                 "MJPEG %dx%d@%d: %s", profile->width, profile->height,
                 profile->fps, uvc_strerror(uvc_result));
        candidate->mode_ok = 0;
        uvc_close(uvc_handle);
        return -1;
    }
    candidate->streaming_interface = control.bInterfaceNumber;
    candidate->negotiated_payload = control.dwMaxPayloadTransferSize;
    candidate->mode_ok = candidate->altsetting == profile->expected_altsetting;
    int result = 0;
    if (!candidate->mode_ok) {
        snprintf(candidate->error, sizeof(candidate->error),
                 "alt=%d expected=%d", candidate->altsetting,
                 profile->expected_altsetting);
        uvc_close(uvc_handle);
        return -1;
    }
    if ((flash_mode == FLASH_MODE_FULL ||
         flash_mode == FLASH_MODE_IDENTITY) &&
        candidate->vid == SIGHTAC_VID &&
        candidate->pid == SIGHTAC_PID) {
        result = read_flash_identity_verified(
            candidate, usb_handle, flash_mode == FLASH_MODE_FULL);
        if (result != 0) {
            uvc_close(uvc_handle);
            return -1;
        }
    }
    uvc_close(uvc_handle);
    return 0;
}

static int same_outer_parent(const camera_candidate_t *left,
                             const camera_candidate_t *right) {
    char left_parent[MAX_PORT_PATH];
    char right_parent[MAX_PORT_PATH];
    const char *left_dot = strrchr(left->port, '.');
    const char *right_dot = strrchr(right->port, '.');
    if (left_dot == NULL || right_dot == NULL || left->bus != right->bus) {
        return 0;
    }
    size_t left_size = (size_t)(left_dot - left->port);
    size_t right_size = (size_t)(right_dot - right->port);
    if (left_size == 0 || left_size >= sizeof(left_parent) ||
        right_size == 0 || right_size >= sizeof(right_parent)) {
        return 0;
    }
    memcpy(left_parent, left->port, left_size);
    left_parent[left_size] = '\0';
    memcpy(right_parent, right->port, right_size);
    right_parent[right_size] = '\0';
    return strcmp(left_parent, right_parent) == 0;
}

static int port_has_prefix(const char *port, const char *prefix) {
    size_t prefix_size = strlen(prefix);
    return strncmp(port, prefix, prefix_size) == 0 &&
           port[prefix_size] == '.';
}

static int build_groups(const camera_candidate_t *candidates, size_t count,
                        camera_group_t *groups, size_t *group_count) {
    int used[MAX_CANDIDATES] = {0};
    size_t output_count = 0;
    for (size_t i = 0; i < count; ++i) {
        if (candidates[i].vid != SIGHTAC_VID || used[i]) {
            continue;
        }
        int matches[2] = {-1, -1};
        size_t match_count = 0;
        for (size_t j = i; j < count; ++j) {
            if (candidates[j].vid == SIGHTAC_VID &&
                same_outer_parent(&candidates[i], &candidates[j])) {
                if (match_count < 2) {
                    matches[match_count] = (int)j;
                }
                ++match_count;
            }
        }
        if (match_count != 2) {
            fprintf(stderr, "[DISCOVERY] ambiguous Sightac group bus=%d parent of %s: %zu node(s)\n",
                    candidates[i].bus, candidates[i].port, match_count);
            continue;
        }
        char outer_path[MAX_PORT_PATH];
        const char *dot = strrchr(candidates[matches[0]].port, '.');
        size_t outer_size = (size_t)(dot - candidates[matches[0]].port);
        if (outer_size == 0 || outer_size >= sizeof(outer_path)) {
            continue;
        }
        memcpy(outer_path, candidates[matches[0]].port, outer_size);
        outer_path[outer_size] = '\0';

        int decxin = -1;
        size_t decxin_count = 0;
        for (size_t j = 0; j < count; ++j) {
            if (candidates[j].vid == DECXIN_VID &&
                candidates[j].pid == DECXIN_PID &&
                candidates[j].bus == candidates[i].bus &&
                port_has_prefix(candidates[j].port, outer_path)) {
                decxin = (int)j;
                ++decxin_count;
            }
        }
        if (decxin_count != 1 || output_count >= MAX_GROUPS) {
            fprintf(stderr, "[DISCOVERY] bus=%d outer=%s DECXIN match count=%zu; group rejected\n",
                    candidates[i].bus, outer_path, decxin_count);
            continue;
        }
        groups[output_count].sightac[0] = matches[0];
        groups[output_count].sightac[1] = matches[1];
        groups[output_count].decxin = decxin;
        groups[output_count].bus = candidates[i].bus;
        copy_text(groups[output_count].outer_path,
                  sizeof(groups[output_count].outer_path), outer_path);
        used[matches[0]] = 1;
        used[matches[1]] = 1;
        used[decxin] = 1;
        ++output_count;
    }
    *group_count = output_count;
    for (size_t i = 0; i < count; ++i) {
        if (!used[i]) {
            fprintf(stderr, "[DISCOVERY] ungrouped %s %04x:%04x bus=%d port=%s\n",
                    profile_for(candidates[i].vid, candidates[i].pid)->kind,
                    candidates[i].vid, candidates[i].pid, candidates[i].bus,
                    candidates[i].port);
        }
    }
    return output_count > 0 ? 0 : -1;
}

static const camera_candidate_t *candidate_by_side(
    const camera_candidate_t *candidates, const camera_group_t *group,
    const char *side) {
    for (size_t i = 0; i < 2; ++i) {
        const camera_candidate_t *candidate = &candidates[group->sightac[i]];
        if (strcmp(candidate->side, side) == 0) {
            return candidate;
        }
    }
    return NULL;
}

static int group_sides_ok(const camera_candidate_t *candidates,
                          const camera_group_t *group,
                          int require_calibration) {
    const camera_candidate_t *left = candidate_by_side(candidates, group, "left");
    const camera_candidate_t *right = candidate_by_side(candidates, group, "right");
    const camera_candidate_t *decxin = &candidates[group->decxin];
    return left != NULL && right != NULL && left != right &&
           candidates[group->sightac[0]].flash_ok &&
           candidates[group->sightac[1]].flash_ok &&
           (!require_calibration ||
            (candidates[group->sightac[0]].flash_params_ok &&
             candidates[group->sightac[1]].flash_params_ok)) &&
           left->mode_ok > 0 && right->mode_ok > 0 &&
           decxin->mode_ok > 0;
}

static void print_candidate(const camera_candidate_t *candidate,
                            flash_mode_t flash_mode) {
    const mode_profile_t *profile = profile_for(candidate->vid, candidate->pid);
    const char *mode_status = candidate->mode_ok > 0 ? "ok" :
                              (candidate->mode_ok < 0 ? "unknown" : "failed");
    fprintf(stderr,
            "[DISCOVERY] %-7s vidpid=%04x:%04x bus=%d port=%s usb_serial=%s "
            "control_if=%d stream_if=%d alt=%d descriptor_payload=%u "
            "negotiated_payload=%u mode=%s",
            profile->kind, candidate->vid, candidate->pid, candidate->bus,
            candidate->port, candidate->usb_serial[0] ? candidate->usb_serial : "<none>",
            candidate->control_interface, candidate->streaming_interface,
            candidate->altsetting, candidate->descriptor_payload,
            candidate->negotiated_payload, mode_status);
    if (candidate->vid == SIGHTAC_VID &&
        (flash_mode == FLASH_MODE_FULL ||
         flash_mode == FLASH_MODE_IDENTITY)) {
        const char *calibration = flash_mode == FLASH_MODE_IDENTITY
                                      ? "skipped"
                                      : (candidate->flash_params_ok
                                             ? "ok" : "failed");
        fprintf(stderr, " flash=%s calibration=%s side=%s serial=%s type=%s",
                candidate->flash_ok ? "ok" : "failed",
                calibration,
                candidate->side[0] ? candidate->side : "<none>",
                candidate->flash_serial[0] ? candidate->flash_serial : "<none>",
                candidate->device_type[0] ? candidate->device_type : "<none>");
    }
    if (candidate->error[0]) {
        fprintf(stderr, " error=%s", candidate->error);
    }
    fputc('\n', stderr);
}

static void write_json_candidate(FILE *out,
                                 const camera_candidate_t *candidate,
                                 int include_flash,
                                 int include_calibration) {
    const mode_profile_t *profile = profile_for(candidate->vid, candidate->pid);
    fprintf(out,
            "{\"kind\":");
    write_json_string(out, profile->kind);
    fprintf(out,
            ",\"vid\":%d,\"pid\":%d,\"bus\":%d,\"port\":",
            candidate->vid, candidate->pid, candidate->bus);
    write_json_string(out, candidate->port);
    fprintf(out,
            ",\"usb_serial\":");
    write_json_string(out, candidate->usb_serial);
    fprintf(out,
            ",\"control_interface\":%d,\"streaming_interface\":%d,"
            "\"altsetting\":%d,\"descriptor_payload\":%u,"
            "\"negotiated_payload\":%u,\"mode_ok\":%s",
            candidate->control_interface, candidate->streaming_interface,
            candidate->altsetting, candidate->descriptor_payload,
            candidate->negotiated_payload,
            candidate->mode_ok > 0 ? "true" : "false");
    if (include_flash) {
        fprintf(out, ",\"flash_ok\":%s,\"flash_params_ok\":%s,\"side\":",
                candidate->flash_ok ? "true" : "false",
                candidate->flash_params_ok ? "true" : "false");
        write_json_string(out, candidate->side);
        fprintf(out, ",\"flash_serial\":");
        write_json_string(out, candidate->flash_serial);
        fprintf(out, ",\"device_type\":");
        write_json_string(out, candidate->device_type);
        if (!include_calibration) {
            goto candidate_error;
        }
        fprintf(out, ",\"flash_params\":{\"device_type\":");
        write_json_string(out, candidate->device_type);
        fprintf(out, ",\"fx_params\":[");
        for (size_t i = 0; i < 5; ++i) {
            if (i != 0) fputc(',', out);
            fprintf(out, "%.17g", candidate->fx_params[i]);
        }
        fprintf(out, "],\"fy_params\":[");
        for (size_t i = 0; i < 5; ++i) {
            if (i != 0) fputc(',', out);
            fprintf(out, "%.17g", candidate->fy_params[i]);
        }
        fprintf(out, "],\"fz_params\":[");
        for (size_t i = 0; i < 21; ++i) {
            if (i != 0) fputc(',', out);
            fprintf(out, "%.17g", candidate->fz_params[i]);
        }
        fprintf(out, "],\"ft_params\":[");
        for (size_t i = 0; i < 20; ++i) {
            if (i != 0) fputc(',', out);
            fprintf(out, "%.17g", candidate->ft_params[i]);
        }
        fprintf(out, "],\"fw_params\":[");
        for (size_t i = 0; i < 9; ++i) {
            if (i != 0) fputc(',', out);
            if (isfinite(candidate->fw_params[i])) {
                fprintf(out, "%.17g", candidate->fw_params[i]);
            } else {
                /* The bevel firmware reserves fw[5..8]; current ROI/line
                 * code consumes only fw[0..4].  Keep the JSON valid. */
                fputs("null", out);
            }
        }
        fprintf(out, "],\"flash_state\":\"programmed\","
                     "\"calibration_source\":\"hardware\"}");
    }
candidate_error:
    fprintf(out, ",\"error\":");
    write_json_string(out, candidate->error);
    fputc('}', out);
}

static void write_camera_section(FILE *out, const camera_candidate_t *candidate,
                                 int group_number, const char *name,
                                 const char *socket_name) {
    const mode_profile_t *profile = profile_for(candidate->vid, candidate->pid);
    fprintf(out,
            "[%s]\n"
            "vid=0x%04x\n"
            "pid=0x%04x\n"
            "usb_bus=%d\n"
            "usb_port_path=%s\n"
            "streaming_interface=%d\n"
            "forced_altsetting=%d\n"
            "forced_payload=%d\n"
            "width=%d\n"
            "height=%d\n"
            "fps=%d\n"
            "format=MJPEG\n"
            "socket_path=/tmp/ksq-camera-service-unit-%d-%s.sock\n\n",
            name, candidate->vid, candidate->pid, candidate->bus, candidate->port,
            candidate->streaming_interface, candidate->altsetting,
            profile->forced_payload, profile->width, profile->height,
            profile->fps, group_number, socket_name);
}

static int write_generated_config(FILE *out, const camera_candidate_t *candidates,
                                  const camera_group_t *groups,
                                  size_t group_count) {
    for (size_t index = 0; index < group_count; ++index) {
        if (!group_sides_ok(candidates, &groups[index], 1)) {
            fprintf(stderr, "[DISCOVERY] group %zu rejected: mode or left/right Flash identity is incomplete\n",
                    index + 1);
            return -1;
        }
    }
    fprintf(out,
            "# Generated by discover-uvc-config.\n"
            "# Scope: Sightac + DECXIN only; Fays is intentionally excluded.\n"
            "# Bus/port/interface/alt are from the current libusb/libuvc scan.\n\n");
    for (size_t index = 0; index < group_count; ++index) {
        const camera_candidate_t *left =
            candidate_by_side(candidates, &groups[index], "left");
        const camera_candidate_t *right =
            candidate_by_side(candidates, &groups[index], "right");
        const camera_candidate_t *decxin = &candidates[groups[index].decxin];
        char name[64];
        snprintf(name, sizeof(name), "unit_%zu_sightac_left", index + 1);
        write_camera_section(out, left, (int)(index + 1), name,
                             "sightac-left");
        snprintf(name, sizeof(name), "unit_%zu_sightac_right", index + 1);
        write_camera_section(out, right, (int)(index + 1), name,
                             "sightac-right");
        snprintf(name, sizeof(name), "unit_%zu_decxin", index + 1);
        write_camera_section(out, decxin, (int)(index + 1), name, "decxin");
    }
    return 0;
}

static int write_json_report(FILE *out, const camera_candidate_t *candidates,
                             const camera_group_t *groups, size_t group_count,
                             flash_mode_t flash_mode) {
    const int include_flash = flash_mode == FLASH_MODE_FULL ||
                              flash_mode == FLASH_MODE_IDENTITY;
    const int include_calibration = flash_mode == FLASH_MODE_FULL;
    const int require_calibration = flash_mode == FLASH_MODE_FULL;
    fprintf(out, "{\"groups\":[");
    for (size_t index = 0; index < group_count; ++index) {
        const camera_group_t *group = &groups[index];
        if (index != 0) {
            fputc(',', out);
        }
        fprintf(out, "{\"bus\":%d,\"outer_path\":", group->bus);
        write_json_string(out, group->outer_path);
        fprintf(out, ",\"sightac\":[");
        write_json_candidate(out, &candidates[group->sightac[0]],
                             include_flash, include_calibration);
        fputc(',', out);
        write_json_candidate(out, &candidates[group->sightac[1]],
                             include_flash, include_calibration);
        fprintf(out, "],\"decxin\":");
        write_json_candidate(out, &candidates[group->decxin], 0, 0);
        fprintf(out, ",\"valid\":%s}",
                group_sides_ok(candidates, group,
                               require_calibration) ? "true" : "false");
    }
    fprintf(out, "]}\n");
    return ferror(out) ? -1 : 0;
}

static void print_usage(const char *program) {
    fprintf(stderr,
            "usage: %s [--emit-config PATH] [--json] [--skip-flash|--identity-only|--topology-only] [--bus N --port-prefix P]\n"
            "  default: scan Sightac/DECXIN and read Sightac Flash side/serial\n"
            "  --emit-config PATH: write a generated camera-service INI\n"
            "  --json: write machine-readable UVC groups to stdout\n"
            "  --skip-flash: topology/mode report only; never emits a usable INI\n"
            "  --identity-only: read Flash side/serial without calibration tables\n"
            "  --topology-only: USB topology only; does not open UVC streams\n"
            "  --bus N --port-prefix P: inspect only this physical USB group\n",
            program);
}

int main(int argc, char **argv) {
    const char *config_path = NULL;
    int json_output = 0;
    flash_mode_t flash_mode = FLASH_MODE_FULL;
    int only_bus = -1;
    const char *only_port = NULL;
    for (int index = 1; index < argc; ++index) {
        if (strcmp(argv[index], "--emit-config") == 0 && index + 1 < argc) {
            config_path = argv[++index];
        } else if (strcmp(argv[index], "--bus") == 0 && index + 1 < argc) {
            const char *value = argv[++index];
            if (!*value || strlen(value) > 3 || strspn(value, "0123456789") != strlen(value)) return 2;
            only_bus = atoi(value);
            if (only_bus <= 0 || only_bus > 255) return 2;
        } else if (strcmp(argv[index], "--port-prefix") == 0 && index + 1 < argc) {
            only_port = argv[++index];
            if (!*only_port || strspn(only_port, "0123456789.") != strlen(only_port)) return 2;
        } else if (strcmp(argv[index], "--json") == 0) {
            json_output = 1;
        } else if (strcmp(argv[index], "--skip-flash") == 0) {
            flash_mode = FLASH_MODE_NONE;
        } else if (strcmp(argv[index], "--identity-only") == 0) {
            flash_mode = FLASH_MODE_IDENTITY;
        } else if (strcmp(argv[index], "--topology-only") == 0) {
            flash_mode = FLASH_MODE_TOPOLOGY_ONLY;
        } else {
            print_usage(argv[0]);
            return 2;
        }
    }

    if ((only_bus > 0) != (only_port != NULL)) return 2;

    uvc_context_t *uvc_context = NULL;
    uvc_error_t uvc_result = uvc_init(&uvc_context, NULL);
    if (uvc_result != UVC_SUCCESS) {
        fprintf(stderr, "[DISCOVERY] uvc_init failed: %s\n",
                uvc_strerror(uvc_result));
        return 1;
    }
    uvc_device_t **devices = NULL;
    uvc_result = uvc_get_device_list(uvc_context, &devices);
    if (uvc_result != UVC_SUCCESS) {
        fprintf(stderr, "[DISCOVERY] uvc_get_device_list failed: %s\n",
                uvc_strerror(uvc_result));
        uvc_exit(uvc_context);
        return 1;
    }
    libusb_context *usb_context = NULL;
    int usb_result = libusb_init(&usb_context);
    if (usb_result != 0) {
        fprintf(stderr, "[DISCOVERY] libusb_init failed: %s\n",
                usb_error_text(usb_result));
        uvc_free_device_list(devices, 1);
        uvc_exit(uvc_context);
        return 1;
    }
    libusb_device **raw_devices = NULL;
    ssize_t raw_count = libusb_get_device_list(usb_context, &raw_devices);
    if (raw_count < 0) {
        fprintf(stderr, "[DISCOVERY] libusb_get_device_list failed: %s\n",
                usb_error_text((int)raw_count));
        libusb_exit(usb_context);
        uvc_free_device_list(devices, 1);
        uvc_exit(uvc_context);
        return 1;
    }

    camera_candidate_t candidates[MAX_CANDIDATES];
    memset(candidates, 0, sizeof(candidates));
    size_t candidate_count = 0;
    for (size_t index = 0; devices[index] != NULL; ++index) {
        /* Filter physical scope BEFORE descriptors/Flash/open operations.
         * A second rig must never open cameras already streaming for the first. */
        if (only_port) {
            char port[MAX_TEXT] = {0};
            size_t length = strlen(only_port);
            if ((int)uvc_get_bus_number(devices[index]) != only_bus ||
                format_uvc_port_path(devices[index], port, sizeof(port)) != 0 ||
                strncmp(port, only_port, length) != 0 ||
                (port[length] != '\0' && port[length] != '.')) continue;
        }
        uvc_device_descriptor_t *descriptor = NULL;
        if (uvc_get_device_descriptor(devices[index], &descriptor) != UVC_SUCCESS ||
            descriptor == NULL) {
            continue;
        }
        const mode_profile_t *profile =
            profile_for(descriptor->idVendor, descriptor->idProduct);
        if (profile == NULL || candidate_count >= MAX_CANDIDATES) {
            uvc_free_device_descriptor(descriptor);
            continue;
        }
        camera_candidate_t *candidate = &candidates[candidate_count++];
        candidate->uvc_device = devices[index];
        candidate->control_interface = -1;
        candidate->streaming_interface = -1;
        candidate->altsetting = -1;
        candidate->mode_ok = -1;
        candidate->vid = descriptor->idVendor;
        candidate->pid = descriptor->idProduct;
        copy_text(candidate->usb_serial, sizeof(candidate->usb_serial),
                  descriptor->serialNumber);
        if (format_uvc_port_path(devices[index], candidate->port,
                                 sizeof(candidate->port)) != 0) {
            copy_text(candidate->error, sizeof(candidate->error),
                      "cannot read USB port path");
        }
        candidate->bus = uvc_get_bus_number(devices[index]);
        candidate->usb_device = find_raw_device(
            raw_devices, raw_count, candidate->vid, candidate->pid,
            candidate->bus, candidate->port);
        int status = 0;
        if (flash_mode != FLASH_MODE_TOPOLOGY_ONLY) {
            status = inspect_candidate(candidate, flash_mode);
        }
        if (status != 0 && candidate->error[0] == '\0') {
            copy_text(candidate->error, sizeof(candidate->error), "inspection failed");
        }
        print_candidate(candidate, flash_mode);
        uvc_free_device_descriptor(descriptor);
    }

    camera_group_t groups[MAX_GROUPS];
    memset(groups, 0, sizeof(groups));
    size_t group_count = 0;
    if (candidate_count == 0 ||
        build_groups(candidates, candidate_count, groups, &group_count) != 0) {
        fprintf(stderr, "[DISCOVERY] no complete Sightac + DECXIN topology group\n");
        libusb_free_device_list(raw_devices, 1);
        libusb_exit(usb_context);
        uvc_free_device_list(devices, 1);
        uvc_exit(uvc_context);
        return 3;
    }
    fprintf(stderr, "[DISCOVERY] complete group(s)=%zu\n", group_count);

    int result = 0;
    if (config_path != NULL && json_output) {
        fprintf(stderr, "[DISCOVERY] refusing --emit-config with --json\n");
        result = 2;
    } else if (json_output) {
        if (write_json_report(stdout, candidates, groups, group_count,
                              flash_mode) != 0) {
            result = 1;
        }
    } else if (config_path != NULL) {
        if (flash_mode != FLASH_MODE_FULL) {
            fprintf(stderr, "[DISCOVERY] refusing --emit-config without full Flash mode\n");
            result = 2;
        } else {
            char temporary_path[PATH_MAX];
            int path_result = snprintf(temporary_path, sizeof(temporary_path),
                                       "%s.tmp.%ld", config_path,
                                       (long)getpid());
            if (path_result < 0 || (size_t)path_result >= sizeof(temporary_path)) {
                fprintf(stderr, "[DISCOVERY] config path is too long: %s\n",
                        config_path);
                result = 1;
                goto cleanup;
            }
            FILE *out = fopen(temporary_path, "w");
            if (out == NULL) {
                fprintf(stderr, "[DISCOVERY] cannot write %s: %s\n",
                        config_path, strerror(errno));
                result = 1;
            } else {
                int write_result = write_generated_config(
                    out, candidates, groups, group_count);
                int close_result = fclose(out);
                if (write_result != 0 || close_result != 0 ||
                    rename(temporary_path, config_path) != 0) {
                    fprintf(stderr, "[DISCOVERY] failed to generate %s\n",
                            config_path);
                    unlink(temporary_path);
                    result = 1;
                } else {
                    fprintf(stderr, "[DISCOVERY] generated config: %s\n",
                            config_path);
                }
            }
        }
    } else if (flash_mode == FLASH_MODE_FULL) {
        if (write_generated_config(stdout, candidates, groups, group_count) != 0) {
            result = 1;
        }
    }
cleanup:
    libusb_free_device_list(raw_devices, 1);
    libusb_exit(usb_context);
    uvc_free_device_list(devices, 1);
    uvc_exit(uvc_context);
    return result;
}
