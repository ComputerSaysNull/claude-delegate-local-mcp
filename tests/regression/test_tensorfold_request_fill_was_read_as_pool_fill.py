"""TensorFold's per-request KV fill was reported as pool fill.

TensorFold publishes `tensorfold:kv_cache_usage_perc{stream="N"}` once per running request,
and its help text says "Tokens in a stream cache divided by that stream's context window":
each series is one request's own context window, not the engine's KV pool, and TensorFold
publishes no pool-fill metric at all. `read_metrics` took the maximum over those series and
reported it under `kv_cache_used_fraction`, a key that means pool fill.
"""

from claude_delegate_local.backends import openai_compat as oc

BUSY = """\
tensorfold:kv_cache_usage_perc{stream="0"} 0.157021
tensorfold:kv_cache_usage_ratio{pool="0"} 0.157021
tensorfold:kv_cache_usage_perc{stream="1"} 0.093973
tensorfold:kv_cache_usage_ratio{pool="1"} 0.093973
tensorfold:kv_cache_usage_perc{stream="2"} 0.075603
tensorfold:kv_cache_usage_ratio{pool="2"} 0.075603
tensorfold:kv_cache_usage_perc{stream="3"} 0.11194
tensorfold:kv_cache_usage_ratio{pool="3"} 0.11194
"""


def test_tensorfold_stream_fill_is_not_pool_fill():
    got = oc.read_metrics(BUSY)
    assert "kv_cache_used_fraction" not in got
    assert got["kv_cache_fullest_request_fraction"] == 0.157021


def test_an_idle_engine_reads_zero_not_absent():
    got = oc.read_metrics('tensorfold:kv_cache_usage_perc{stream="0"} 0\n')
    assert got["kv_cache_fullest_request_fraction"] == 0
    assert "kv_cache_used_fraction" not in got


def test_vllms_pool_fill_still_reads():
    got = oc.read_metrics("vllm:kv_cache_usage_perc 0.5\n")
    assert got["kv_cache_used_fraction"] == 0.5
    assert "kv_cache_fullest_request_fraction" not in got
