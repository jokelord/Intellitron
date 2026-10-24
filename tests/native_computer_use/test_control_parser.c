#define _POSIX_C_SOURCE 200809L
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>
#include "control.h"
#include <stdbool.h>

/* Parser-only in-memory I/O seam. The separate production IPC smoke test
 * verifies credentials and transport; this does not stand in for that gate. */
static const char *input_packet;
static char output_packet[4096];
ssize_t ipc_receive_control(int fd, void *buffer, size_t capacity, pid_t parent) {
    (void)fd; (void)parent;
    if (!input_packet) return 0;
    size_t length = strlen(input_packet);
    assert(length <= capacity);
    memcpy(buffer, input_packet, length);
    input_packet = NULL;
    return length;
}
ssize_t send(int fd, const void *buffer, size_t size, int flags) {
    (void)fd; (void)flags;
    assert(size < sizeof(output_packet));
    memcpy(output_packet, buffer, size);
    output_packet[size] = 0;
    return size;
}

int ipc_wait_control(int fd,unsigned timeout_ms) { (void)fd; (void)timeout_ms; return 1; }

bool ipc_send_frame(int fd,const void *data,size_t size,int frame_fd) { assert(frame_fd<0); return send(fd,data,size,0)==(ssize_t)size; }

#define NONCE "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
#define HELLO "{\"version\":1,\"generation\":1,\"sequence\":1,\"nonce\":\"" NONCE "\",\"kind\":\"hello\",\"payload\":{}}"

static void run(const char *packet, int expected, int reply_expected) {
    input_packet = packet;
    output_packet[0] = 0;
    assert(control_run(3, getpid()) == expected);
    if (reply_expected) {
        assert(strstr(output_packet, "SESSION_BUS_UNAVAILABLE"));
        assert(strstr(output_packet, "\"capture_ready\":false"));
        assert(strstr(output_packet, "\"input_ready\":false"));
    } else assert(output_packet[0] == 0);
}

int main(void) {
    setenv("XDG_SESSION_TYPE","wayland",1);
    setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/nonexistent/computer-use-test-bus", 1);
    run(HELLO, 0, 1);
    run("{\"version\":1,\"version\":1,\"generation\":1,\"sequence\":1,\"nonce\":\"" NONCE "\",\"kind\":\"hello\",\"payload\":{}}", 4, 0);
    run("{\"version\":true,\"generation\":1,\"sequence\":1,\"nonce\":\"" NONCE "\",\"kind\":\"hello\",\"payload\":{}}", 4, 0);
    run("{\"version\":1,\"generation\":1,\"sequence\":2,\"nonce\":\"" NONCE "\",\"kind\":\"hello\",\"payload\":{}}", 4, 0);
    run("{}", 4, 0);
    return 0;
}
