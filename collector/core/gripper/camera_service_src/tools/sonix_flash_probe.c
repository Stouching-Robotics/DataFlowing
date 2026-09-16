/*
 * Read-only Sonix XU/Serial-Flash probe.
 *
 * This is the libusb equivalent of the source SDK's
 * UVCIOC_CTRL_QUERY(UVC_SET_CUR/UVC_GET_CUR) path.  It never issues a
 * serial-flash write or erase command.  The SET_CUR requests below only set
 * the address for the following read, exactly as XU_ReadDataFormFlash().
 */
#include <libusb-1.0/libusb.h>

#include <ctype.h>
#include <errno.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define SONIX_VID 0x0c45
#define SONIX_PID 0x636f
#define XU_GET_CUR 0x81
#define XU_SET_CUR 0x01
#define XU_CTRL_GET 0xa1
#define XU_CTRL_SET 0x21
#define XU_ASIC_SELECTOR 0x01
#define XU_FLASH_SELECTOR 0x03
#define XU_FLASH_XU6_SELECTOR 0x06
#define CTRL_TIMEOUT_MS 2000
#define FLASH_MAX_ADDRESS 0x4ffff
#define SF_XU64_EU_ADDR 0x335
#define SF_XU64_RW_START_ADDR 0x337
#define SF_XU64_RW_END_ADDR 0x33b

static void print_hex(const uint8_t *data, size_t length) {
    for (size_t i = 0; i < length; ++i) {
        printf("%02x%s", data[i], (i + 1 == length) ? "" : " ");
    }
}

static void print_ascii(const uint8_t *data, size_t length) {
    putchar('"');
    for (size_t i = 0; i < length; ++i) {
        putchar(isprint(data[i]) ? (int)data[i] : '.');
    }
    puts("\"");
}

static int port_path(libusb_device *device, char *out, size_t out_size) {
    uint8_t ports[8];
    int count = libusb_get_port_numbers(device, ports, sizeof(ports));
    size_t used = 0;
    if (count < 0) {
        return count;
    }
    out[0] = '\0';
    for (int i = 0; i < count; ++i) {
        int written = snprintf(out + used, out_size - used, "%s%u",
                               i == 0 ? "" : ".", ports[i]);
        if (written < 0 || (size_t)written >= out_size - used) {
            return LIBUSB_ERROR_OVERFLOW;
        }
        used += (size_t)written;
    }
    return 0;
}

static int control_interface(libusb_device *device) {
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
    int result = xu_control(handle, interface_number, unit, XU_ASIC_SELECTOR,
                            0, data, sizeof(data));
    if (result != (int)sizeof(data)) {
        return result < 0 ? result : LIBUSB_ERROR_IO;
    }
    data[3] = 0;
    result = xu_control(handle, interface_number, unit, XU_ASIC_SELECTOR,
                        1, data, sizeof(data));
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
    int result = xu_control(handle, interface_number, unit, XU_ASIC_SELECTOR,
                            0, data, sizeof(data));
    if (result != (int)sizeof(data)) {
        return result < 0 ? result : LIBUSB_ERROR_IO;
    }
    return 0;
}

static int sf_xu6_write_config(libusb_device_handle *handle,
                               int interface_number, uint8_t unit,
                               uint32_t start_address, uint32_t data_size,
                               uint8_t buffer_size) {
    /* This is the packed XU6Data structure used by SetReadInfo_SFXU6(). */
    uint8_t data[64] = {0};
    data[0] = (uint8_t)(start_address & 0xff);
    data[1] = (uint8_t)((start_address >> 8) & 0xff);
    data[2] = (uint8_t)((start_address >> 16) & 0xff);
    data[3] = (uint8_t)((start_address >> 24) & 0xff);
    data[4] = 2u << 6; /* dummy write */
    data[5] = buffer_size;
    uint32_t end_address = start_address + data_size;
    data[6] = (uint8_t)(end_address & 0xff);
    data[7] = (uint8_t)((end_address >> 8) & 0xff);
    data[8] = (uint8_t)((end_address >> 16) & 0xff);
    data[9] = (uint8_t)((end_address >> 24) & 0xff);
    data[10] = 0x0b; /* SDK default read command */
    data[11] = 0x01; /* SF output mode */
    int result = xu_control(handle, interface_number, unit,
                            XU_FLASH_XU6_SELECTOR, 0, data, sizeof(data));
    if (result != (int)sizeof(data)) {
        return result < 0 ? result : LIBUSB_ERROR_IO;
    }
    return 0;
}

static int sf_xu6_stop(libusb_device_handle *handle, int interface_number,
                       uint8_t unit) {
    int result;
    for (uint16_t i = 0; i < 4; ++i) {
        result = asic_write(handle, interface_number, unit,
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
    /* SetSFControllerSCK() for ROM290 in the vendor source. */
    int result = asic_write(handle, interface_number, unit, 0x8045, 10);
    if (result != 0) {
        return result;
    }
    return asic_write(handle, interface_number, unit, 0x8e10, 1);
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
                        uint8_t *unit, uint8_t *chip) {
    /* The vendor source deliberately probes units 3 and 4 in this order. */
    for (uint8_t candidate = 3; candidate <= 4; ++candidate) {
        uint8_t value = 0;
        uint8_t raw[4] = {
            0x1f, 0x10, 0, 0xff
        };
        int set_result = xu_control(handle, interface_number, candidate,
                                    XU_ASIC_SELECTOR, 0, raw, sizeof(raw));
        int get_result = -1;
        if (set_result == (int)sizeof(raw)) {
            raw[3] = 0;
            get_result = xu_control(handle, interface_number, candidate,
                                    XU_ASIC_SELECTOR, 1, raw, sizeof(raw));
            if (get_result == (int)sizeof(raw)) {
                value = raw[2];
            }
        }
        fprintf(stderr, "  xu_probe unit=%u set=%d get=%d data=",
                candidate, set_result, get_result);
        print_hex(raw, sizeof(raw));
        fputc('\n', stderr);
        if (value == 0xaa && get_result == (int)sizeof(raw)) {
            uint8_t id80f0 = 0, id80f1 = 0, id80f3 = 0;
            int r0 = asic_read(handle, interface_number, candidate, 0x80f0,
                               &id80f0);
            int r1 = asic_read(handle, interface_number, candidate, 0x80f1,
                               &id80f1);
            int r3 = asic_read(handle, interface_number, candidate, 0x80f3,
                               &id80f3);
            fprintf(stderr, "  xu_probe fallback 80f0=%02x(%d) 80f1=%02x(%d) "
                            "80f3=%02x(%d)\n",
                    id80f0, r0, id80f1, r1, id80f3, r3);
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
            *chip = value;
            return 0;
        }
    }
    return LIBUSB_ERROR_NOT_FOUND;
}

static int flash_read_block(libusb_device_handle *handle, int interface_number,
                            uint8_t unit, uint32_t address, uint8_t *out,
                            uint8_t length) {
    if (length == 0 || length > 8 || address > FLASH_MAX_ADDRESS) {
        return LIBUSB_ERROR_INVALID_PARAM;
    }
    uint8_t data[11] = {0};
    data[0] = (uint8_t)address;
    data[1] = (uint8_t)(address >> 8);
    uint8_t command = address < 0x10000 ? 0x88 :
                      address < 0x20000 ? 0x98 :
                      address < 0x30000 ? 0xa8 : 0xb8;
    data[2] = (uint8_t)((command & 0xf0) | length);
    int result = xu_control(handle, interface_number, unit,
                            XU_FLASH_SELECTOR, 0, data, sizeof(data));
    if (result != (int)sizeof(data)) {
        return result < 0 ? result : LIBUSB_ERROR_IO;
    }
    result = xu_control(handle, interface_number, unit, XU_FLASH_SELECTOR, 1,
                        data, sizeof(data));
    if (result != (int)sizeof(data)) {
        return result < 0 ? result : LIBUSB_ERROR_IO;
    }
    memcpy(out, data + 3, length);
    return 0;
}

static int flash_read(libusb_device_handle *handle, int interface_number,
                      uint8_t unit, uint32_t address, uint8_t *out,
                      size_t length) {
    while (length > 0) {
        uint8_t chunk = length > 8 ? 8 : (uint8_t)length;
        int result = flash_read_block(handle, interface_number, unit, address,
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

static int rom_read(libusb_device_handle *handle, int interface_number,
                    uint8_t unit, uint16_t address, uint8_t *out) {
    uint8_t data[11] = {0};
    data[0] = (uint8_t)address;
    data[1] = (uint8_t)(address >> 8);
    data[2] = 8;
    int result = xu_control(handle, interface_number, unit, 0x04, 0, data,
                            sizeof(data));
    if (result != (int)sizeof(data)) {
        return result < 0 ? result : LIBUSB_ERROR_IO;
    }
    result = xu_control(handle, interface_number, unit, 0x04, 1, data,
                        sizeof(data));
    if (result != (int)sizeof(data)) {
        return result < 0 ? result : LIBUSB_ERROR_IO;
    }
    memcpy(out, data + 3, 8);
    return 0;
}

static int looks_like_rom(const uint8_t *data) {
    return isdigit(data[0]) && isdigit(data[1]) && isdigit(data[2]) &&
           data[3] == 'R' && data[4] == '0';
}

static void print_setting(const char *label, const uint8_t *data, size_t size) {
    printf("  %s raw=", label);
    print_hex(data, size);
    printf(" ascii=");
    print_ascii(data, size);
    if (data[0] >= 2 && (data[0] & 1) == 0 && data[0] <= size * 2) {
        size_t chars = (data[0] - 2) / 2;
        printf("  %s decoded=\"", label);
        for (size_t i = 0; i < chars; ++i) {
            putchar(isprint(data[2 + i * 2]) ? data[2 + i * 2] : '.');
        }
        puts("\"");
    }
}

typedef int (*flash_reader_fn)(libusb_device_handle *, int, uint8_t,
                               uint32_t, uint8_t *, size_t);

static void print_double_group(libusb_device_handle *handle, int interface_number,
                               uint8_t unit, const char *label,
                               uint32_t address, size_t count,
                               flash_reader_fn reader) {
    printf("  %s @0x%05x:", label, address);
    for (size_t i = 0; i < count; ++i) {
        uint8_t raw[8] = {0};
        int result = reader(handle, interface_number, unit,
                            address + (uint32_t)(i * sizeof(raw)), raw,
                            sizeof(raw));
        if (result != 0) {
            printf(" [%zu]=<read:%s>", i, libusb_strerror(result));
            continue;
        }
        if (memcmp(raw, "\xff\xff\xff\xff\xff\xff\xff\xff", 8) == 0) {
            printf(" [%zu]=<unburned>", i);
            continue;
        }
        double value = 0.0;
        memcpy(&value, raw, sizeof(value));
        if (isfinite(value)) {
            printf(" [%zu]=%.12g", i, value);
        } else {
            printf(" [%zu]=<nonfinite>", i);
        }
    }
    putchar('\n');
}

static void print_device_params(libusb_device_handle *handle,
                                int interface_number, uint8_t unit,
                                flash_reader_fn reader) {
    printf("  device_params @0x45000:");
    for (size_t i = 0; i < 5; ++i) {
        uint8_t raw[8] = {0};
        int result = reader(handle, interface_number, unit,
                            0x45000 + (uint32_t)(i * sizeof(raw)), raw,
                            sizeof(raw));
        if (result != 0) {
            printf(" [%zu]=<read:%s>", i, libusb_strerror(result));
            continue;
        }
        char text[9];
        memcpy(text, raw, sizeof(raw));
        text[8] = '\0';
        for (size_t j = 0; j < sizeof(raw); ++j) {
            if (text[j] == '\0' || text[j] == (char)0xff) {
                text[j] = '\0';
                break;
            }
            if (!isprint((unsigned char)text[j])) {
                text[j] = '.';
            }
        }
        printf(" [%zu]=%s", i, text[0] == '\0' ? "<empty>" : text);
    }
    putchar('\n');
}

static int probe_device(libusb_device *device, const char *wanted_port) {
    struct libusb_device_descriptor descriptor;
    char port[64];
    if (libusb_get_device_descriptor(device, &descriptor) != 0 ||
        descriptor.idVendor != SONIX_VID || descriptor.idProduct != SONIX_PID ||
        port_path(device, port, sizeof(port)) != 0 ||
        (wanted_port != NULL && strcmp(port, wanted_port) != 0)) {
        return 0;
    }

    libusb_device_handle *handle = NULL;
    int result = libusb_open(device, &handle);
    if (result != 0) {
        fprintf(stderr, "port=%s open failed: %s\n", port,
                libusb_strerror(result));
        return -1;
    }
    int interface_number = control_interface(device);
    if (interface_number < 0) {
        fprintf(stderr, "port=%s control interface lookup failed: %s\n", port,
                libusb_strerror(interface_number));
        libusb_close(handle);
        return -1;
    }

    int was_attached = libusb_kernel_driver_active(handle, interface_number);
    if (was_attached == 1) {
        result = libusb_detach_kernel_driver(handle, interface_number);
        if (result != 0) {
            fprintf(stderr, "port=%s detach interface=%d failed: %s\n", port,
                    interface_number, libusb_strerror(result));
            libusb_close(handle);
            return -1;
        }
    }
    result = libusb_claim_interface(handle, interface_number);
    if (result != 0) {
        fprintf(stderr, "port=%s claim interface=%d failed: %s\n", port,
                interface_number, libusb_strerror(result));
        if (was_attached == 1) {
            libusb_attach_kernel_driver(handle, interface_number);
        }
        libusb_close(handle);
        return -1;
    }

    uint8_t unit = 0, chip = 0;
    result = find_xu_unit(handle, interface_number, &unit, &chip);
    printf("port=%s interface=%d xu_unit=%s", port, interface_number,
           result == 0 ? "found" : "not-found");
    if (result == 0) {
        printf("(%u) chip=0x%02x\n", unit, chip);
        uint8_t roms[6][8];
        const uint16_t addresses[6] = {
            0x9ff8, 0xd2b2, 0xdff8, 0xbff8, 0xc7f8, 0xaff8
        };
        int rom_found = 0;
        for (size_t i = 0; i < 6; ++i) {
            memset(roms[i], 0xff, sizeof(roms[i]));
            int read_result = rom_read(handle, interface_number, unit,
                                       addresses[i], roms[i]);
            if (read_result == 0) {
                printf("  ROM[0x%04x]=", addresses[i]);
                print_hex(roms[i], sizeof(roms[i]));
                printf(" ascii=");
                print_ascii(roms[i], sizeof(roms[i]));
                if (looks_like_rom(roms[i])) {
                    rom_found = 1;
                }
            } else {
                printf("  ROM[0x%04x] read failed: %s\n", addresses[i],
                       libusb_strerror(read_result));
            }
        }

        uint8_t raw[64];
        if (flash_read(handle, interface_number, unit, 0, raw, sizeof(raw)) == 0) {
            printf("  FLASH[0x0000..0x003f]=");
            print_hex(raw, sizeof(raw));
            putchar('\n');
        }

        /* The source SDK's default table fallback is 0x8000. */
        uint32_t parameter_start = 0x8000;
        uint8_t rom[8] = {0};
        for (size_t i = 0; i < 6; ++i) {
            if (looks_like_rom(roms[i])) {
                memcpy(rom, roms[i], sizeof(rom));
                break;
            }
        }
        if (memcmp(rom, "232R0", 4) == 0 && rom[5] == 1) {
            parameter_start = 0xc000;
        } else if (memcmp(rom, "232R0", 4) == 0 && rom[5] == 2) {
            parameter_start = 0xc000;
        } else if (memcmp(rom, "276R0", 4) == 0 && rom[5] == 1) {
            parameter_start = 0xc000;
        } else if (memcmp(rom, "216R0", 4) == 0) {
            parameter_start = 0x5800;
        } else if (rom[0] >= '2' && rom[0] <= '9') {
            /* Newer Sonix layouts store the table address in sectorTable. */
            uint8_t table[0x2b];
            if (flash_read(handle, interface_number, unit, 0x160, table,
                           sizeof(table)) == 0 && table[0x0f] != 0xff) {
                parameter_start = ((uint32_t)table[0x0f] << 24) |
                                  ((uint32_t)table[0x10] << 16) |
                                  ((uint32_t)table[0x11] << 8) | table[0x12];
            }
        }
        printf("  parameter_table_start=0x%08x (rom_probe=%s)\n",
               parameter_start, rom_found ? "matched" : "unknown");
        uint8_t setting[64];
        if (flash_read(handle, interface_number, unit, parameter_start + 0xc0,
                       setting, sizeof(setting)) == 0) {
            print_setting("serial", setting, sizeof(setting));
        } else {
            printf("  serial read failed at 0x%08x\n",
                   parameter_start + 0xc0);
        }
        int xu6_prepare = sf_xu6_prepare_290(handle, interface_number, unit);
        printf("  XU6 calibration channel prepare=%s\n",
               xu6_prepare == 0 ? "ok" : libusb_strerror(xu6_prepare));
        if (xu6_prepare == 0) {
            print_double_group(handle, interface_number, unit, "fx_params",
                               0x41000, 5, sf_xu6_read);
            print_double_group(handle, interface_number, unit, "fy_params",
                               0x42000, 5, sf_xu6_read);
            print_double_group(handle, interface_number, unit, "fz_params",
                               0x43000, 8, sf_xu6_read);
            print_double_group(handle, interface_number, unit, "fw_params",
                               0x44000, 9, sf_xu6_read);
            print_device_params(handle, interface_number, unit, sf_xu6_read);
        }
    } else {
        printf("\n");
    }

    libusb_release_interface(handle, interface_number);
    if (was_attached == 1) {
        libusb_attach_kernel_driver(handle, interface_number);
    }
    libusb_close(handle);
    return 1;
}

int main(int argc, char **argv) {
    const char *wanted_port = argc > 1 ? argv[1] : NULL;
    libusb_context *context = NULL;
    int result = libusb_init(&context);
    if (result != 0) {
        fprintf(stderr, "libusb_init failed: %s\n", libusb_strerror(result));
        return 1;
    }
    libusb_device **devices = NULL;
    ssize_t count = libusb_get_device_list(context, &devices);
    if (count < 0) {
        fprintf(stderr, "get device list failed: %s\n", libusb_strerror((int)count));
        libusb_exit(context);
        return 1;
    }
    int matched = 0;
    for (ssize_t i = 0; i < count; ++i) {
        int status = probe_device(devices[i], wanted_port);
        if (status > 0) {
            matched++;
        }
    }
    libusb_free_device_list(devices, 1);
    libusb_exit(context);
    if (wanted_port != NULL && matched == 0) {
        fprintf(stderr, "no Sonix 0c45:636f device at port %s\n", wanted_port);
        return 2;
    }
    return matched > 0 ? 0 : 2;
}
