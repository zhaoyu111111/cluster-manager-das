/* Copyright (c) 2026 Hygon Information Technology Co., Ltd. */
/* SPDX-License-Identifier: Apache-2.0 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

enum { HIP_SUCCESS = 0, HIP_NOT_INITIALIZED = 3, HIP_LAUNCH_FAILURE = 719 };

static int rule_for_device(int directory, void *stream, const char *api,
                           int *delay_ms, uint64_t *expires)
{
    int (*get_device)(int *) = dlsym(RTLD_NEXT, "hipGetDevice");
    int (*stream_device)(void *, int *) = dlsym(RTLD_NEXT, "hipStreamGetDevice");
    int (*get_bdf)(char *, int, int) = dlsym(RTLD_NEXT, "hipDeviceGetPCIBusId");
    int device;
    char bdf[32] = {0};

    if (!get_bdf || (stream ? !stream_device || stream_device(stream, &device)
                            : !get_device || get_device(&device)))
        return 0;
    if (get_bdf(bdf, sizeof(bdf), device))
        return 0;

    unsigned int domain, bus, slot, function;
    int consumed = 0;
    if (sscanf(bdf, "%4x:%2x:%2x.%1x%n", &domain, &bus, &slot, &function, &consumed) != 4 ||
        bdf[consumed] || slot > 31 || function > 7)
        return 0;
    char name[64];
    snprintf(name, sizeof(name), "%04x:%02x:%02x.%x.rule", domain, bus, slot, function);
    int fd = openat(directory, name, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    if (fd < 0)
        return 0;
    char text[96];
    ssize_t size = read(fd, text, sizeof(text) - 1);
    close(fd);
    if (size <= 0)
        return 0;
    text[size] = '\0';

    char token[33], operation[8];
    unsigned long long deadline;
    int millis;
    if (sscanf(text, "%32[0-9a-f] %llu %7s %d", token, &deadline, operation, &millis) != 4 ||
        strlen(token) != 32 || millis <= 0 || millis > 1000)
        return 0;
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) ||
        deadline <= (uint64_t)now.tv_sec * 1000000000 + now.tv_nsec)
        return 0;

    int error = strcmp(operation, "error") == 0;
    if (!error && strcmp(operation, "delay") != 0)
        return 0;
    *delay_ms = error ? 0 : millis;
    *expires = deadline;
    snprintf(name, sizeof(name), "%s.hit", token);
    fd = openat(directory, name, O_WRONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    if (fd >= 0) {
        struct stat info;
        if (!flock(fd, LOCK_EX | LOCK_NB) && !fstat(fd, &info) && S_ISREG(info.st_mode) && !info.st_size)
            dprintf(fd, "pid=%ld api=%s", (long)getpid(), api);
        close(fd);
    }
    return error;
}

static int inject(void *stream, const char *api)
{
    const char *path = getenv("HCU_FAULT_HOOK_DIR");
    if (!path)
        return 0;
    int directory = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    if (directory < 0)
        return 0;
    int delay_ms = 0;
    uint64_t expires = 0;
    int error = rule_for_device(directory, stream, api, &delay_ms, &expires);
    close(directory);

    if (delay_ms) {
        struct timespec now;
        if (!clock_gettime(CLOCK_MONOTONIC, &now)) {
            uint64_t until = (uint64_t)now.tv_sec * 1000000000 + now.tv_nsec +
                             (uint64_t)delay_ms * 1000000;
            if (until > expires)
                until = expires;
            struct timespec wake = { .tv_sec = until / 1000000000, .tv_nsec = until % 1000000000 };
            while (clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &wake, NULL) == EINTR)
                continue;
        }
    }
    return error;
}

int hipDeviceSynchronize(void)
{
    int (*synchronize)(void) = dlsym(RTLD_NEXT, "hipDeviceSynchronize");
    if (!synchronize)
        return HIP_NOT_INITIALIZED;
    int error = inject(NULL, "hipDeviceSynchronize");
    int result = synchronize();
    return result == HIP_SUCCESS && error ? HIP_LAUNCH_FAILURE : result;
}

int hipStreamSynchronize(void *stream)
{
    int (*synchronize)(void *) = dlsym(RTLD_NEXT, "hipStreamSynchronize");
    if (!synchronize)
        return HIP_NOT_INITIALIZED;
    int error = inject(stream, "hipStreamSynchronize");
    int result = synchronize(stream);
    return result == HIP_SUCCESS && error ? HIP_LAUNCH_FAILURE : result;
}
