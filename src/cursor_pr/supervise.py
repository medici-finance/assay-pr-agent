"""Run one queued review for the poller, and end it when the poller is gone or its time is up.

The poller starts this as the leader of a new process group, and this starts the review in it. A
review must never run beside a later poller, which would start the same request again, and a review
that hangs must not keep the poller's lock for good. Both are decided here, outside the review, by
a process that only waits:

  the poller is gone (its end of the lifeline closes)  -> the review is ended
  the review runs past its time                        -> the review is ended
  the review ends                                      -> what it left running is ended

This process, the review and all the review starts are one process group, apart from the poller's.
A signal meant for the poller's group, from a terminal or from an operator, therefore reaches
neither, and the poller's death is seen here on the lifeline, whatever ended it. The group ends
together: the last thing this process does is end its own group, itself included. The poller's lock
is held here until then, so no other poller starts before the review is gone. The poller knows the
group by this process's number and ends it itself if this process cannot.

Run by path, with only the standard library:  supervise.py LIFELINE SECONDS FDS COMMAND...
LIFELINE is a descriptor the poller holds the other end of. FDS is a comma-separated list of
descriptors to hand on to the review (the lock), or an empty string.
"""
import os
import selectors
import signal
import subprocess
import sys
import time

# How long a review asked to stop has to remove its temporary files before it is ended outright.
GRACE_SECONDS = 5
STOPPED = 124


def supervise(lifeline, seconds, keep, command):
    group = os.getpgrp()
    if group != os.getpid():
        # Ending its group would then end whoever started it.
        print("cursor-review: the supervisor must be started as the leader of its own process group", file=sys.stderr)
        return STOPPED
    # A signal to this process is read from a pipe like everything else it waits for, so it cannot
    # arrive between two steps and end the supervisor with the review still running. One that this
    # process was started to ignore stays ignored. SIGTERM is always taken: this process sends it
    # to its own group and must outlive that.
    woken, wake = os.pipe()
    os.set_blocking(wake, False)
    signal.set_wakeup_fd(wake, warn_on_full_buffer=False)
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGQUIT):
        if number == signal.SIGTERM or signal.getsignal(number) != signal.SIG_IGN:
            signal.signal(number, lambda *received: None)
    # The review holds one end of this pipe and nothing it starts does. It closes when the review ends.
    ended, alive = os.pipe()
    review = subprocess.Popen(command, pass_fds=(*keep, alive))
    try:
        os.close(alive)
        reason = watch(lifeline, ended, woken, seconds)
        if reason:
            # Said first: this process ends with its group.
            print(f"cursor-review: the review was stopped: {reason}", file=sys.stderr, flush=True)
            # Asked first, so that it removes its temporary files and ends the command it waits on.
            os.killpg(group, signal.SIGTERM)
            wait_for(ended, GRACE_SECONDS)
    except BaseException as failure:
        # Whatever goes wrong with the waiting, the review is not left running without it.
        print(f"cursor-review: the review was stopped: its supervisor failed: {failure!r}", file=sys.stderr, flush=True)
    finally:
        try:
            # The review itself first, and waited for, so that it is not left for another to wait for.
            review.kill()
            review.wait()
        finally:
            # Then the group: whatever the review started, and this process with it.
            os.killpg(group, signal.SIGKILL)
    return STOPPED


def watch(lifeline, ended, woken, seconds):
    """Wait for the review to end. Return why it has to be stopped, or None when it ended by itself."""
    # Not select(): it refuses a descriptor numbered 1024 or more, and the lifeline has whatever
    # number it was given in the poller.
    with selectors.DefaultSelector() as waiting:
        for descriptor in (lifeline, ended, woken):
            waiting.register(descriptor, selectors.EVENT_READ)
        deadline = time.monotonic() + seconds
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return f"it was still running after {seconds:g} seconds"
            readable = {key.fd for key, _ in waiting.select(left)}
            if ended in readable:
                return None
            if lifeline in readable:
                return "the poller that started it is gone"
            if woken in readable:
                return "its supervisor was told to stop"


def wait_for(descriptor, seconds):
    with selectors.DefaultSelector() as waiting:
        waiting.register(descriptor, selectors.EVENT_READ)
        deadline = time.monotonic() + seconds
        while (left := deadline - time.monotonic()) > 0 and not waiting.select(left):
            pass


def main(argv):
    lifeline, seconds = int(argv[0]), float(argv[1])
    keep = tuple(int(fd) for fd in argv[2].split(",") if fd)
    return supervise(lifeline, seconds, keep, argv[3:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
