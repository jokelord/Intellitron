#pragma once
#include <sys/types.h>
#include <signal.h>
extern volatile sig_atomic_t control_stop_requested;
int control_run(int fd, pid_t parent_pid);
