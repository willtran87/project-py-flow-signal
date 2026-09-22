from service import process, quiet
def checkout(path): return process(path, False)
def scheduled(path): return process(path, True)
def preview(path): return quiet(path)
