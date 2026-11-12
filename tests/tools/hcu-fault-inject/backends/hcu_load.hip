// Copyright (c) 2026 Hygon Information Technology Co., Ltd.
// SPDX-License-Identifier: Apache-2.0

#include <hip/hip_runtime.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <csignal>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <strings.h>
#include <sys/prctl.h>
#include <unistd.h>

namespace {

constexpr int kThreads = 256;
constexpr int kComputeIterations = 512;
constexpr int kComputeBatch = 8;
constexpr int kCopyBatch = 24;
constexpr size_t kMiB = 1024 * 1024;
volatile sig_atomic_t stopping = 0;

void stop(int) {
    stopping = 1;
}

void check(hipError_t code, const char* operation) {
    if (code != hipSuccess) {
        std::fprintf(stderr, "%s: %s\n", operation, hipGetErrorString(code));
        std::exit(EXIT_FAILURE);
    }
}

long parse_number(const char* input, long min, long max) {
    char* end = nullptr;
    const long number = std::strtol(input, &end, 10);
    if (end == input || *end != '\0' || number < min || number > max) {
        std::fprintf(stderr, "Invalid numeric argument: %s\n", input);
        std::exit(EXIT_FAILURE);
    }
    return number;
}

__global__ void fp32_load(float* output, float seed) {
    const auto index = blockIdx.x * blockDim.x + threadIdx.x;
    const float start = seed + static_cast<float>(index & 255) * 0.0001f;
    float x = start;
    float y = start + 0.1f;
    float z = start + 0.2f;
    float w = start + 0.3f;
    for (int i = 0; i < kComputeIterations; ++i) {
        x = fmaf(x, 0.9991f, 0.0002f);
        y = fmaf(y, 0.9992f, 0.0003f);
        z = fmaf(z, 0.9993f, 0.0004f);
        w = fmaf(w, 0.9994f, 0.0005f);
    }
    output[index] = x + y + z + w;
}

void sample(const char* mode, const char* bdf, double rate, const char* unit, size_t buffer_size) {
    std::printf("{\"ready\":true,\"mode\":\"%s\",\"bdf\":\"%s\",\"throughput\":%.6f,"
                "\"unit\":\"%s\",\"buffer_bytes\":%zu}\n", mode, bdf, rate, unit, buffer_size);
    std::fflush(stdout);
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 5 || argc > 6 || (argc == 6 && std::strcmp(argv[5], "--samples") != 0)) {
        std::fprintf(stderr, "Usage: %s compute|memory BDF SECONDS PARENT_PID [--samples]\n", argv[0]);
        return EXIT_FAILURE;
    }
    const bool compute = std::strcmp(argv[1], "compute") == 0;
    if (!compute && std::strcmp(argv[1], "memory") != 0) {
        std::fputs("Unknown HIP load mode\n", stderr);
        return EXIT_FAILURE;
    }
    const long seconds = parse_number(argv[3], 1, 602);
    const auto parent = static_cast<pid_t>(parse_number(argv[4], 0, 2147483647L));
    const bool continuous_samples = argc == 6;
    std::signal(SIGTERM, stop);
    std::signal(SIGINT, stop);
    if (parent != 0 && (prctl(PR_SET_PDEATHSIG, SIGTERM, 0L, 0L, 0L) != 0 || getppid() != parent)) {
        std::fputs("Could not bind HIP load helper lifetime to its supervisor\n", stderr);
        return EXIT_FAILURE;
    }

    int device = -1;
    char bdf[32] = {};
    check(hipDeviceGetByPCIBusId(&device, argv[2]), "find visible HIP device by PCI BDF");
    check(hipDeviceGetPCIBusId(bdf, sizeof(bdf), device), "read HIP device PCI BDF");
    if (strcasecmp(bdf, argv[2]) != 0) {
        std::fprintf(stderr, "HIP device PCI BDF %s does not match requested %s\n", bdf, argv[2]);
        return EXIT_FAILURE;
    }
    check(hipSetDevice(device), "select HIP device");
    hipStream_t stream;
    check(hipStreamCreateWithFlags(&stream, hipStreamNonBlocking), "create HIP stream");

    hipDeviceProp_t properties;
    check(hipGetDeviceProperties(&properties, device), "read HIP device properties");
    const int blocks = std::max(1, properties.multiProcessorCount) * 16;
    float* first = nullptr;
    float* second = nullptr;
    size_t buffer_size = static_cast<size_t>(blocks) * kThreads * sizeof(float);
    if (!compute) {
        size_t available = 0;
        size_t total = 0;
        check(hipMemGetInfo(&available, &total), "read available HIP memory");
        buffer_size = std::min<size_t>(256 * kMiB, available / 16 / kMiB * kMiB);
        if (buffer_size < 64 * kMiB) {
            std::fputs("At least 1 GiB of free device memory is required for the memory load\n", stderr);
            return EXIT_FAILURE;
        }
    }
    check(hipMalloc(&first, buffer_size), "allocate HIP load buffer");
    if (!compute) {
        check(hipMalloc(&second, buffer_size), "allocate HIP copy buffer");
        check(hipMemsetAsync(first, 0xa5, buffer_size, stream), "initialize HIP copy buffer");
        check(hipStreamSynchronize(stream), "finish HIP copy buffer initialization");
    }

    using Clock = std::chrono::steady_clock;
    const auto begin = Clock::now();
    const auto deadline = begin + std::chrono::seconds(seconds);
    auto sample_start = begin;
    double operations = 0;
    bool first_sample = true;
    while (!stopping && Clock::now() < deadline) {
        if (compute) {
            for (int i = 0; i < kComputeBatch; ++i) {
                hipLaunchKernelGGL(fp32_load, dim3(blocks), dim3(kThreads), 0, stream, first, 0.25f + i * 0.01f);
                check(hipGetLastError(), "launch HIP FP32 load");
            }
            operations += static_cast<double>(blocks) * kThreads * kComputeIterations * 8 * kComputeBatch;
        } else {
            for (int i = 0; i < kCopyBatch; ++i) {
                check(hipMemcpyAsync(second, first, buffer_size, hipMemcpyDeviceToDevice, stream), "queue HIP copy");
                std::swap(first, second);
            }
            operations += static_cast<double>(buffer_size) * kCopyBatch;
        }
        check(hipStreamSynchronize(stream), "finish HIP load batch");
        const auto now = Clock::now();
        if (first_sample || (continuous_samples && now - sample_start >= std::chrono::seconds(1))) {
            const double elapsed = std::chrono::duration<double>(now - sample_start).count();
            const double rate = operations / elapsed / (compute ? 1e12 : 1e9);
            sample(argv[1], bdf, rate, compute ? "TFLOPS" : "GB/s", buffer_size);
            first_sample = false;
            sample_start = now;
            operations = 0;
        }
    }
    if (second != nullptr) {
        check(hipFree(second), "free HIP copy buffer");
    }
    check(hipFree(first), "free HIP load buffer");
    check(hipStreamDestroy(stream), "destroy HIP stream");
    return first_sample ? EXIT_FAILURE : EXIT_SUCCESS;
}
