import asyncio
import io
import json
from unittest.mock import patch

from core import assessment_agent, assessment_model
from core.assessment_usage import collect_usage


def test_usage_spans_tools_and_async_review_without_double_counting():
    usage = []
    def response(envelope):
        stream = io.BytesIO(json.dumps(envelope).encode())
        stream.headers = {}
        return stream

    responses = [
        response({'status':'completed', 'output':[], 'usage':{
            'input_tokens':100, 'output_tokens':50, 'total_tokens':150,
            'output_tokens_details':{'reasoning_tokens':30}}}),
        response({'choices':[{'finish_reason':'stop', 'message':{'content':'{}'}}], 'usage':{
            'prompt_tokens':20, 'completion_tokens':6}}),
        response({'status':'completed', 'output':[]}),
    ]
    with patch.object(assessment_model._OPENER, 'open', side_effect=responses), collect_usage(usage.append):
        assessment_agent._request_response('test', {})
        asyncio.run(asyncio.to_thread(assessment_model._request_json, [], 'test'))
        assessment_agent._request_response('test', {})
    assert sum(row['total'] for row in usage) == 176
    assert sum(row['input'] for row in usage) == 120
    assert sum(row['output'] for row in usage) == 56
    assert sum(row['reported'] for row in usage) == 2
    assert sum(row['missing'] for row in usage) == 1
