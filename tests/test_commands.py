"""Simple typed or spoken commands are matched without the model; everything else must fall through to it."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assistant import commands  # noqa: E402
from assistant.commands import parse, to_number  # noqa: E402


@pytest.mark.parametrize("text,expected", [
    ("next song", {"action": "next"}), ("play next song", {"action": "next"}), ("Play the next one, please.", {"action": "next"}),
    ("skip", {"action": "next"}), ("skip this song", {"action": "next"}), ("Jarvis, next track", {"action": "next"}),
    ("play previous song", {"action": "prev"}), ("previous", {"action": "prev"}), ("go back", {"action": "prev"}),
    ("play the last song", {"action": "prev"}), ("play prev song", {"action": "prev"}),
    ("pause", {"action": "pause"}), ("stop the music", {"action": "pause"}), ("resume", {"action": "resume"}),
    ("play", {"action": "resume"}), ("continue playing", {"action": "resume"}),
    ("mute", {"action": "mute"}), ("unmute", {"action": "unmute"}),
    ("play it again", {"action": "restart"}),
])
def test_media_commands(text, expected):
    assert parse(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("vol to 40%", ("volume_set", 40)), ("volume 60", ("volume_set", 60)), ("set the volume to sixty percent", ("volume_set", 60)),
    ("vol 60%", ("volume_set", 60)), ("volume at fifty five", ("volume_set", 55)), ("volume to max", ("volume_set", 100)),
    ("volume to half", ("volume_set", 50)), ("set volume to 0", ("volume_set", 0)), ("60 percent volume", ("volume_set", 60)),
    ("vol up", ("volume_up", 10)), ("volume down", ("volume_down", 10)), ("turn it up", ("volume_up", 10)),
    ("reduce the vol a bit", ("volume_down", 5)), ("increase the volume a lot", ("volume_up", 25)),
    ("volume up by 20", ("volume_up", 20)), ("lower the volume", ("volume_down", 10)), ("louder", ("volume_up", 10)),
    ("turn the volume down by fifteen", ("volume_down", 15)), ("make it quieter", ("volume_down", 10)),
])
def test_volume_commands(text, expected):
    cmd = parse(text)
    assert (cmd["action"], cmd["value"]) == expected


def test_play_a_song():
    assert parse("play mere rashke kamar on utube music") == {"action": "play", "query": "mere rashke kamar", "service": "youtube_music"}
    assert parse("play Mere Rashke Qamar") == {"action": "play", "query": "mere rashke qamar", "service": "youtube_music"}
    assert parse("Play Believer on YouTube.") == {"action": "play", "query": "believer", "service": "youtube"}
    assert parse("play the song kesariya")["query"] == "kesariya"


def test_song_titles_that_look_like_commands_are_songs():
    assert parse("play next to me by imagine dragons")["action"] == "play"
    assert parse("play sound of rain")["query"] == "sound of rain"
    assert parse("play volume 4 by sugarland")["action"] == "play"
    assert parse("play last christmas")["query"] == "last christmas"


def test_open_app():
    assert parse("open outlook") == {"action": "open_app", "name": "outlook"}
    assert parse("launch spotify") == {"action": "open_app", "name": "spotify"}


@pytest.mark.parametrize("text", [
    "", "what is the capital of france", "write a python script that sorts a list", "open the file notes.txt",
    "what's the volume of a sphere with radius 3", "tell me a joke", "send an email to ravi", "next week's weather in delhi",
])
def test_everything_else_goes_to_the_model(text):
    assert parse(text) is None


def test_numbers():
    assert to_number("sixty five") == 65 and to_number("a hundred") == 100 and to_number("fifteen") == 15
    assert to_number("twenty-one") == 21 and to_number("banana") is None


def test_run_calls_media_and_desktop(monkeypatch):
    calls = []
    monkeypatch.setattr(commands.media, "do", lambda action, value=None: calls.append((action, value)) or "ok")
    assert commands.run({"action": "volume_set", "value": 40}) == "ok" and calls == [("volume_set", 40)]
    from assistant import desktop
    monkeypatch.setattr(desktop, "play_music", lambda q, s: f"playing {q} on {s}")
    assert commands.run({"action": "play", "query": "x", "service": "youtube"}) == "playing x on youtube"
