"""Collect provider-reported usage within one assessment operation."""
from contextlib import contextmanager
from contextvars import ContextVar


_collector = ContextVar('assessment_usage', default=None)


@contextmanager
def collect_usage(report):
    token = _collector.set(report)
    try:
        yield
    finally:
        _collector.reset(token)


def record_usage(envelope):
    report = _collector.get()
    if report is None:
        return
    data = envelope.get('usage') if isinstance(envelope, dict) else None
    data = data if isinstance(data, dict) else {}
    incoming = data.get('input_tokens', data.get('prompt_tokens'))
    outgoing = data.get('output_tokens', data.get('completion_tokens'))
    total = data.get('total_tokens')
    def valid(value):
        return type(value) is int and value >= 0
    if not valid(total) and valid(incoming) and valid(outgoing):
        total = incoming + outgoing
    report(dict(total=total if valid(total) else 0, input=incoming if valid(incoming) else 0,
                output=outgoing if valid(outgoing) else 0, reported=int(valid(total)),
                missing=int(not valid(total)), split=int(valid(incoming) and valid(outgoing))))
