"""Static review fixture: scan this file; no dependency installation is needed."""

import logging

import requests


class Client:
    def fetch(self):
        return requests.get("https://example.invalid/data", timeout=5)


class Workflow:
    def __init__(self, client: Client):
        self.client = client

    def execute(self):
        return self.client.fetch()


def build_workflow():
    return Workflow(Client())


def run():
    try:
        return build_workflow().execute()
    except Exception:
        logging.exception("Workflow failed")
        raise


def refresh(enabled):
    try:
        return requests.post("https://example.invalid/refresh", timeout=5)
    except Exception:
        if enabled:
            logging.warning("Refresh failed", exc_info=True)
        return None
