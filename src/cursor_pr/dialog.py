"""TTY and browser transports share the same setup questions and validators."""
from contextvars import ContextVar

browser = ContextVar("setup_browser", default=None)


def prompt(label):
    session = browser.get()
    return session.ask(label) if session else input(label)


def notice(text):
    session = browser.get()
    if session:
        session.message(str(text))
    else:
        print(text)
