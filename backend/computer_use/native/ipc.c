#define _GNU_SOURCE
#include "ipc.h"
#include <sys/socket.h>
#include <unistd.h>
#include <string.h>
#include <errno.h>
#include <poll.h>
bool ipc_validate_channel(int fd) {
    int type = 0, enabled = 1;
    socklen_t size = sizeof(type);
    struct sockaddr_storage addr;
    socklen_t addr_size = sizeof(addr);
    return getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &size) == 0
        && type == SOCK_SEQPACKET && getsockname(fd, (struct sockaddr *)&addr, &addr_size) == 0
        && addr.ss_family == AF_UNIX
        && setsockopt(fd, SOL_SOCKET, SO_PASSCRED, &enabled, sizeof(enabled)) == 0;
}
ssize_t ipc_receive_control(int fd, void *buffer, size_t capacity, pid_t parent_pid) {
    if (!buffer || !capacity || capacity > 65536) { errno = EINVAL; return -1; }
    union { struct cmsghdr align; char bytes[CMSG_SPACE(16*sizeof(int)) + CMSG_SPACE(sizeof(struct ucred))]; } control;
    struct iovec iov = {.iov_base=buffer, .iov_len=capacity};
    struct msghdr msg = {.msg_iov=&iov, .msg_iovlen=1, .msg_control=control.bytes, .msg_controllen=sizeof(control.bytes)};
    ssize_t count = recvmsg(fd, &msg, MSG_CMSG_CLOEXEC);
    if (count <= 0) return count;
    bool valid = !(msg.msg_flags & (MSG_TRUNC|MSG_CTRUNC));
    int credentials = 0;
    for (struct cmsghdr *c = CMSG_FIRSTHDR(&msg); c; c = CMSG_NXTHDR(&msg,c)) {
        if (c->cmsg_level == SOL_SOCKET && c->cmsg_type == SCM_RIGHTS) {
            size_t n = (c->cmsg_len - CMSG_LEN(0))/sizeof(int);
            int *fds = (int *)CMSG_DATA(c);
            for (size_t i=0;i<n;i++) close(fds[i]);
            valid = false;
        } else if (c->cmsg_level == SOL_SOCKET && c->cmsg_type == SCM_CREDENTIALS && c->cmsg_len == CMSG_LEN(sizeof(struct ucred))) {
            struct ucred peer;
            memcpy(&peer,CMSG_DATA(c),sizeof(peer));
            credentials++;
            if (peer.pid != parent_pid || peer.uid != getuid()) valid = false;
        } else valid = false;
    }
    if (!valid || credentials != 1) { errno = EPROTO; return -1; }
    return count;
}

bool ipc_send_frame(int fd,const void *data,size_t length,int frame_fd) {
    if(frame_fd<0)return send(fd,data,length,MSG_NOSIGNAL)==(ssize_t)length;
    union {struct cmsghdr align;char bytes[CMSG_SPACE(sizeof(int))];} control={0};
    struct iovec iov={.iov_base=(void *)data,.iov_len=length};
    struct msghdr msg={.msg_iov=&iov,.msg_iovlen=1,.msg_control=control.bytes,.msg_controllen=sizeof(control.bytes)};
    struct cmsghdr *c=CMSG_FIRSTHDR(&msg);c->cmsg_level=SOL_SOCKET;c->cmsg_type=SCM_RIGHTS;c->cmsg_len=CMSG_LEN(sizeof(int));
    memcpy(CMSG_DATA(c),&frame_fd,sizeof(frame_fd));
    return sendmsg(fd,&msg,MSG_NOSIGNAL)==(ssize_t)length;
}

int ipc_wait_control(int fd,unsigned timeout_ms) {
    struct pollfd watched={.fd=fd,.events=POLLIN};
    int result=poll(&watched,1,(int)timeout_ms);
    if(result<0&&errno==EINTR)return 0;
    return result;
}
