"""An image inside a `tool` message is refused by the serving stack's vision patch, as written.

The patch is written to accept images in `user` messages only, and a refused image stays in
the history to fail every later turn; the live cluster has accepted one regardless, so this
holds the shape both accept. A tool result is sent as plain text that says its image follows,
and the image goes in the `user` message the adapter sends after the results.
"""

import base64

from claude_delegate_local.backends import base
from claude_delegate_local.backends.openai_compat import _wire_message

IMAGE = base.ImageBlock("image/png", b"\x89PNG fake", 1, 1)
URI = "data:image/png;base64," + base64.b64encode(b"\x89PNG fake").decode("ascii")


def wire(*blocks):
    return _wire_message(base.Message("user", blocks), resend_reasoning=False)


def test_a_tool_result_s_image_moves_to_the_user_message_after_it():
    out = wire(base.ToolResultBlock("call_1", "a.png: image/png", images=(IMAGE,)))
    assert [m["role"] for m in out] == ["tool", "user"]
    assert out[0]["content"] == (
        "a.png: image/png\n[The image is attached to the user message that follows.]"
    )
    assert out[1]["content"] == [
        {"type": "text", "text": "Image from the tool call call_1:"},
        {"type": "image_url", "image_url": {"url": URI}},
    ]


def test_no_tool_message_ever_carries_content_parts():
    out = wire(
        base.ToolResultBlock("call_1", "a.png", images=(IMAGE, IMAGE)),
        base.ToolResultBlock("call_2", "b.py: x = 1"),
        base.TextBlock("3 turns left"),
    )
    assert [m["role"] for m in out] == ["tool", "tool", "user"]
    assert all(isinstance(m["content"], str) for m in out if m["role"] == "tool")
    assert out[0]["content"].endswith(
        "[The 2 images are attached to the user message that follows.]"
    )
    assert out[1]["content"] == "b.py: x = 1"
    assert out[2]["content"] == [
        {"type": "text", "text": "Images from the tool call call_1:"},
        {"type": "image_url", "image_url": {"url": URI}},
        {"type": "image_url", "image_url": {"url": URI}},
        {"type": "text", "text": "3 turns left"},
    ]


def test_a_turn_without_images_is_sent_as_before():
    out = wire(base.ToolResultBlock("call_1", "b.py: x = 1"), base.TextBlock("3 turns left"))
    assert out == [
        {"role": "tool", "tool_call_id": "call_1", "content": "b.py: x = 1"},
        {"role": "user", "content": "3 turns left"},
    ]
