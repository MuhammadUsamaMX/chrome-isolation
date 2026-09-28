/*
 * libhwspoof.c — LD_PRELOAD shim that masks host-identity leaks.
 *
 * WHAT THIS DOES
 *   open/open64/openat/openat64/fopen/fopen64/reopen of a *specific allow-list*
 *   of paths are redirected to a faked file tree under $CHROME_SPOOF_ROOT.
 *   sysinfo() reports the faked total RAM, uname() the faked kernel, and
 *   getifaddrs() the faked MAC address.
 *
 * WHAT THIS DOES NOT DO — read this before trusting it
 *   procfs and sysfs are read-only in an unprivileged container and
 *   user-namespace creation is blocked by Docker's default seccomp profile, so
 *   the real files CANNOT be replaced, bind-mounted over or unlinked. This shim
 *   is therefore only a *partial* mitigation:
 *
 *     1. It only affects processes that resolve those paths through libc.
 *        A process issuing a raw `syscall(__NR_openat, ...)` (which is what
 *        Chromium's base:: filesystem helpers do) is NOT intercepted.
 *     2. It does not affect the kernel's view, /proc/self/maps, strace of
 *        another process, or anything already cached in a running process.
 *     3. Web content cannot read these paths at all, so the realistic
 *        exposure is to *native* code running inside the container.
 *
 * Nothing here may ever be fatal: every hook falls through to the real libc
 * function if the faked replacement is missing or unusable.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <ifaddrs.h>
#include <net/if.h>
#include <netinet/in.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/sysinfo.h>
#include <sys/utsname.h>
#include <unistd.h>

#define MAX_FAKE 4096

/*
 * musl's <ifaddrs.h> does not expose struct sockaddr_ll and the Linux UAPI
 * headers are not present in the runtime image. The layout is frozen kernel
 * ABI, so declaring it locally avoids pulling linux-headers into the build.
 * sll_halen / sll_addr sit at fixed offsets that must not be reordered.
 */
struct hwspoof_sockaddr_ll {
    unsigned short sll_family;
    unsigned short sll_protocol;
    int            sll_ifindex;
    unsigned short sll_hatype;
    unsigned char  sll_pkttype;
    unsigned char  sll_halen;
    unsigned char  sll_addr[8];
};

/*
 * Load probe. musl's loader IGNORES an LD_PRELOAD entry that cannot be loaded
 * and still execs the program with exit status 0, so "the process started" is
 * not evidence that the shim is active. hardware-spoof.sh sets
 * CHROME_SPOOF_PROBE to a path and checks the file afterwards. Raw syscalls
 * are used so the probe cannot recurse through our own open() hook.
 */
__attribute__((constructor))
static void hwspoof_probe(void)
{
    const char *p = getenv("CHROME_SPOOF_PROBE");
    long fd;

    if (!p || !*p)
        return;
    fd = syscall(SYS_openat, AT_FDCWD, p,
                 O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
    if (fd < 0)
        return;
    syscall(SYS_write, fd, "loaded\n", 7);
    syscall(SYS_close, fd);
}

static const char *spoof_root(void)
{
    const char *r = getenv("CHROME_SPOOF_ROOT");
    return (r && *r == '/') ? r : NULL;
}

/* Path allow-list. Only these are ever redirected. */
static int is_fake_file(const char *p)
{
    return p && (strcmp(p, "/proc/cpuinfo") == 0 ||
                 strcmp(p, "/proc/meminfo") == 0 ||
                 strcmp(p, "/proc/version") == 0);
}

static const char *dmi_prefixes[] = {
    "/sys/class/dmi/id/",
    "/sys/devices/virtual/dmi/id/",
};

/*
 * Map an allow-listed path onto its faked twin. Returns 1 and fills `out` when
 * a real faked file exists (so we never turn a readable path into ENOENT).
 */
static int translate(const char *path, char *out, size_t outsz)
{
    const char *root = spoof_root();
    size_t i;

    if (!root || !path || path[0] != '/')
        return 0;

    if (is_fake_file(path)) {
        if ((size_t)snprintf(out, outsz, "%s%s", root, path) < outsz &&
            access(out, F_OK) == 0)
            return 1;
        return 0;
    }
    for (i = 0; i < sizeof(dmi_prefixes) / sizeof(dmi_prefixes[0]); i++) {
        size_t plen = strlen(dmi_prefixes[i]);
        if (strncmp(path, dmi_prefixes[i], plen) == 0 && path[plen] != '\0') {
            if ((size_t)snprintf(out, outsz, "%s/sys/class/dmi/id/%s", root,
                                 path + plen) < outsz &&
                access(out, F_OK) == 0)
                return 1;
            return 0;
        }
    }
    return 0;
}

/* Resolve the real libc symbol; `goto unresolved` on failure so every hook
 * fails closed (no crash) and open to the caller. */
#define RESOLVE(ret, name, args)                       \
    static ret (*real_##name) args;                    \
    if (!real_##name)                                  \
        real_##name = dlsym(RTLD_NEXT, #name);         \
    if (!real_##name)                                  \
        goto unresolved;

int open(const char *path, int flags, ...)
{
    mode_t mode = 0;
    char fake[MAX_FAKE];
    va_list ap;

    RESOLVE(int, open, (const char *, int, ...))

    va_start(ap, flags);
    if (flags & (O_CREAT | O_TMPFILE))
        mode = (mode_t)va_arg(ap, int);
    va_end(ap);

    if (translate(path, fake, sizeof(fake)))
        return real_open(fake, flags, mode);
    return real_open(path, flags, mode);

unresolved:
    return -1;
}

int open64(const char *path, int flags, ...)
{
    mode_t mode = 0;
    char fake[MAX_FAKE];
    va_list ap;

    RESOLVE(int, open64, (const char *, int, ...))

    va_start(ap, flags);
    if (flags & (O_CREAT | O_TMPFILE))
        mode = (mode_t)va_arg(ap, int);
    va_end(ap);

    if (translate(path, fake, sizeof(fake)))
        return real_open64(fake, flags, mode);
    return real_open64(path, flags, mode);

unresolved:
    return -1;
}

static int openat_common(int dirfd, const char *path, int flags, mode_t mode,
                         int is64)
{
    char fake[MAX_FAKE];

    if (translate(path, fake, sizeof(fake)))
        path = fake;

    if (is64) {
        static int (*real_openat64)(int, const char *, int, ...);
        if (!real_openat64)
            real_openat64 = dlsym(RTLD_NEXT, "openat64");
        if (real_openat64)
            return real_openat64(dirfd, path, flags, mode);
        return -1;
    }
    {
        static int (*real_openat)(int, const char *, int, ...);
        if (!real_openat)
            real_openat = dlsym(RTLD_NEXT, "openat");
        if (real_openat)
            return real_openat(dirfd, path, flags, mode);
    }
    return -1;
}

int openat(int dirfd, const char *path, int flags, ...)
{
    mode_t mode = 0;
    va_list ap;

    RESOLVE(int, openat, (int, const char *, int, ...))

    va_start(ap, flags);
    if (flags & (O_CREAT | O_TMPFILE))
        mode = (mode_t)va_arg(ap, int);
    va_end(ap);

    return openat_common(dirfd, path, flags, mode, 0);

unresolved:
    return -1;
}

int openat64(int dirfd, const char *path, int flags, ...)
{
    mode_t mode = 0;
    va_list ap;

    RESOLVE(int, openat64, (int, const char *, int, ...))

    va_start(ap, flags);
    if (flags & (O_CREAT | O_TMPFILE))
        mode = (mode_t)va_arg(ap, int);
    va_end(ap);

    return openat_common(dirfd, path, flags, mode, 1);

unresolved:
    return -1;
}

FILE *fopen(const char *path, const char *mode)
{
    char fake[MAX_FAKE];

    RESOLVE(FILE *, fopen, (const char *, const char *))

    if (translate(path, fake, sizeof(fake)))
        return real_fopen(fake, mode);
    return real_fopen(path, mode);

unresolved:
    return NULL;
}

FILE *fopen64(const char *path, const char *mode)
{
    char fake[MAX_FAKE];

    RESOLVE(FILE *, fopen64, (const char *, const char *))

    if (translate(path, fake, sizeof(fake)))
        return real_fopen64(fake, mode);
    return real_fopen64(path, mode);

unresolved:
    return NULL;
}

/* ---------------------------------------------------------------- sysinfo */

int sysinfo(struct sysinfo *info)
{
    const char *kb = getenv("CHROME_MEM_TOTAL_KB");

    RESOLVE(int, sysinfo, (struct sysinfo *))

    if (real_sysinfo(info) != 0)
        return -1;

    if (kb && *kb) {
        char *end;
        unsigned long long v = strtoull(kb, &end, 10);
        if (*end == '\0' && v > 0) {
            unsigned long long bytes = v * 1024ULL;
            /* The container's *real* available memory must not grow. */
            if (bytes < (unsigned long long)info->totalram) {
                info->totalram   = bytes;
                info->freeram    = bytes / 2;
                info->sharedram  = bytes / 50;
                info->bufferram  = bytes / 33;
                info->totalswap  = bytes / 4;
                info->freeswap   = info->totalswap - 1024;
                info->totalhigh  = 0;
                info->freehigh   = 0;
                info->mem_unit   = 1;
            }
        }
    }
    return 0;

unresolved:
    return -1;
}

/* ------------------------------------------------------------------ uname */

int uname(struct utsname *buf)
{
    const char *rel = getenv("CHROME_KERNEL_RELEASE");

    RESOLVE(int, uname, (struct utsname *))

    if (real_uname(buf) != 0)
        return -1;

    if (rel && *rel) {
        strncpy(buf->release, rel, sizeof(buf->release) - 1);
        buf->release[sizeof(buf->release) - 1] = '\0';
        snprintf(buf->version, sizeof(buf->version),
                 "#1 SMP PREEMPT_DYNAMIC %s", rel);
    }
    /* nodename is deliberately left alone: the container hostname is owned by
     * docker_manager._profile_hostname(), a different concern. */
    return 0;

unresolved:
    return -1;
}

/* -------------------------------------------------------------- getifaddrs */

int getifaddrs(struct ifaddrs **ifap)
{
    const char *mac = getenv("CHROME_MAC_ADDRESS");
    unsigned char addr[6];
    unsigned int a[6];
    struct ifaddrs *ifa;
    int i;

    RESOLVE(int, getifaddrs, (struct ifaddrs **))

    if (real_getifaddrs(ifap) != 0)
        return -1;
    if (!mac || !mac[0])
        return 0;
    if (sscanf(mac, "%x:%x:%x:%x:%x:%x", &a[0], &a[1], &a[2], &a[3],
               &a[4], &a[5]) != 6)
        return 0;
    for (i = 0; i < 6; i++)
        addr[i] = (unsigned char)a[i];

    for (ifa = *ifap; ifa; ifa = ifa->ifa_next) {
        struct hwspoof_sockaddr_ll *sll;
        if (!ifa->ifa_addr || ifa->ifa_addr->sa_family != AF_PACKET)
            continue;
        sll = (struct hwspoof_sockaddr_ll *)ifa->ifa_addr;
        if (sll->sll_halen != 6)
            continue;
        memcpy(sll->sll_addr, addr, 6);
    }
    return 0;

unresolved:
    return -1;
}

void freeifaddrs(struct ifaddrs *ifa)
{
    static void (*real_freeifaddrs)(struct ifaddrs *);
    if (!real_freeifaddrs)
        real_freeifaddrs = dlsym(RTLD_NEXT, "freeifaddrs");
    if (real_freeifaddrs)
        real_freeifaddrs(ifa);
}
