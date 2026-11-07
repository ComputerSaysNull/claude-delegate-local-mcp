"""A model billing more per image was costed at the previous model's 512.

`TOKENS_PER_IMAGE` was one constant for every model, measured on a checkpoint that caps an
image at 384 tokens. A model that bills by resolution charged ~760 for a 1-megapixel tile,
so the prefetch, eviction and overflow estimates under-counted it -- the unsafe direction.
A registry entry may now say what an image costs on its model.
"""

import pytest

from claude_delegate_local import loop
from claude_delegate_local.backends.base import ImageBlock, Message, ToolResultBlock
from claude_delegate_local.config import TOKENS_PER_IMAGE
from claude_delegate_local.context import prefetch
from test_context import _png_bytes, cfg as context_cfg, write
from test_context_overflow import cfg as overflow_cfg
from test_registry import GOOD, build


def test_an_entry_may_set_its_image_cost(tmp_path):
    reg, _ = build(tmp_path, GOOD + "tokens_per_image = 800\n")
    assert reg.resolve(None).tokens_per_image == 800


def test_an_entry_without_it_keeps_the_constant(tmp_path):
    reg, _ = build(tmp_path, GOOD)
    assert reg.resolve(None).tokens_per_image == TOKENS_PER_IMAGE


def test_an_image_costing_nothing_is_refused(tmp_path):
    from claude_delegate_local import registry
    with pytest.raises(registry.RegistryError, match="tokens_per_image must be at least 1"):
        build(tmp_path, GOOD + "tokens_per_image = 0\n")


def test_the_prefetch_costs_an_image_at_the_entrys_rate(tmp_path):
    item = write(tmp_path, "pic.png", _png_bytes())
    result = prefetch(context_cfg(), (item,), max_images=8, tokens_per_image=800)
    assert result.files[0].est_tokens == 800


def test_the_loop_estimates_cost_an_image_at_the_entrys_rate():
    image = ImageBlock("image/png", b"\x89PNG fake", 1, 1)
    c = overflow_cfg()
    block = ToolResultBlock(tool_use_id="t", content="x", images=(image, image))
    plain = ToolResultBlock(tool_use_id="t", content="x")
    with_images = loop.estimate_message_tokens(
        c, Message("user", (block,)), tokens_per_image=800)
    without = loop.estimate_message_tokens(c, Message("user", (plain,)), tokens_per_image=800)
    assert with_images - without == 1600
    sizes = loop._tool_result_sizes(c, (Message("user", (block,)),), tokens_per_image=800)
    assert sizes[0] - loop._tool_result_sizes(c, (Message("user", (plain,)),))[0] == 1600
