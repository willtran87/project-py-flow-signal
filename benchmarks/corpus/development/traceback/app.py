import logging
def run():
    try: raise ValueError()
    except Exception:
        logging.error("failed")
