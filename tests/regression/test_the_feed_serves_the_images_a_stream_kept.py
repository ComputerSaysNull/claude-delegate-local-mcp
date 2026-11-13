"""The feed served a stream's events but not the images that stream kept.

`Stream` copies every image sent to the model into `<stem>.images/`, named from the
sha256 of the bytes, and a reader on another machine rendering those events had no way
to fetch the picture: the feed knew only `/list` and `/events`, so every image URL was a
404. These tests are red until `GET /image?stream=<stem>.jsonl&file=<name>` exists. Each
refusal case carries its valid twin in the same test, so a refusal cannot pass merely
because the route is absent: the valid request has to come back 200 first.
"""

import io
import urllib.parse
from datetime import datetime, UTC
from pathlib import Path

from PIL import Image as PILImage

from claude_delegate_local import transcript
from claude_delegate_local.backends.base import ImageBlock
from test_watch_feed import _get, running, stream, viewer  # noqa: F401  (fixtures)

# The media type each kept extension promises, and the media type `image_name` writes for
# it. `image/tiff` is the one this module does not know, so it lands as `bin`.
EXT_MEDIA = {
    "image/png": ("PNG", "image/png"),
    "image/jpeg": ("JPEG", "image/jpeg"),
    "image/gif": ("GIF", "image/gif"),
    "image/webp": ("WEBP", "image/webp"),
    "image/tiff": ("TIFF", "application/octet-stream"),
}


def _encoded(fmt: str, colour: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    """A real image of `fmt`, built by Pillow rather than written as a literal."""
    buf = io.BytesIO()
    PILImage.new("RGB", (2, 2), colour).save(buf, format=fmt)
    return buf.getvalue()


def _keep(path: Path, media_type: str = "image/png", fmt: str = "PNG") -> tuple[str, bytes]:
    """The copy `Stream` would keep for one image sent in the stream at `path`."""
    data = _encoded(fmt)
    block = ImageBlock(media_type, data, 2, 2)
    name = transcript.image_name(block)
    folder = transcript.Stream(path).images_dir
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(data)
    return name, data


def _kept(tmp_path: Path, media_type: str = "image/png", fmt: str = "PNG",
          task: str = "KEPT") -> tuple[Path, str, bytes]:
    """A stream file, and the copy `Stream` would keep for one image sent in it."""
    path = stream(tmp_path, datetime.now(UTC), task)
    name, data = _keep(path, media_type, fmt)
    return path, name, data


def _url(stream_name: str, file_name: str) -> str:
    return "/image?" + urllib.parse.urlencode({"stream": stream_name, "file": file_name})


def _fetch(host: str, port: int, path: str, header_host: str | None = None):
    """`_get`'s request, keeping the response headers so a media type can be read."""
    import http.client

    conn = http.client.HTTPConnection(host, port)
    conn.request("GET", path, headers={"Host": header_host} if header_host else {})
    resp = conn.getresponse()
    body = resp.read()
    headers = {key.lower(): value for key, value in resp.getheaders()}
    conn.close()
    return resp.status, headers, body


def _media_type(headers: dict) -> str:
    return headers.get("content-type", "").split(";")[0].strip()


def test_a_kept_image_is_served_with_its_bytes_and_the_media_type_its_name_promises(
        tmp_path, running):  # noqa: F811
    """A reader renders the images a stream lists, and the bytes it gets must be the ones
    the model was shown: the copy's name is the digest of exactly those bytes, so anything
    else -- a re-encode, a truncated file, the wrong file -- is a picture nobody sent."""
    path, name, data = _kept(tmp_path)
    host, port = running()
    status, headers, body = _fetch(host, port, _url(path.name, name))
    assert status == 200, "the image route answered " + str(status)
    assert body == data, "the bytes served are not the bytes the name digests"
    assert _media_type(headers) == "image/png", headers.get("content-type")

    for media_type, (fmt, served) in EXT_MEDIA.items():
        if media_type == "image/png":
            continue
        other, _ = _keep(path, media_type, fmt)
        status, headers, _ = _fetch(host, port, _url(path.name, other))
        assert status == 200, media_type
        assert _media_type(headers) == served, media_type


def test_a_file_name_that_climbs_out_of_the_images_folder_is_refused(tmp_path, running):  # noqa: F811
    """`file` arrives from a URL. A relative path is not a name, and `..` would let a
    reader open anything beside the folder; the valid twin proves the refusal is not the
    whole route missing."""
    path, name, _ = _kept(tmp_path)
    host, port = running()
    assert _get(host, port, _url(path.name, name))[0] == 200, "the valid name was refused"
    assert _get(host, port, _url(path.name, "../" + path.name))[0] == 404


def test_an_absolute_file_name_is_refused(tmp_path, running):  # noqa: F811
    """An absolute path names a file anywhere on the host, which is more than this feed
    promises to serve."""
    path, name, _ = _kept(tmp_path)
    image = transcript.Stream(path).images_dir / name
    host, port = running()
    assert _get(host, port, _url(path.name, name))[0] == 200, "the valid name was refused"
    assert _get(host, port, _url(path.name, str(image)))[0] == 404


def test_a_symlink_pointing_out_of_the_images_folder_is_refused(
        tmp_path, tmp_path_factory, running):  # noqa: F811
    """A kept copy can be replaced by a symlink to anything. The name still matches, so
    only resolving the path catches it: the file served must be inside the folder."""
    path, name, _ = _kept(tmp_path)
    other = _encoded("PNG", (200, 0, 0))  # different bytes, so a different name
    outside = tmp_path_factory.mktemp("outside") / "elsewhere.png"
    outside.write_bytes(other)
    linked = transcript.image_name(ImageBlock("image/png", other, 2, 2))
    assert linked != name
    (transcript.Stream(path).images_dir / linked).symlink_to(outside)

    host, port = running()
    assert _get(host, port, _url(path.name, name))[0] == 200, "the real file was refused"
    assert _get(host, port, _url(path.name, linked))[0] == 404


def test_the_stream_file_itself_is_not_an_image(tmp_path, running):  # noqa: F811
    """`<stem>.jsonl` sits beside the folder, not in it, and its extension is not one of
    the five; asking for it must not serve a transcript as a picture."""
    path, name, _ = _kept(tmp_path)
    host, port = running()
    assert _get(host, port, _url(path.name, name))[0] == 200, "the valid name was refused"
    assert _get(host, port, _url(path.name, path.name))[0] == 404


def test_a_stream_name_that_is_refused_by_the_transcript_guard_is_refused_here(
        tmp_path, running):  # noqa: F811
    """The stream name is a transcript name, so it goes through `transcript_path`: `..`
    must be refused here as it is on `/events`, or the image route would read a folder
    outside the one the feed was pointed at."""
    path, name, _ = _kept(tmp_path)
    host, port = running()
    assert _get(host, port, _url(path.name, name))[0] == 200, "the valid stream was refused"
    assert _get(host, port, _url("../" + path.name, name))[0] == 404


def test_the_image_route_sits_behind_the_host_check(tmp_path, running):  # noqa: F811
    """The streams carry task text and model replies, and an image is a model's reply: a
    rebinding page must be refused the pictures as it is refused the events. The negative
    control is the same request to a feed that was not told the name."""
    path, name, _ = _kept(tmp_path)
    url = _url(path.name, name)

    host, port = running(("viewer-host.example.org",))
    allowed = _get(host, port, url, "viewer-host.example.org")
    assert allowed[0] == 200, "the allowed host was refused"
    assert _get(host, port, url, "evil.example.org")[0] == 403

    host, port = running()
    assert _get(host, port, url, "viewer-host.example.org")[0] == 403
