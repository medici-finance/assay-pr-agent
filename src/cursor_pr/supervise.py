"""Run one queued review for the poller, and end it when the poller is gone or its time is up.

The poller starts this, and this starts the review in a process group of its own. A review must
never run beside a later poller, which would start the same request again, and a review that hangs
must not keep the poller's lock for good. Both are decided here, outside the review, by a process
that only waits:

  the poller is gone (its end of the lifeline closes)  -> the review's group is ended
  the review runs past its time                        -> the review's group is ended
  the review ends                                      -> what it left running is ended

The poller's lock is held here until the group is gone, so no other poller starts before that.

Run by path, with only the standard library:  supervise.py LIFELINE SECONDS FDS COMMAND...
LIFELINE is a descriptor the poller holds the other end of. FDS is a comma-separated list of
descriptors to hand on to the review (the lock), or an empty string.
"""
import os
import select
import signal
import subprocess
import sys
import time

# How long a review asked to stop has to remove its temporary files before it is ended outright.
GRACE_SECONDS = 5
STOPPED = 124


def supervise(lifeline, seconds, keep, command):
    # A signal to this process is read from a pipe like everything else it waits for, so it cannot
    # arrive between two steps and end the supervisor with the review still running.
    woken, wake = os.pipe()
    os.set_blocking(wake, False)
    signal.set_wakeup_fd(wake, warn_on_full_buffer=False)
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, lambda *received: None)
    # The review holds one end of this pipe and nothing it starts does. It closes when the review ends.
    ended, alive = os.pipe()
    review = subprocess.Popen(command, pass_fds=(*keep, alive), start_new_session=True)
    os.close(alive)
    reason = None
    deadline = time.monotonic() + seconds
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            reason = f"it was still running after {seconds:g} seconds"
            break
        readable = select.select([lifeline, ended, woken], [], [], left)[0]
        if ended in readable:
            break
        if lifeline in readable:
            reason = "the poller that started it is gone"
            break
        if woken in readable:
            reason = "its supervisor was told to stop"
            break
    if reason:
        # Asked first, so that it removes its temporary files and ends the command it waits on.
        signal_group(review.pid, signal.SIGTERM)
        select.select([ended], [], [], GRACE_SECONDS)
    # The review has not been waited for yet, so its group cannot have become another's.
    signal_group(review.pid, signal.SIGKILL)
    status = review.wait()
    if reason:
        print(f"cursor-review: the review was stopped: {reason}", file=sys.stderr, flush=True)
        return STOPPED
    return status if status >= 0 else STOPPED


def signal_group(group, number):
    # Nothing is left to signal once every process of the group has ended. Linux then reports no such
    # process. macOS reports a refusal for a group whose only member has ended and waits to be read.
    try:
        os.killpg(group, number)
    except (ProcessLookupError, PermissionError):
        pass


def main(argv):
    lifeline, seconds = int(argv[0]), float(argv[1])
    keep = tuple(int(fd) for fd in argv[2].split(",") if fd)
    return supervise(lifeline, seconds, keep, argv[3:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
