"""Scan-only example: uncertainty is retained rather than inventing execution edges."""


class Base:
    def prepare(self):
        pass


class Other:
    pass


class Workflow(Base, Other):
    def execute(self, client, callback):
        self.prepare()
        client.fetch()
        callback()


def run(client, callback):
    Workflow().execute(client, callback)


def scheduled(client, callback):
    Workflow().execute(client, callback)


def detached(client):
    client.fetch()
