"""A second engine's metrics read as none, so `backend_status` said `cluster: null`.

The head node's newer engine publishes the scheduler's figures under a `tensorfold:`
prefix rather than `vllm:`, one of them renamed, and the allowlist knew only vLLM's names:
every line was ignored, the queue and KV gauges vanished, and the decode-rate counter that
`_DecodeWindow` differences was never seen. Its names are now on the allowlist beside
vLLM's, which still read as before.
"""

from claude_delegate_local.backends import openai_compat as oc

TENSORFOLD = """\
# HELP tensorfold:num_requests_running Requests running.
tensorfold:num_requests_running 3
tensorfold:num_requests_waiting 1
tensorfold:preemptions_total 2
tensorfold:prefix_cache_queries_total 5575805
tensorfold:prefix_cache_hits_total 2988032
tensorfold:generation_tokens_total 123456
tensorfold:request_time_per_output_token_seconds_sum 40.0
tensorfold:request_time_per_output_token_seconds_count 1000
tensorfold:pair_broken{pair="a"} 1
tensorfold:device_memory_bytes{device="0"} 9.0e10
"""


def test_the_scheduler_figures_are_read():
    got = oc.read_metrics(TENSORFOLD)
    assert got["requests_running"] == 3
    assert got["requests_waiting"] == 1
    assert got["preemptions"] == 2
    assert got["prefix_cache_query_tokens"] == 5575805
    assert got["prefix_cache_hit_tokens"] == 2988032
    assert got["generation_tokens"] == 123456
    assert got["decode_tokens_per_second_since_boot"] == 25.0


def test_names_off_the_allowlist_are_still_ignored():
    got = oc.read_metrics(TENSORFOLD)
    assert not any("pair" in k or "device" in k or "memory" in k for k in got)


def test_vllm_names_still_read():
    assert oc.read_metrics("vllm:num_requests_running 5\n")["requests_running"] == 5
