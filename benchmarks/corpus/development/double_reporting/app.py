import logging
def inner():
    try: raise ValueError()
    except Exception:
        logging.exception("failed")
        raise
def outer():
    try: inner()
    except Exception: logging.exception("operation failed")
