import logging
from storage import read_order, save_order
def process(path, recover):
    try:
        order = read_order(path)
    except Exception:
        if recover:
            logging.warning('using empty order', exc_info=True)
            order = {}
        else:
            logging.exception('order unavailable')
            raise
    save_order(path, order)
def quiet(path):
    try: return read_order(path)
    except Exception: return {}
