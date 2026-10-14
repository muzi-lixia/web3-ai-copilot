"""并发压测客户端的协议处理与统计，不调用真实模型。"""

import httpx

from scripts.benchmark_agent import run_turn, summarize


async def test_benchmark_reads_done_timings_without_returning_content_or_token():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(202, json={"data": {"turn_id": "t"}})
        return httpx.Response(
            200,
            text='data: {"type":"text_delta","message":{"content":"sensitive"}}\n\n'
            'data: {"type":"done","status":"completed","timings":{"queue_ms":12}}\n\n',
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://test") as client:
        result = await run_turn(client, "private-token", "question")
    assert result["status"] == "completed" and result["queue_ms"] == 12
    assert "private-token" not in str(result) and "sensitive" not in str(result)
    assert summarize([result], 2)["completed_per_second"] == 0.5


async def test_benchmark_reports_rejection_without_treating_it_as_success():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(429)), base_url="http://test"
    ) as client:
        result = await run_turn(client, "token", "q")
    assert result["status"] == "http_429"
    summary = summarize([result], 1)
    assert summary["completed_per_second"] == 0 and summary["queue_ms_p95"] is None
