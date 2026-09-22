import logging
def run():
    try: raise ValueError()
    except Exception:
        logging.warning("failed", exc_info=True)
        raise
