#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <sys/types.h>
bool ipc_validate_channel(int fd);
/* Receives only zero-FD control packets, closes every unexpected received FD.
 * Caller must parse/version/check nonce, generation and sequence before action.
 * The diagnostic helper intentionally has no input dispatch entrypoint. */
ssize_t ipc_receive_control(int fd, void *buffer, size_t capacity, pid_t parent_pid);

bool ipc_send_frame(int fd,const void *data,size_t length,int frame_fd);

int ipc_wait_control(int fd,unsigned timeout_ms);
