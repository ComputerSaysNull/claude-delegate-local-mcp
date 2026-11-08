"""A second engine's KV-cache usage was left unread, so `backend_status` never showed it.

TensorFold publishes `kv_cache_usage_perc` once per running request, with a `stream` label,
on the same 0-1 scale as vLLM's (it equalled `kv_cache_usage_ratio` in every sample). Each
series is one request's share of its own window, so the figure is their maximum, not the
last line parsed or a sum; why it is not pool fill is the sibling test's.
"""

from claude_delegate_local.backends import openai_compat as oc

BUSY = """\
tensorfold:kv_cache_usage_ratio{pool="0"} 0.157023
tensorfold:kv_cache_usage_perc{stream="1"} 0.294061
tensorfold:kv_cache_usage_perc{stream="0"} 0.157023
tensorfold:kv_cache_usage_ratio{pool="1"} 0.294061
"""


def test_the_fullest_request_is_reported_whatever_the_order():
    assert oc.read_metrics(BUSY)["kv_cache_fullest_request_fraction"] == 0.294061
    flipped = "\n".join(reversed(BUSY.splitlines())) + "\n"
    assert oc.read_metrics(flipped)["kv_cache_fullest_request_fraction"] == 0.294061


def test_vllms_single_series_still_reads():
    assert oc.read_metrics("vllm:kv_cache_usage_perc 0.5\n")["kv_cache_used_fraction"] == 0.5
