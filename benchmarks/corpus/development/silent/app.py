import requests, logging
def fetch():
    return requests.get('url')
def run():
    try:
        return fetch()
    except Exception:
        return None
