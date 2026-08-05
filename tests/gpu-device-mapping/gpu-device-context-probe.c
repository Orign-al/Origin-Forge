#define _GNU_SOURCE

#include <dlfcn.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <time.h>
#include <unistd.h>

typedef int nvmlReturn_t;
typedef void *nvmlDevice_t;

typedef int CUresult;
typedef int CUdevice;
typedef void *CUcontext;

typedef struct {
    unsigned char bytes[16];
} CUuuid;

enum { CUDA_SUCCESS = 0, NVML_SUCCESS = 0 };

struct options {
    int expected_allocated;
    bool assert_slurm_isolation;
    const char *expect_cuda;
    double hold_seconds;
};

struct open_result {
    bool opened;
    int error_number;
};

static void print_cgroup(void)
{
    FILE *handle = fopen("/proc/self/cgroup", "r");
    char buffer[1024];

    puts("CGROUP_BEGIN");
    if (handle == NULL) {
        printf("ERROR errno=%d %s\n", errno, strerror(errno));
    } else {
        while (fgets(buffer, sizeof(buffer), handle) != NULL)
            fputs(buffer, stdout);
        fclose(handle);
    }
    puts("CGROUP_END");
}

static const char *role_for_minor(int minor, const bool allocated[256])
{
    if (minor >= 0 && minor < 256 && allocated[minor])
        return "ALLOCATED";
    return "UNALLOCATED";
}

static struct open_result probe_open(const char *path, const char *role,
                                     const char *mode_name, int flags)
{
    struct open_result result = {false, 0};
    int descriptor = open(path, flags | O_CLOEXEC);

    if (descriptor >= 0) {
        result.opened = true;
        printf("DEVICE_OPEN path=%s role=%s flags=%s result=OPENED errno=0 error=OK\n",
               path, role, mode_name);
        close(descriptor);
        return result;
    }

    result.error_number = errno;
    printf("DEVICE_OPEN path=%s role=%s flags=%s result=%s errno=%d error=%s\n",
           path, role, mode_name,
           (errno == EACCES || errno == EPERM) ? "DENIED" : "ERROR",
           errno, strerror(errno));
    return result;
}

static void print_cuda_uuid(const CUuuid *uuid)
{
    const unsigned char *b = uuid->bytes;
    printf("GPU-%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-"
           "%02x%02x%02x%02x%02x%02x",
           b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7],
           b[8], b[9], b[10], b[11], b[12], b[13], b[14], b[15]);
}

static int parse_options(int argc, char **argv, struct options *options)
{
    int index;

    options->expected_allocated = -1;
    options->assert_slurm_isolation = false;
    options->expect_cuda = "any";
    options->hold_seconds = 5.0;

    for (index = 1; index < argc; index++) {
        if (strcmp(argv[index], "--expect-allocated") == 0 && index + 1 < argc) {
            options->expected_allocated = atoi(argv[++index]);
        } else if (strcmp(argv[index], "--assert-slurm-isolation") == 0) {
            options->assert_slurm_isolation = true;
        } else if (strcmp(argv[index], "--expect-cuda") == 0 && index + 1 < argc) {
            options->expect_cuda = argv[++index];
        } else if (strcmp(argv[index], "--hold-seconds") == 0 && index + 1 < argc) {
            options->hold_seconds = atof(argv[++index]);
        } else {
            fprintf(stderr, "unknown or incomplete argument: %s\n", argv[index]);
            return -1;
        }
    }

    if (strcmp(options->expect_cuda, "success") != 0 &&
        strcmp(options->expect_cuda, "denied") != 0 &&
        strcmp(options->expect_cuda, "any") != 0) {
        fprintf(stderr, "invalid --expect-cuda value: %s\n", options->expect_cuda);
        return -1;
    }
    return 0;
}

static int collect_nvml_minors(bool allocated[256], int *visible_count)
{
    void *library = dlopen("libnvidia-ml.so.1", RTLD_NOW | RTLD_LOCAL);
    nvmlReturn_t (*nvmlInit_v2)(void);
    nvmlReturn_t (*nvmlShutdown)(void);
    nvmlReturn_t (*nvmlDeviceGetCount_v2)(unsigned int *);
    nvmlReturn_t (*nvmlDeviceGetHandleByIndex_v2)(unsigned int, nvmlDevice_t *);
    nvmlReturn_t (*nvmlDeviceGetUUID)(nvmlDevice_t, char *, unsigned int);
    nvmlReturn_t (*nvmlDeviceGetMinorNumber)(nvmlDevice_t, unsigned int *);
    unsigned int count = 0;
    unsigned int index;
    nvmlReturn_t result;

    if (library == NULL) {
        printf("NVML_LOAD=FAILED error=%s\n", dlerror());
        return -1;
    }

#define NVML_SYMBOL(name) do { \
    *(void **)(&name) = dlsym(library, #name); \
    if ((name) == NULL) { \
        printf("NVML_SYMBOL=FAILED name=%s error=%s\n", #name, dlerror()); \
        dlclose(library); \
        return -1; \
    } \
} while (0)

    NVML_SYMBOL(nvmlInit_v2);
    NVML_SYMBOL(nvmlShutdown);
    NVML_SYMBOL(nvmlDeviceGetCount_v2);
    NVML_SYMBOL(nvmlDeviceGetHandleByIndex_v2);
    NVML_SYMBOL(nvmlDeviceGetUUID);
    NVML_SYMBOL(nvmlDeviceGetMinorNumber);

    result = nvmlInit_v2();
    printf("NVML_INIT=result=%d\n", result);
    if (result != NVML_SUCCESS) {
        dlclose(library);
        return -1;
    }

    result = nvmlDeviceGetCount_v2(&count);
    printf("NVML_DEVICE_COUNT=result=%d count=%u\n", result, count);
    if (result != NVML_SUCCESS) {
        nvmlShutdown();
        dlclose(library);
        return -1;
    }

    for (index = 0; index < count; index++) {
        nvmlDevice_t device = NULL;
        char uuid[128] = {0};
        unsigned int minor = 0;
        nvmlReturn_t handle_result;
        nvmlReturn_t uuid_result;
        nvmlReturn_t minor_result;

        handle_result = nvmlDeviceGetHandleByIndex_v2(index, &device);
        if (handle_result != NVML_SUCCESS) {
            printf("VISIBLE_GPU nvml_index=%u handle_result=%d\n",
                   index, handle_result);
            continue;
        }
        uuid_result = nvmlDeviceGetUUID(device, uuid, sizeof(uuid));
        minor_result = nvmlDeviceGetMinorNumber(device, &minor);
        printf("VISIBLE_GPU nvml_index=%u uuid_result=%d uuid=%s "
               "minor_result=%d linux_minor=%u device_path=/dev/nvidia%u\n",
               index, uuid_result, uuid, minor_result, minor, minor);
        if (uuid_result == NVML_SUCCESS && minor_result == NVML_SUCCESS && minor < 256)
            allocated[minor] = true;
    }

    *visible_count = (int)count;
    nvmlShutdown();
    dlclose(library);
    return 0;
}

static bool run_cuda_context(double hold_seconds)
{
    void *library = dlopen("libcuda.so.1", RTLD_NOW | RTLD_LOCAL);
    CUresult (*cuInit)(unsigned int);
    CUresult (*cuDeviceGetCount)(int *);
    CUresult (*cuDeviceGet)(CUdevice *, int);
    CUresult (*cuDeviceGetName)(char *, int, CUdevice);
    CUresult (*cuDeviceGetUuid_v2)(CUuuid *, CUdevice);
    CUresult (*cuCtxCreate_v2)(CUcontext *, unsigned int, CUdevice);
    CUresult (*cuCtxSynchronize)(void);
    CUresult (*cuCtxDestroy_v2)(CUcontext);
    CUresult (*cuGetErrorName)(CUresult, const char **);
    CUresult result;
    int count = 0;
    CUdevice device = 0;
    char name[256] = {0};
    CUuuid uuid = {{0}};
    CUcontext context = NULL;
    const char *error_name = "UNKNOWN";
    struct timespec hold;
    bool success = false;

    if (library == NULL) {
        printf("CUDA_CONTEXT_RESULT=BLOCKED stage=dlopen detail=%s\n", dlerror());
        return false;
    }

#define CUDA_SYMBOL(name) do { \
    *(void **)(&name) = dlsym(library, #name); \
    if ((name) == NULL) { \
        printf("CUDA_CONTEXT_RESULT=BLOCKED stage=dlsym detail=%s\n", #name); \
        dlclose(library); \
        return false; \
    } \
} while (0)

    CUDA_SYMBOL(cuInit);
    CUDA_SYMBOL(cuDeviceGetCount);
    CUDA_SYMBOL(cuDeviceGet);
    CUDA_SYMBOL(cuDeviceGetName);
    CUDA_SYMBOL(cuDeviceGetUuid_v2);
    CUDA_SYMBOL(cuCtxCreate_v2);
    CUDA_SYMBOL(cuCtxSynchronize);
    CUDA_SYMBOL(cuCtxDestroy_v2);
    CUDA_SYMBOL(cuGetErrorName);

    result = cuInit(0);
    cuGetErrorName(result, &error_name);
    printf("CUINIT=code=%d name=%s\n", result, error_name);
    if (result != CUDA_SUCCESS)
        goto done;

    result = cuDeviceGetCount(&count);
    cuGetErrorName(result, &error_name);
    printf("CUDEVICEGETCOUNT=code=%d name=%s count=%d\n", result, error_name, count);
    if (result != CUDA_SUCCESS || count < 1)
        goto done;

    result = cuDeviceGet(&device, 0);
    cuGetErrorName(result, &error_name);
    printf("CUDEVICEGET=code=%d name=%s device=%d\n", result, error_name, device);
    if (result != CUDA_SUCCESS)
        goto done;

    result = cuDeviceGetName(name, sizeof(name), device);
    cuGetErrorName(result, &error_name);
    printf("CUDEVICEGETNAME=code=%d name=%s device_name=%s\n",
           result, error_name, name);
    if (result != CUDA_SUCCESS)
        goto done;

    result = cuDeviceGetUuid_v2(&uuid, device);
    cuGetErrorName(result, &error_name);
    printf("CUDEVICEGETUUID=code=%d name=%s uuid=", result, error_name);
    if (result == CUDA_SUCCESS)
        print_cuda_uuid(&uuid);
    putchar('\n');
    if (result != CUDA_SUCCESS)
        goto done;

    result = cuCtxCreate_v2(&context, 0, device);
    cuGetErrorName(result, &error_name);
    printf("CUCTXCREATE=code=%d name=%s context=%p\n", result, error_name, context);
    if (result != CUDA_SUCCESS)
        goto done;

    result = cuCtxSynchronize();
    cuGetErrorName(result, &error_name);
    printf("CUCTXSYNCHRONIZE=code=%d name=%s\n", result, error_name);
    if (result != CUDA_SUCCESS)
        goto destroy;

    hold.tv_sec = (time_t)hold_seconds;
    hold.tv_nsec = (long)((hold_seconds - (double)hold.tv_sec) * 1000000000.0);
    if (hold.tv_nsec < 0)
        hold.tv_nsec = 0;
    printf("CUDA_CONTEXT_HOLD_SECONDS=%.3f\n", hold_seconds);
    nanosleep(&hold, NULL);
    success = true;

destroy:
    result = cuCtxDestroy_v2(context);
    cuGetErrorName(result, &error_name);
    printf("CUCTXDESTROY=code=%d name=%s\n", result, error_name);
    if (result != CUDA_SUCCESS)
        success = false;

done:
    dlclose(library);
    printf("CUDA_CONTEXT_RESULT=%s\n", success ? "PASSED" : "BLOCKED");
    return success;
}

int main(int argc, char **argv)
{
    struct options options;
    bool allocated[256] = {false};
    struct open_result rdwr_results[4];
    int visible_count = 0;
    int failures = 0;
    int minor;
    bool cuda_success;
    const char *slurm_job_id;

    if (parse_options(argc, argv, &options) != 0)
        return 2;

    slurm_job_id = getenv("SLURM_JOB_ID");
    printf("PID=%ld UID=%ld GID=%ld\n", (long)getpid(), (long)getuid(), (long)getgid());
    printf("ENV SLURM_JOB_ID=%s\n", slurm_job_id ? slurm_job_id : "");
    printf("ENV SLURM_JOB_GPUS=%s\n", getenv("SLURM_JOB_GPUS") ?: "");
    printf("ENV SLURM_STEP_GPUS=%s\n", getenv("SLURM_STEP_GPUS") ?: "");
    printf("ENV CUDA_VISIBLE_DEVICES=%s\n", getenv("CUDA_VISIBLE_DEVICES") ?: "");
    printf("ENV NVIDIA_VISIBLE_DEVICES=%s\n", getenv("NVIDIA_VISIBLE_DEVICES") ?: "");
    print_cgroup();

    if (collect_nvml_minors(allocated, &visible_count) != 0)
        failures++;

    for (minor = 0; minor < 4; minor++) {
        char path[64];
        const char *role = role_for_minor(minor, allocated);
        snprintf(path, sizeof(path), "/dev/nvidia%d", minor);
        probe_open(path, role, "O_RDONLY", O_RDONLY);
        probe_open(path, role, "O_WRONLY", O_WRONLY);
        rdwr_results[minor] = probe_open(path, role, "O_RDWR", O_RDWR);
    }

    {
        const char *shared[] = {
            "/dev/nvidiactl",
            "/dev/nvidia-uvm",
            "/dev/nvidia-uvm-tools",
            "/dev/nvidia-modeset",
        };
        size_t index;
        for (index = 0; index < sizeof(shared) / sizeof(shared[0]); index++) {
            if (access(shared[index], F_OK) != 0)
                continue;
            probe_open(shared[index], "SHARED", "O_RDONLY", O_RDONLY);
            probe_open(shared[index], "SHARED", "O_WRONLY", O_WRONLY);
            probe_open(shared[index], "SHARED", "O_RDWR", O_RDWR);
        }
    }

    {
        DIR *directory = opendir("/dev/nvidia-caps");
        struct dirent *entry;
        if (directory != NULL) {
            while ((entry = readdir(directory)) != NULL) {
                char path[512];
                if (entry->d_name[0] == '.')
                    continue;
                snprintf(path, sizeof(path), "/dev/nvidia-caps/%s", entry->d_name);
                probe_open(path, "SHARED_CAPS", "O_RDONLY", O_RDONLY);
                probe_open(path, "SHARED_CAPS", "O_WRONLY", O_WRONLY);
                probe_open(path, "SHARED_CAPS", "O_RDWR", O_RDWR);
            }
            closedir(directory);
        }
    }

    if (options.expected_allocated >= 0 && visible_count != options.expected_allocated) {
        printf("ASSERTION_FAILURE=visible count %d != expected allocated %d\n",
               visible_count, options.expected_allocated);
        failures++;
    }

    if (options.assert_slurm_isolation) {
        if (slurm_job_id == NULL || slurm_job_id[0] == '\0') {
            puts("ASSERTION_FAILURE=SLURM_JOB_ID is absent");
            failures++;
        }
        for (minor = 0; minor < 4; minor++) {
            if (allocated[minor] && !rdwr_results[minor].opened) {
                printf("ASSERTION_FAILURE=allocated /dev/nvidia%d O_RDWR denied\n", minor);
                failures++;
            }
            if (!allocated[minor] &&
                (rdwr_results[minor].opened ||
                 (rdwr_results[minor].error_number != EACCES &&
                  rdwr_results[minor].error_number != EPERM))) {
                printf("ASSERTION_FAILURE=unallocated /dev/nvidia%d O_RDWR not denied\n",
                       minor);
                failures++;
            }
        }
    }

    cuda_success = run_cuda_context(options.hold_seconds);
    if (strcmp(options.expect_cuda, "success") == 0 && !cuda_success)
        failures++;
    if (strcmp(options.expect_cuda, "denied") == 0 && cuda_success)
        failures++;

    printf("GPU_DEVICE_CONTEXT_PROBE_RESULT=%s\n", failures == 0 ? "PASSED" : "FAILED");
    return failures == 0 ? 0 : 1;
}
