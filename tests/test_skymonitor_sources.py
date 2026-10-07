from __future__ import annotations

import base64
import io
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import replace
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
import pytest
from PIL import Image
from scipy import ndimage

from skymonitor.analysis import AnalysisParams, CameraAnalyzer, Verdict, _near
from skymonitor.sources import (
    FfmpegSource,
    FileSource,
    FrameBurst,
    Freshness,
    SnapshotSource,
    SourceError,
    _parse_pgm,
    decode_image,
    find_ffmpeg,
    redact,
)
from skymonitor.stars import detect_stars
from skymonitor.synthetic import SkyScene, cloud_bank
from skymonitor_helpers import NIGHT

# Stands in for ffmpeg: writes binary PGM frames the way ``-c:v pgm -f image2pipe`` does.
FAKE_FFMPEG = r"""
import sys, time

mode = sys.argv[1]
out = sys.stdout.buffer


def frame(value, width=64, height=48):
    out.write(b"P5\n%d %d\n255\n" % (width, height) + bytes([value]) * (width * height))
    out.flush()


if mode == "frames":
    for index in range(8):
        frame(10 * index)
elif mode == "slow-camera":
    for value in (10, 10, 10, 10, 10, 10, 90, 90):
        frame(value)
elif mode == "frozen":
    for index in range(8):
        frame(90)
elif mode == "hang":
    time.sleep(60)
elif mode == "one-then-hang":
    frame(50)
    time.sleep(60)
elif mode == "fail":
    sys.stderr.write("rtsp://admin:hunter2@10.0.0.9/stream: Connection refused\n")
    sys.exit(1)
"""


@pytest.fixture
def fake_ffmpeg(tmp_path):
    script = tmp_path / "fake_ffmpeg.py"
    script.write_text(FAKE_FFMPEG, encoding="utf-8")

    def source(mode: str, **settings) -> FfmpegSource:
        return FfmpegSource("rtsp://camera/stream", ffmpeg=[sys.executable, str(script), mode], **settings)

    return source


def _png(image: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    Image.fromarray(image).save(buffer, "PNG")
    return buffer.getvalue()


def test_a_capture_becomes_two_stacks(fake_ffmpeg) -> None:
    burst = fake_ffmpeg("frames", frames=8, seconds=8.0).grab()

    assert burst.frames == 8
    assert burst.seconds_apart == 4.0
    assert [image.shape for image in burst.images] == [(48, 64), (48, 64)]
    assert burst.images[0].dtype == np.float32
    # Frames 0, 10, 20, 30 and 40, 50, 60, 70 of 255.
    assert burst.images[0][0, 0] == pytest.approx(15 / 255)
    assert burst.images[1][0, 0] == pytest.approx(55 / 255)
    assert burst.captured_at.tzinfo is not None


def test_a_repeated_picture_is_not_a_second_look(fake_ffmpeg) -> None:
    # An all-sky camera exposing for many seconds: the stream repeats one picture, then the next.
    slow = fake_ffmpeg("slow-camera").grab()
    frozen = fake_ffmpeg("frozen").grab()

    assert (slow.frames, len(slow.images), slow.seconds_apart) == (2, 1, 0.0)
    assert slow.images[0][0, 0] == pytest.approx(90 / 255)
    # The same newest picture again, however often it was repeated: nothing new.
    assert frozen.content_id == slow.content_id


def test_the_ffmpeg_command_reads_rtsp_over_tcp_and_writes_grey_frames() -> None:
    source = FfmpegSource("rtsp://camera/stream", ffmpeg=["ffmpeg"], frames=16, seconds=8.0)
    custom = FfmpegSource("desktop", ffmpeg=["ffmpeg"], input_options=["-f", "gdigrab"], max_width=0, seek=12.5)

    command = source.command()

    assert command[command.index("-i") - 2 : command.index("-i") + 2] == [
        "-rtsp_transport",
        "tcp",
        "-i",
        "rtsp://camera/stream",
    ]
    assert command[command.index("-vf") + 1] == "fps=2,scale='min(iw,1920)':-2:flags=area"
    assert command[-9:] == ["-frames:v", "16", "-pix_fmt", "gray", "-c:v", "pgm", "-f", "image2pipe", "pipe:1"]
    assert "-rtsp_transport" not in custom.command()
    assert custom.command()[custom.command().index("-ss") + 1] == "12.500"
    assert custom.command()[custom.command().index("-vf") + 1] == "fps=2"


def test_a_stream_that_delivers_nothing_is_cut_off(fake_ffmpeg) -> None:
    started = time.monotonic()

    with pytest.raises(SourceError) as failure:
        fake_ffmpeg("hang", timeout=1.0).grab()

    assert failure.value.code == "TIMEOUT"
    assert time.monotonic() - started < 15


def test_a_capture_in_flight_can_be_cancelled(fake_ffmpeg) -> None:
    source = fake_ffmpeg("hang", timeout=45.0)
    outcome: list[SourceError] = []

    def capture() -> None:
        try:
            source.grab()
        except SourceError as error:
            outcome.append(error)

    worker = threading.Thread(target=capture)
    worker.start()
    time.sleep(1.0)
    started = time.monotonic()
    source.cancel()
    worker.join(timeout=20)

    assert not worker.is_alive()
    assert time.monotonic() - started < 10
    assert [error.code for error in outcome] == ["CANCELLED"]
    with pytest.raises(SourceError):
        source.grab()


def test_frames_that_arrived_before_the_cut_off_are_used(fake_ffmpeg) -> None:
    burst = fake_ffmpeg("one-then-hang", timeout=1.5).grab()

    assert burst.frames == 1
    assert len(burst.images) == 1
    assert burst.images[0][0, 0] == pytest.approx(50 / 255)


def test_a_failed_stream_reports_why_without_the_password(fake_ffmpeg) -> None:
    with pytest.raises(SourceError) as failure:
        fake_ffmpeg("fail").grab()

    assert failure.value.code == "NO_FRAMES"
    assert "Connection refused" in failure.value.detail
    assert "hunter2" not in failure.value.detail
    assert "hunter2" not in str(failure.value)


def test_a_missing_ffmpeg_is_a_source_error(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("SKY_MONITOR_FFMPEG", raising=False)

    with pytest.raises(SourceError) as configured:
        find_ffmpeg(str(tmp_path / "no-ffmpeg-here"))
    with pytest.raises(SourceError) as spawned:
        FfmpegSource("x", ffmpeg=[str(tmp_path / "no-ffmpeg-here")]).grab()

    assert configured.value.code == spawned.value.code == "NO_FFMPEG"


def test_passwords_are_removed_from_text() -> None:
    assert redact("rtsp://admin:hunter2@10.0.0.9:554/a?x=1") == "rtsp://admin:***@10.0.0.9:554/a?x=1"
    assert redact("http://host/snap?user=a&password=hunter2&ch=1") == "http://host/snap?user=a&password=***&ch=1"
    assert redact("nothing secret here") == "nothing secret here"


def test_pgm_frames_are_read_up_to_the_first_incomplete_one() -> None:
    first = b"P5\n3 2\n255\n" + bytes(range(6))
    second = b"P5\n3 2\n255\n" + bytes(range(10, 16))

    frames = _parse_pgm(first + second + b"P5\n3 2\n255\n\x01\x02")

    assert [frame.tolist() for frame in frames] == [[[0, 1, 2], [3, 4, 5]], [[10, 11, 12], [13, 14, 15]]]
    assert _parse_pgm(b"not a picture") == []


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_stars_survive_a_real_video_codec(tmp_path) -> None:
    scene = SkyScene(shape=(240, 320), stars=150, noise=0.006)
    frames = [
        (scene.frame(shift=(0.02 * index, 0.0), frame_seed=index) * 255).round().astype(np.uint8) for index in range(10)
    ]
    video = tmp_path / "sky.mp4"
    encode = [
        find_ffmpeg(), "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s", "320x240",
        "-r", "5", "-i", "pipe:0", "-c:v", "mpeg4", "-q:v", "2", str(video),
    ]  # fmt: skip
    subprocess.run(encode, input=b"".join(frame.tobytes() for frame in frames), check=True, timeout=120)

    burst = FfmpegSource(str(video), ffmpeg=[find_ffmpeg()], frames=8, seconds=1.6, max_width=0).grab()

    assert burst.frames == 8
    assert [image.shape for image in burst.images] == [(240, 320), (240, 320)]
    early, late = (detect_stars(image).xy for image in burst.images)
    again = late[_near(late, early, 2.5)]
    truth = scene.star_positions()
    assert len(again) > 0.6 * len(truth)
    assert _near(again, truth, 2.5).mean() > 0.95


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_a_codecs_artifacts_on_overcast_are_not_stars(tmp_path) -> None:
    # A glowing, textured, drifting overcast with sensor noise, squeezed hard by a blocky codec.
    scene = SkyScene(noise=0.0)
    generator = np.random.default_rng(3)
    height, width = scene.shape
    texture = ndimage.gaussian_filter(generator.normal(size=(height, width + 200)).astype(np.float32), 25)
    texture *= 0.05 / texture.std()
    overcast = scene.frame(cloud=cloud_bank(scene.shape, 1.0), cloud_glow=0.2)
    analyzer = CameraAnalyzer(replace(AnalysisParams(), require_mature=False), interval_seconds=20)

    for look in range(2):
        video = tmp_path / f"overcast-{look}.mp4"
        encode = [
            find_ffmpeg(), "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{width}x{height}",
            "-r", "10", "-i", "pipe:0", "-c:v", "mpeg4", "-q:v", "31", "-g", "20", str(video),
        ]  # fmt: skip
        frames = []
        for index in range(40):
            start = 60 * look + index // 2
            frame = overcast + texture[:, start : start + width] + generator.normal(0.0, 0.02, scene.shape)
            frames.append((np.clip(frame, 0.0, 1.0) * 255).round().astype(np.uint8).tobytes())
        subprocess.run(encode, input=b"".join(frames), check=True, timeout=120)
        burst = FfmpegSource(str(video), ffmpeg=[find_ffmpeg()], frames=16, seconds=4.0, max_width=0).grab()
        result = analyzer.assess(replace(burst, captured_at=NIGHT + timedelta(seconds=20 * look)))

    assert len(burst.images) == 2
    assert result.star_count <= 2
    assert (result.verdict, result.reason) == (Verdict.CLOUDY, "FEW_STARS")


class _Pictures(BaseHTTPRequestHandler):
    picture = b""

    def do_GET(self) -> None:
        if self.path == "/missing.jpg":
            self.send_error(404)
            return
        if self.path == "/private.png":
            expected = "Basic " + base64.b64encode(b"viewer:hunter2").decode("ascii")
            if self.headers.get("Authorization") != expected:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="camera"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        kind = "multipart/x-mixed-replace; boundary=frame" if self.path == "/stream" else "image/png"
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(self.picture)))
        self.end_headers()
        self.wfile.write(self.picture)

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def picture_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Pictures)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    gradient = np.tile(np.arange(64, dtype=np.uint8) * 4, (48, 1))
    _Pictures.picture = _png(gradient)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_a_snapshot_is_fetched_and_decoded(picture_server) -> None:
    burst = SnapshotSource(f"{picture_server}/sky.png", timeout=10).grab()
    same = SnapshotSource(f"{picture_server}/sky.png", timeout=10).grab()

    assert burst.frames == 1 and burst.seconds_apart == 0.0
    assert burst.images[0].shape == (48, 64)
    assert burst.images[0][0, 10] == pytest.approx(40 / 255)
    assert burst.content_id == same.content_id


def test_a_snapshot_behind_a_password(picture_server) -> None:
    address = picture_server.replace("http://", "http://viewer:hunter2@")

    by_address = SnapshotSource(f"{address}/private.png", timeout=10).grab()
    by_setting = SnapshotSource(
        f"{picture_server}/private.png", username="viewer", password="hunter2", timeout=10
    ).grab()

    assert by_address.content_id == by_setting.content_id
    with pytest.raises(SourceError) as refused:
        SnapshotSource(f"{picture_server}/private.png", timeout=10).grab()
    assert refused.value.code == "HTTP_401"


def test_snapshot_failures_have_codes(picture_server) -> None:
    with pytest.raises(SourceError) as missing:
        SnapshotSource(f"{picture_server}/missing.jpg", timeout=10).grab()
    with pytest.raises(SourceError) as stream:
        SnapshotSource(f"{picture_server}/stream", timeout=10).grab()
    with pytest.raises(SourceError) as local_file:
        SnapshotSource("file:///etc/hosts")

    assert missing.value.code == "HTTP_404"
    assert stream.value.code == "NOT_A_SNAPSHOT"
    assert local_file.value.code == "BAD_URL"


def test_an_unreachable_snapshot_fails_within_its_time_limit() -> None:
    # A port nothing listens on.
    probe = ThreadingHTTPServer(("127.0.0.1", 0), _Pictures)
    port = probe.server_address[1]
    probe.server_close()
    started = time.monotonic()

    with pytest.raises(SourceError) as failure:
        SnapshotSource(f"http://127.0.0.1:{port}/sky.png", timeout=3).grab()

    assert failure.value.code in ("UNREACHABLE", "TIMEOUT")
    assert time.monotonic() - started < 15


def test_a_file_and_the_newest_file_of_a_directory(tmp_path) -> None:
    dark = np.full((48, 64), 20, dtype=np.uint8)
    light = np.full((48, 64), 200, dtype=np.uint8)
    (tmp_path / "a.png").write_bytes(_png(dark))
    time.sleep(0.05)
    (tmp_path / "b.png").write_bytes(_png(light))
    (tmp_path / "notes.txt").write_text("not a picture", encoding="utf-8")

    single = FileSource(tmp_path / "a.png").grab()
    newest = FileSource(tmp_path).grab()

    assert single.images[0][0, 0] == pytest.approx(20 / 255)
    assert newest.images[0][0, 0] == pytest.approx(200 / 255)


def test_file_failures_have_codes(tmp_path) -> None:
    (tmp_path / "broken.jpg").write_bytes(b"\xff\xd8\xff\xe0 not really a jpeg")
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(SourceError) as broken:
        FileSource(tmp_path / "broken.jpg").grab()
    with pytest.raises(SourceError) as missing:
        FileSource(tmp_path / "absent.jpg").grab()
    with pytest.raises(SourceError) as nothing:
        FileSource(empty).grab()

    assert (broken.value.code, missing.value.code, nothing.value.code) == ("DECODE", "UNREADABLE", "NO_FILES")


def test_pictures_are_decoded_to_luminance() -> None:
    deep = np.full((40, 60), 32768, dtype=np.uint16)
    colour = np.zeros((40, 60, 3), dtype=np.uint8)
    colour[..., 1] = 255
    wide = np.tile(np.arange(400, dtype=np.uint8) % 200, (40, 1))

    assert decode_image(_png(deep))[0, 0] == pytest.approx(32768 / 65535)
    assert decode_image(_png(colour))[0, 0] == pytest.approx(0.587, abs=0.01)
    assert decode_image(_png(wide), max_width=200).shape == (20, 200)
    with pytest.raises(SourceError):
        decode_image(_png(np.zeros((8, 8), dtype=np.uint8)))


def test_a_picture_that_stops_changing_goes_stale() -> None:
    freshness = Freshness(stale_after_seconds=300)
    image = np.zeros((48, 64), dtype=np.float32)

    def burst(content: str) -> FrameBurst:
        return FrameBurst((image,), 0.0, 1, content, NIGHT)

    freshness.check(burst("a"), 0.0)
    freshness.check(burst("a"), 290.0)
    with pytest.raises(SourceError) as stale:
        freshness.check(burst("a"), 301.0)
    freshness.check(burst("b"), 302.0)
    freshness.check(burst("b"), 600.0)

    assert stale.value.code == "STALE"
