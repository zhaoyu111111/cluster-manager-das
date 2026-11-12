/* Copyright (c) 2026 Hygon Information Technology Co., Ltd. */
/* SPDX-License-Identifier: Apache-2.0 */

#define _GNU_SOURCE
#include <arpa/inet.h>
#include <dlfcn.h>
#include <errno.h>
#include <stdlib.h>
#include <sys/socket.h>

int bind(int fd, const struct sockaddr *address, socklen_t length)
{
    int (*original)(int, const struct sockaddr *, socklen_t) = dlsym(RTLD_NEXT, "bind");
    if (!original) {
        errno = ENOSYS;
        return -1;
    }

    const char *setting = getenv("HCU_RDMA_LOOPBACK_PORT");
    if (!setting || !address || length < sizeof(address->sa_family))
        return original(fd, address, length);

    char *end;
    long port = strtol(setting, &end, 10);
    if (end == setting || *end || port < 1 || port > 65535) {
        errno = EINVAL;
        return -1;
    }

    in_port_t requested_port;
    if (address->sa_family == AF_INET && length >= sizeof(struct sockaddr_in)) {
        requested_port = ((const struct sockaddr_in *)address)->sin_port;
    } else if (address->sa_family == AF_INET6 && length >= sizeof(struct sockaddr_in6)) {
        requested_port = ((const struct sockaddr_in6 *)address)->sin6_port;
    } else {
        return original(fd, address, length);
    }
    if (requested_port != htons((in_port_t)port))
        return original(fd, address, length);

    int type;
    socklen_t type_length = sizeof(type);
    if (getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &type_length) < 0)
        return -1;
    if (type != SOCK_STREAM)
        return original(fd, address, length);
    if (address->sa_family != AF_INET) {
        errno = EAFNOSUPPORT;
        return -1;
    }

    struct sockaddr_in loopback = *(const struct sockaddr_in *)address;
    if (loopback.sin_addr.s_addr != htonl(INADDR_ANY) &&
        loopback.sin_addr.s_addr != htonl(INADDR_LOOPBACK)) {
        errno = EADDRNOTAVAIL;
        return -1;
    }
    loopback.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    return original(fd, (const struct sockaddr *)&loopback, sizeof(loopback));
}
