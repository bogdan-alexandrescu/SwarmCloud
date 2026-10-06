import time, pytest
_t = {}
@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    s = time.perf_counter()
    yield
    _t[collector.nodeid] = time.perf_counter() - s
def pytest_collection_finish(session):
    for k, v in sorted(_t.items(), key=lambda kv: -kv[1])[:25]:
        print(f"{v:8.3f} {k}")
