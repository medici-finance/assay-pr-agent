"""TTY and browser transports share the same setup questions and validators."""
from contextvars import ContextVar

browser = ContextVar("setup_browser", default=None)

# The manager uses this child exit status to close the attached session.
QUIT_MANAGER = 20


class QuitSetup(Exception):
    pass


def terminal_input(label=""):
    value = input(label)
    if value.strip().lower() == "/quit":
        raise QuitSetup
    return value


def prompt(label):
    session = browser.get()
    return session.ask(label) if session else terminal_input(label)


def notice(text):
    session = browser.get()
    if session:
        session.message(str(text))
    else:
        print(text)
