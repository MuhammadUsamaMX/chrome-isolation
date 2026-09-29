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
 * Nothing here may ever be fatal, and "never fatal" must not be implemented by
 * failing closed. An earlier revision returned -1/NULL from any hook whose
 * dlsym(RTLD_NEXT, ...) failed; on musl that made open() fail for every path,
 * so the dynamic loader could not map the browser and Chromium never started
 * (measured: `/bin/sh: can't open '/usr/bin/chromium-browser'`, exit 2). Every
 * hook now falls back to issuing the underlying syscall itself, which is what
 * libc was doing anyway, so a shim that cannot resolve degrades to "no
 * spoofing" rather than to "nothing works".
 *
 * Built and verified against musl (Alpine 3.24). musl's dlsym(RTLD_NEXT, ...)
 * resolves correctly here — that hypothesis was tested and is false. musl does
 * NOT export open64/openat64/fopen64 at all (and the linker refuses to resolve
 * them), so those three hooks are unreachable on this platform; they are kept
 * for glibc builds and fall through to the raw syscall if ever called. Which
 * symbols are optional, and the evidence for that claim, is documented on
 * `hwspoof_symbols` below.
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
 * The complete set of libc symbols this shim interposes, resolved ONCE in the
 * constructor (see hwspoof_probe below). Every hook reads its pointer from
 * here instead of calling dlsym() itself, which is what makes the load probe
 * meaningful: the probe measures the very same pointers the hooks will use.
 *
 * Before this, each hook resolved its own pointer lazily on first call, and the
 * constructor independently dlsym'd two of them ("open", "uname"). That is not
 * the same set, so a shim in which every RESOLVE failed still passed the
 * constructor's check on those two names and reported "loaded" — with
 * sysinfo(), fopen(), openat() and getifaddrs() all dead. (The behaviour was
 * still safe: every hook except getifaddrs() falls back to the raw syscall, so
 * the browser kept working. It was the *proof* that was wrong, not the code.)
 *
 * The struct is written through a per-symbol table so the probe can enumerate
 * exactly what the hooks need and report precisely which symbol failed.
 */
struct hwspoof_sym {
    const char *name;
    void       **slot;       /* where the resolved pointer is stored */
    int         optional;    /* 1 = absent on this libc, absence is normal */
    const char *why_optional;
};

static int (*real_open)(const char *, int, ...);
static int (*real_open64)(const char *, int, ...);
static int (*real_openat)(int, const char *, int, ...);
static int (*real_openat64)(int, const char *, int, ...);
static FILE *(*real_fopen)(const char *, const char *);
static FILE *(*real_fopen64)(const char *, const char *);
static int (*real_sysinfo)(struct sysinfo *);
static int (*real_uname)(struct utsname *);
static int (*real_getifaddrs)(struct ifaddrs **);
static void (*real_freeifaddrs)(struct ifaddrs *);

/*
 * "optional" is a claim about a SPECIFIC libc, and it has to be backed by
 * evidence rather than by recollection — hence the comments.
 *
 *   open64 / openat64 / fopen64
 *       musl does not export them at all. Evidence, not recollection: the
 *       Alpine 3.24 image's /lib/ld-musl-x86_64.so.1 was extracted and
 *       `nm -D --defined-only` run on it — it lists open, openat, fopen,
 *       sysinfo, uname, getifaddrs, freeifaddrs and fdopen, and no `*64`
 *       variant of any of them. Demanding these would condemn the shim on the
 *       very platform it is built for, so they are optional. They still have
 *       raw-syscall fallbacks, so on a glibc build (where they do resolve)
 *       they work as before.
 *
 *   getifaddrs / freeifaddrs
 *       Exported by musl, so they do resolve on the target platform. They are
 *       marked optional anyway, for a different reason: getifaddrs() has NO
 *       raw-syscall equivalent (it builds a malloc'd list inside libc), so an
 *       unresolved getifaddrs() degrades to a -1 return rather than to a
 *       crash, and that path is off everything the browser needs in order to
 *       start. A shim that can open, read and report but cannot rewrite the
 *       MAC is still worth loading; the gate must not throw it away. The
 *       same reasoning means a *musl* build must not be condemned over it
 *       either — hence optional rather than required.
 */
static struct hwspoof_sym hwspoof_symbols[] = {
    { "open",         (void **)&real_open,         0, NULL },
    { "openat",       (void **)&real_openat,       0, NULL },
    { "fopen",        (void **)&real_fopen,        0, NULL },
    { "sysinfo",      (void **)&real_sysinfo,      0, NULL },
    { "uname",        (void **)&real_uname,        0, NULL },
    { "open64",       (void **)&real_open64,       1, "not exported by musl" },
    { "openat64",     (void **)&real_openat64,     1, "not exported by musl" },
    { "fopen64",      (void **)&real_fopen64,      1, "not exported by musl" },
    { "getifaddrs",   (void **)&real_getifaddrs,   1, "no raw-syscall fallback; "
                                                      "MAC rewrite is optional" },
    { "freeifaddrs",  (void **)&real_freeifaddrs,  1, "pairs with getifaddrs" },
};

#define HWSHIM_SYMBOL_COUNT \
    ((int)(sizeof(hwspoof_symbols) / sizeof(hwspoof_symbols[0])))

/*
 * Set to 1 while every REQUIRED libc pointer resolved, and cleared to 0 by the
 * load probe the moment any of them fails. hardware-spoof.sh reads the
 * constructor's probe FILE to decide whether to export LD_PRELOAD, so the
 * probe verdict — not this variable alone — is what "the shim is usable"
 * means at runtime. Declared here because the constructor below is the first
 * thing that writes it.
 */
int hwspoof_hooks_usable = 1;

/*
 * Load probe.
 *
 * musl's loader IGNORES an LD_PRELOAD entry that cannot be loaded and still
 * execs the program with exit status 0, so "the process started" is not
 * evidence that the shim is active. hardware-spoof.sh sets CHROME_SPOOF_PROBE
 * to a path and checks the file afterwards. Raw syscalls are used so the probe
 * cannot recurse through our own open() hook.
 *
 * WHAT THIS ACTUALLY PROVES, precisely — the earlier version of this comment
 * over-claimed, and the code with it:
 *
 *   It resolves every symbol in hwspoof_symbols, through the same table the
 *   hooks read, and reports "loaded" only if every REQUIRED one resolved. So
 *   "loaded" means: the loader accepted the object, and every hook this libc
 *   is expected to provide is live and will interpose.
 *
 *   It does NOT prove the shim's *effects* — that /proc/meminfo is actually
 *   redirected, that a specific path is allow-listed, or that the spoof tree
 *   exists. Those depend on CHROME_SPOOF_ROOT and on the callers actually going
 *   through libc, which is precisely the gap this shim cannot close (see the
 *   header comment). scripts/verify.sh check 4 measures the effects end to end;
 *   this probe is a build-time gate on the mechanism, not on the outcome.
 *
 *   An earlier revision wrote "loaded" unconditionally, which let a shim whose
 *   dlsym(RTLD_NEXT) calls all failed pass the build gate and ship. A shim that
 *   reports itself broken is safe; one that lies about being healthy is not.
 */
__attribute__((constructor))
static void hwspoof_probe(void)
{
    const char *p = getenv("CHROME_SPOOF_PROBE");
    int i;
    long fd;
    const char *msg;
    int len;

    /* Resolve every symbol the hooks will use, before any hook can run, and
     * decide the verdict from the result. An unresolved REQUIRED symbol means
     * the shim silently intercepts nothing, which is a different thing from
     * "loaded" and must not be reported as such. */
    for (i = 0; i < HWSHIM_SYMBOL_COUNT; i++) {
        void *sym = dlsym(RTLD_NEXT, hwspoof_symbols[i].name);
        *hwspoof_symbols[i].slot = sym;
        if (!sym && !hwspoof_symbols[i].optional)
            hwspoof_hooks_usable = 0;
    }

    if (!p || !*p)
        return;

    if (hwspoof_hooks_usable) {
        msg = "loaded\n";
        len  = 7;
    } else {
        /* Distinct marker: the script must treat this as "not usable" and leave
         * LD_PRELOAD unset, so the browser runs uninstrumented. */
        msg = "unusable\n";
        len  = 9;
    }
    fd = syscall(SYS_openat, AT_FDCWD, p,
                 O_WRONLY | O_CREAT | O_APPEND | O_CLOEXEC, 0644);
    if (fd < 0)
        return;
    syscall(SYS_write, fd, msg, len);
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

/*
 * Symbol resolution.
 *
 * The original version of this macro jumped to an `unresolved:` label that did
 * `return -1;` / `return NULL;` when dlsym(RTLD_NEXT, ...) came back NULL.
 * That is NOT a safe failure mode, and it was demonstrably not one. A shim that
 * loads but cannot resolve its libc pointers makes every hooked function fail.
 * Measured on Alpine 3.24 / musl, a preload whose hooks all resolved to NULL
 * produced:
 *
 *     cat: can't open '/etc/hostname'
 *     /bin/sh: can't open '/usr/bin/chromium-browser'
 *
 * The browser could not even be exec'd, because the dynamic loader's own open()
 * calls were being answered with -1. The browser did not start. A "fail closed"
 * that prevents the browser from launching is strictly worse than no shim at
 * all, so the fallback must preserve the REAL behaviour instead.
 *
 * Each hook below therefore falls back to issuing the underlying syscall
 * directly, which is precisely what libc did before we interposed on it. That
 * keeps a partially-resolving shim harmless — the browser starts either way.
 *
 * The RESOLVE macro therefore has a single job left: detect that a hook's
 * pointer is NULL and jump to the fallback. It no longer calls dlsym, because
 * the constructor already resolved every symbol through the same
 * `hwspoof_symbols` table the probe checks. Two consequences, both deliberate:
 *
 *   * The probe and the hooks can no longer disagree. Previously the hooks
 *     resolved lazily and the probe dlsym'd two names separately, so a hook
 *     could be dead while the probe still reported "loaded".
 *   * Falling back no longer clears `hwspoof_hooks_usable`. That variable is a
 *     load-time verdict, computed in the constructor before any hook runs;
 *     a hook reaching its fallback afterwards does not retroactively change
 *     what the probe file said, and pretending otherwise is what made the
 *     earlier comment at this site wrong. The fallback is the *correct* and
 *     expected behaviour on a partially-supporting libc.
 */
#define RESOLVE(ret, name, args)                       \
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
    /* Not a failure: do what libc would have done. Never return a bare -1
     * here — the loader calls open() to map every object it loads. */
    return (int)syscall(SYS_openat, AT_FDCWD, path, flags, mode);
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
    return (int)syscall(SYS_openat, AT_FDCWD, path, flags, mode);
}

static int openat_common(int dirfd, const char *path, int flags, mode_t mode,
                         int is64)
{
    char fake[MAX_FAKE];

    if (translate(path, fake, sizeof(fake)))
        path = fake;

    if (is64) {
        if (real_openat64)
            return real_openat64(dirfd, path, flags, mode);
    } else {
        if (real_openat)
            return real_openat(dirfd, path, flags, mode);
    }
    /* musl exports neither openat64 nor a usable fallback symbol; go direct. */
    return (int)syscall(SYS_openat, dirfd, path, flags, mode);
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
    return (int)syscall(SYS_openat, dirfd, path, flags, mode);
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
    return (int)syscall(SYS_openat, dirfd, path, flags, mode);
}

FILE *fopen(const char *path, const char *mode)
{
    char fake[MAX_FAKE];

    RESOLVE(FILE *, fopen, (const char *, const char *))

    if (translate(path, fake, sizeof(fake)))
        return real_fopen(fake, mode);
    return real_fopen(path, mode);

unresolved:
    /* Open the fd ourselves and wrap it, so a failed resolution degrades to
     * normal libc behaviour instead of "this file can never be opened". */
    {
        int fd = (int)syscall(SYS_openat, AT_FDCWD, path, O_RDONLY);
        if (fd < 0)
            return NULL;
        return fdopen(fd, mode);
    }
}

FILE *fopen64(const char *path, const char *mode)
{
    char fake[MAX_FAKE];

    RESOLVE(FILE *, fopen64, (const char *, const char *))

    if (translate(path, fake, sizeof(fake)))
        return real_fopen64(fake, mode);
    return real_fopen64(path, mode);

unresolved:
    {
        int fd = (int)syscall(SYS_openat, AT_FDCWD, path, O_RDONLY);
        if (fd < 0)
            return NULL;
        return fdopen(fd, mode);
    }
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
    /* No spoofing, but the caller still gets its real memory figures. */
    return (int)syscall(SYS_sysinfo, info);
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
    /* No spoofing, but a working uname() is far better than a failed one:
     * callers (coreutils, busybox) print garbage on a short/failed return. */
    return (int)syscall(SYS_uname, buf);
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
    /* getifaddrs has no raw-syscall equivalent — it builds a malloc'd list
     * inside libc — so there is nothing to fall back to. Reporting failure is
     * the honest answer, and it is off every crash path: the MAC is a
     * fingerprint nicety, not something the browser cannot start without. */
    return -1;
}

void freeifaddrs(struct ifaddrs *ifa)
{
    if (real_freeifaddrs)
        real_freeifaddrs(ifa);
}
