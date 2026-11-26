"""A Celery app for the tests: tasks run in the test process, no broker."""

from celery import Celery

app = Celery("tests")
app.conf.update(
    broker_url="memory://",
    result_backend="cache+memory://",
    task_always_eager=True,
    task_eager_propagates=True,
)
app.set_default()
