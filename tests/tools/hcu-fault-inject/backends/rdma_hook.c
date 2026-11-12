/* Copyright (c) 2026 Hygon Information Technology Co., Ltd. */
/* SPDX-License-Identifier: Apache-2.0 */

#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <infiniband/verbs.h>
#include <inttypes.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

struct rule {
    char token[33];
    char operation[12];
    char device[64];
    uint64_t expires;
    uint64_t start_ticks;
    unsigned value;
};

struct hooked_context {
    struct ibv_context *context;
    int (*send)(struct ibv_qp *, struct ibv_send_wr *, struct ibv_send_wr **);
    int (*recv)(struct ibv_qp *, struct ibv_recv_wr *, struct ibv_recv_wr **);
    int (*poll)(struct ibv_cq *, int, struct ibv_wc *);
    int directory;
    uint64_t start_ticks;
    pid_t pid;
    struct hooked_context *next;
};

static pthread_mutex_t contexts_lock = PTHREAD_MUTEX_INITIALIZER;
static struct hooked_context *contexts;

static uint64_t now_ns(void)
{
    struct timespec now;

    if (clock_gettime(CLOCK_MONOTONIC, &now))
        return 0;
    return (uint64_t)now.tv_sec * UINT64_C(1000000000) + now.tv_nsec;
}

static uint64_t process_start_ticks(void)
{
    char data[4096];
    int fd = open("/proc/self/stat", O_RDONLY | O_CLOEXEC);

    if (fd < 0)
        return 0;
    ssize_t length = read(fd, data, sizeof(data) - 1);
    close(fd);
    if (length <= 0)
        return 0;
    data[length] = '\0';
    char *fields = strrchr(data, ')');
    if (!fields)
        return 0;
    char *save = NULL;
    char *field = strtok_r(fields + 1, " ", &save);
    for (int index = 3; field && index < 22; ++index)
        field = strtok_r(NULL, " ", &save);
    if (!field)
        return 0;
    char *end = NULL;
    errno = 0;
    uint64_t ticks = strtoull(field, &end, 10);
    return !errno && end != field && !*end ? ticks : 0;
}

static bool private_fd(int fd, bool directory)
{
    struct stat info;

    return !fstat(fd, &info) && info.st_uid == geteuid() &&
           !(info.st_mode & 0077) &&
           (directory ? S_ISDIR(info.st_mode) :
                        S_ISREG(info.st_mode) && info.st_nlink == 1);
}

static void ready_marker(const struct hooked_context *ctx)
{
    char name[48], temporary[88], contents[32];
    snprintf(name, sizeof(name), "%ld.ready", (long)ctx->pid);
    snprintf(temporary, sizeof(temporary), ".%ld-%" PRIu64 ".ready.tmp",
             (long)ctx->pid, ctx->start_ticks);
    int length = snprintf(contents, sizeof(contents), "%" PRIu64 "\n", ctx->start_ticks);
    int fd = openat(ctx->directory, temporary,
                    O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (fd < 0)
        return;
    ssize_t written = write(fd, contents, (size_t)length);
    close(fd);
    if (written != length || renameat(ctx->directory, temporary, ctx->directory, name))
        unlinkat(ctx->directory, temporary, 0);
}

static bool read_rule(const struct hooked_context *ctx, uint32_t qpn, struct rule *rule)
{
    char name[64], data[224], extra;

    if (ctx->pid != getpid())
        return false;
    snprintf(name, sizeof(name), "%ld-%" PRIu32 ".rule", (long)ctx->pid, qpn);
    int fd = openat(ctx->directory, name, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    if (fd < 0)
        return false;
    ssize_t length = private_fd(fd, false) ? read(fd, data, sizeof(data) - 1) : -1;
    close(fd);
    if (length <= 0 || (size_t)length >= sizeof(data) - 1)
        return false;
    data[length] = '\0';
    uint64_t now = now_ns();
    const char *device = ibv_get_device_name(ctx->context->device);
    return now && device &&
           sscanf(data, "%32[0-9a-f] %" SCNu64 " %11s %u %" SCNu64 " %63s %c",
                  rule->token, &rule->expires, rule->operation, &rule->value,
                  &rule->start_ticks, rule->device, &extra) == 6 &&
           strlen(rule->token) == 32 && rule->start_ticks == ctx->start_ticks &&
           !strcmp(device, rule->device) &&
           now < rule->expires;
}

static bool same_rule(const struct hooked_context *ctx, uint32_t qpn, const struct rule *original)
{
    struct rule current;

    return read_rule(ctx, qpn, &current) && !strcmp(current.token, original->token) &&
           !strcmp(current.operation, original->operation) && current.value == original->value &&
           !strcmp(current.device, original->device) &&
           current.expires == original->expires;
}

static int claim_hit(const struct hooked_context *ctx, uint32_t qpn, const struct rule *rule)
{
    char name[40];
    struct stat info;

    snprintf(name, sizeof(name), "%s.hit", rule->token);
    int fd = openat(ctx->directory, name, O_WRONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    if (fd < 0)
        return -1;
    if (!private_fd(fd, false) || flock(fd, LOCK_EX | LOCK_NB) || fstat(fd, &info) ||
        info.st_size != 0 || !same_rule(ctx, qpn, rule)) {
        close(fd);
        return -1;
    }
    return fd;
}

static bool bounded_delay(const struct hooked_context *ctx, uint32_t qpn, const struct rule *rule)
{
    uint64_t now = now_ns();
    uint64_t deadline = now + (uint64_t)rule->value * UINT64_C(1000000);

    while (now && now < deadline) {
        if (!same_rule(ctx, qpn, rule))
            return false;
        uint64_t next = now + UINT64_C(10000000);
        if (next > deadline)
            next = deadline;
        struct timespec wake = {
            .tv_sec = (time_t)(next / UINT64_C(1000000000)),
            .tv_nsec = (long)(next % UINT64_C(1000000000)),
        };
        clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &wake, NULL);
        now = now_ns();
    }
    return now && same_rule(ctx, qpn, rule);
}

static void apply_error(struct ibv_qp *qp, int hit)
{
    struct ibv_qp_attr attributes = {.qp_state = IBV_QPS_ERR};
    struct ibv_qp_init_attr init = {0};
    int rc = ibv_modify_qp(qp, &attributes, IBV_QP_STATE);

    if (rc) {
        int error = rc > 0 ? rc : errno;
        dprintf(hit, "%s qp_modify_errno=%d\n",
                error == EOPNOTSUPP || error == ENOSYS ? "unsupported" : "error", error);
        return;
    }
    struct ibv_qp_attr readback = {0};
    rc = ibv_query_qp(qp, &readback, IBV_QP_STATE, &init);
    if (rc || readback.qp_state != IBV_QPS_ERR)
        dprintf(hit, "error qp_err_readback_failed qpn=%" PRIu32 "\n", qp->qp_num);
    else
        dprintf(hit, "applied qp_state=ERR qpn=%" PRIu32 "\n", qp->qp_num);
}

static void qp_fault(const struct hooked_context *ctx, struct ibv_qp *qp, bool receive)
{
    struct rule rule;
    uint32_t qpn = qp->qp_num;

    if (!read_rule(ctx, qpn, &rule))
        return;
    bool wait = receive && !strcmp(rule.operation, "wait");
    bool error = !strcmp(rule.operation, "error");
    if (!wait && !error)
        return;
    int hit = claim_hit(ctx, qpn, &rule);
    if (hit < 0)
        return;
    if (qp->qp_type != IBV_QPT_RC) {
        dprintf(hit, "unsupported qp_type=%u qpn=%" PRIu32 "\n", (unsigned)qp->qp_type, qpn);
    } else if (wait) {
        if (!rule.value || rule.value > 5000)
            dprintf(hit, "unsupported invalid_receive_delay\n");
        else if (bounded_delay(ctx, qpn, &rule))
            dprintf(hit, "applied post_recv_delay_ms=%u qpn=%" PRIu32 "\n", rule.value, qpn);
    } else {
        apply_error(qp, hit);
    }
    close(hit);
}

static struct hooked_context *find_context(struct ibv_context *context)
{
    pthread_mutex_lock(&contexts_lock);
    struct hooked_context *item = contexts;
    while (item && item->context != context)
        item = item->next;
    pthread_mutex_unlock(&contexts_lock);
    return item;
}

static int hook_send(struct ibv_qp *qp, struct ibv_send_wr *wr, struct ibv_send_wr **bad)
{
    struct hooked_context *ctx = find_context(qp->context);

    if (!ctx || !ctx->send) {
        if (bad)
            *bad = wr;
        return EINVAL;
    }
    qp_fault(ctx, qp, false);
    return ctx->send(qp, wr, bad);
}

static int hook_recv(struct ibv_qp *qp, struct ibv_recv_wr *wr, struct ibv_recv_wr **bad)
{
    struct hooked_context *ctx = find_context(qp->context);

    if (!ctx || !ctx->recv) {
        if (bad)
            *bad = wr;
        return EINVAL;
    }
    qp_fault(ctx, qp, true);
    return ctx->recv(qp, wr, bad);
}

static int hook_poll(struct ibv_cq *cq, int count, struct ibv_wc *completions)
{
    struct hooked_context *ctx = find_context(cq->context);

    if (!ctx || !ctx->poll)
        return -EINVAL;
    int completed = ctx->poll(cq, count, completions);
    if (completed <= 0)
        return completed;
    uint32_t first = completions[0].qp_num;
    bool single_qp = true;
    for (int index = 1; index < completed; ++index)
        if (completions[index].qp_num != first)
            single_qp = false;
    for (int index = 0; index < (single_qp ? 1 : completed); ++index) {
        uint32_t qpn = completions[index].qp_num;
        struct rule rule;
        if (!read_rule(ctx, qpn, &rule) || strcmp(rule.operation, "cq"))
            continue;
        int hit = claim_hit(ctx, qpn, &rule);
        if (hit < 0)
            continue;
        if (!single_qp)
            dprintf(hit, "unsupported shared_cq_batch qpn=%" PRIu32 "\n", qpn);
        else if (!rule.value || rule.value > 5000)
            dprintf(hit, "unsupported invalid_cq_delay\n");
        else if (bounded_delay(ctx, qpn, &rule))
            dprintf(hit, "applied cq_read_delay_ms=%u completions=%d qpn=%" PRIu32 "\n",
                    rule.value, completed, qpn);
        close(hit);
    }
    return completed;
}

static void install_context(struct hooked_context *ctx)
{
    if (ctx->send)
        ctx->context->ops.post_send = hook_send;
    if (ctx->recv)
        ctx->context->ops.post_recv = hook_recv;
    if (ctx->poll)
        ctx->context->ops.poll_cq = hook_poll;
}

struct ibv_context *ibv_open_device(struct ibv_device *device)
{
    /* Older glibc may resolve an RTLD_NEXT lookup to the 1.0 compat ABI. */
    struct ibv_context *(*next_open)(struct ibv_device *) =
        dlvsym(RTLD_NEXT, "ibv_open_device", "IBVERBS_1.1");
    if (!next_open) {
        errno = ENOSYS;
        return NULL;
    }
    struct ibv_context *context = next_open(device);
    const char *path = getenv("HCU_RDMA_HOOK_DIR");
    if (!context || !path)
        return context;

    int directory = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC | O_NOFOLLOW);
    if (directory < 0)
        return context;
    uint64_t ticks = process_start_ticks();
    if (!ticks || !private_fd(directory, true) ||
        !(context->ops.post_send || context->ops.post_recv || context->ops.poll_cq)) {
        close(directory);
        return context;
    }
    struct hooked_context *ctx = calloc(1, sizeof(*ctx));
    if (!ctx) {
        close(directory);
        return context;
    }
    ctx->context = context;
    ctx->send = context->ops.post_send;
    ctx->recv = context->ops.post_recv;
    ctx->poll = context->ops.poll_cq;
    ctx->directory = directory;
    ctx->start_ticks = ticks;
    ctx->pid = getpid();
    pthread_mutex_lock(&contexts_lock);
    ctx->next = contexts;
    contexts = ctx;
    install_context(ctx);
    ready_marker(ctx);
    pthread_mutex_unlock(&contexts_lock);
    return context;
}

int ibv_close_device(struct ibv_context *context)
{
    int (*next_close)(struct ibv_context *) =
        dlvsym(RTLD_NEXT, "ibv_close_device", "IBVERBS_1.1");
    if (!next_close)
        return ENOSYS;
    struct hooked_context *ctx = find_context(context);
    if (!ctx)
        return next_close(context);

    context->ops.post_send = ctx->send;
    context->ops.post_recv = ctx->recv;
    context->ops.poll_cq = ctx->poll;
    int rc = next_close(context);
    if (rc) {
        install_context(ctx);
        return rc;
    }
    pthread_mutex_lock(&contexts_lock);
    struct hooked_context **item = &contexts;
    while (*item && *item != ctx)
        item = &(*item)->next;
    if (*item)
        *item = ctx->next;
    if (ctx->pid == getpid()) {
        struct hooked_context *other = contexts;
        while (other && other->pid != ctx->pid)
            other = other->next;
        if (!other) {
            char ready[48];
            snprintf(ready, sizeof(ready), "%ld.ready", (long)ctx->pid);
            unlinkat(ctx->directory, ready, 0);
        }
    }
    pthread_mutex_unlock(&contexts_lock);
    close(ctx->directory);
    free(ctx);
    return 0;
}
