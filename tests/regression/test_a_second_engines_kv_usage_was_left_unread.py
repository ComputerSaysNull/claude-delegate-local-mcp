"""A second engine's KV-cache usage was left unread, so `backend_status` never showed it.

TensorFold publishes `kv_cache_usage_perc` once per pool, with a `stream` label, on the
same 0-1 scale as vLLM's (it equalled `kv_cache_usage_ratio` in every sample). Under load
two pools read 0.157 and 0.294 at once; the fullest pool is the one that stops new work,
so the figure is their maximum, not the last line parsed or a sum.
"""

from claude_delegate_local.backends import openai_compat as oc

BUSY = """\
tensorfold:kv_cache_usage_ratio{pool="0"} 0.157023
tensorfold:kv_cache_usage_perc{stream="1"} 0.294061
tensorfold:kv_cache_usage_perc{stream="0"} 0.157023
tensorfold:kv_cache_usage_ratio{pool="1"} 0.294061
"""


def test_the_fullest_pool_is_reported_whatever_the_order():
    assert oc.read_metrics(BUSY)["kv_cache_used_fraction"] == 0.294061
    flipped = "\n".join(reversed(BUSY.splitlines())) + "\n"
    assert oc.read_metrics(flipped)["kv_cache_used_fraction"] == 0.294061


def test_an_idle_engine_reads_zero_not_absent():
    got = oc.read_metrics('tensorfold:kv_cache_usage_perc{stream="0"} 0\n')
    assert got["kv_cache_used_fraction"] == 0


def test_vllms_single_series_still_reads():
    assert oc.read_metrics("vllm:kv_cache_usage_perc 0.5\n")["kv_cache_used_fraction"] == 0.5
