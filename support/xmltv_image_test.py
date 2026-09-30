#!/usr/bin/env python3
#
# Copyright (C) 2026 Tvheadend Project (https://tvheadend.org)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.
"""Integration test for XMLTV programme images (Python 3 standard library only).

Run from the source tree after building:
  python3 support/xmltv_image_test.py --binary build.linux/tvheadend

Starts its own tuner-free Tvheadend, XMLTV grabber and image server, all on
loopback. Never connects to an existing server. Uses a temporary configuration
and stops the child and removes its configuration even if an assertion fails.
The fixture dates are expanded to tomorrow so no recording actually starts.
"""

import argparse
import base64
import contextlib
import datetime
import http.server
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from string import Template


IMAGE_FIELDS = ("imagePoster", "imageBackdrop", "imageStill")
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
    "/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")


def wait_for(check, description, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.1)
    raise AssertionError("Timed out waiting for " + description)


def encode_message(message):
    fields = []
    for name, value in message.items():
        name = name.encode("utf-8")
        if isinstance(value, int):
            kind = 2
            data = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "little")
        else:
            kind = 3
            data = value.encode("utf-8")
        fields.append(struct.pack(">BBI", kind, len(name), len(data)) + name + data)
    return b"".join(fields)


def decode_message(data, is_list=False):
    result = [] if is_list else {}
    offset = 0
    while offset < len(data):
        kind, name_len, data_len = struct.unpack_from(">BBI", data, offset)
        offset += 6
        name = data[offset:offset + name_len].decode("utf-8")
        offset += name_len
        value = data[offset:offset + data_len]
        offset += data_len
        if kind in (1, 5):
            value = decode_message(value, kind == 5)
        elif kind == 2:
            value = int.from_bytes(value, "little", signed=data_len == 8)
        elif kind == 3:
            value = value.decode("utf-8")
        elif kind == 7:
            value = bool(value and value[0])
        elif kind != 4:
            raise AssertionError("Unexpected HTSP field type: %d" % kind)
        if is_list:
            result.append(value)
        else:
            result[name] = value
    return result


class HTSPClient:
    """The small subset of HTSP needed for metadata regression tests."""

    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=15)
        self.messages = []
        self.seq = 0
        self.request("hello", htspversion=44, clientname="xmltv image test")
        self.request("enableAsyncMetadata", epg=1)
        self.receive_until(lambda m: m.get("method") == "initialSyncCompleted")

    def close(self):
        self.sock.close()

    def read_exact(self, length):
        data = bytearray()
        while len(data) < length:
            chunk = self.sock.recv(length - len(data))
            if not chunk:
                raise AssertionError("HTSP connection closed")
            data.extend(chunk)
        return data

    def receive(self):
        length, = struct.unpack(">I", self.read_exact(4))
        message = decode_message(self.read_exact(length))
        self.messages.append(message)
        return message

    def receive_until(self, check):
        for message in self.messages:
            if check(message):
                return message
        while True:
            message = self.receive()
            if check(message):
                return message

    def request(self, method, **args):
        self.seq += 1
        data = encode_message(dict(method=method, seq=self.seq, **args))
        self.sock.sendall(struct.pack(">I", len(data)) + data)
        reply = self.receive_until(lambda m: m.get("seq") == self.seq)
        assert "error" not in reply, reply
        return reply

    def event(self, method, event_id):
        return self.receive_until(lambda m: m.get("method") == method and
                                  m.get("eventId") == event_id)


class ImageHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(PNG)))
        self.end_headers()
        try:
            self.wfile.write(PNG)
        except (BrokenPipeError, ConnectionResetError):
            pass  # The image cache may cancel a request during shutdown.

    def log_message(self, *args):
        pass


class Instance:
    def __init__(self, binary, root, http_port, htsp_port):
        self.binary = binary
        self.root = root
        self.config = root / "config"
        self.http_port = http_port
        self.htsp_port = htsp_port
        self.base = "http://127.0.0.1:%d" % http_port
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.process = None
        self.log = None

    def api(self, path, **args):
        args = {key: json.dumps(value) if isinstance(value, (dict, list)) else value
                for key, value in args.items()}
        data = urllib.parse.urlencode(args).encode("utf-8")
        with self.opener.open(self.base + "/api/" + path, data, timeout=5) as reply:
            return json.load(reply)

    def start(self):
        # Only our own preflighted ports, with device discovery disabled.
        env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ["PATH"])
        self.log = (self.root / "tvheadend.log").open("ab")
        self.process = subprocess.Popen([
            str(self.binary), "-c", str(self.config), "-b", "127.0.0.1",
            "--http_port", str(self.http_port), "--htsp_port", str(self.htsp_port),
            "--noacl", "--nosyslog", "--nobackup", "-a", "-1",
            "--nosatipcli", "--satip_rtsp", "-1",
        ], stdout=self.log, stderr=self.log, env=env)

        def ready():
            if self.process.poll() is not None:
                raise AssertionError("Tvheadend exited at startup")
            try:
                return self.api("serverinfo")
            except (OSError, ValueError):
                return False
        wait_for(ready, "Tvheadend startup")

    def stop(self):
        if self.process is not None:
            self.process.terminate()
            try:
                code = self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
                raise AssertionError("Tvheadend failed to stop cleanly")
            finally:
                self.process = None
                self.log.close()
            assert code == 0, "Tvheadend exit status: %d" % code

    def save(self, uuid, **changes):
        self.api("idnode/save", node=dict(uuid=uuid, **changes))

    def events(self):
        return {e["title"]: e for e in
                self.api("epg/events/grid", limit=1000)["entries"]}

    def recordings(self):
        return {e["disp_title"]: e for e in
                self.api("dvr/entry/grid", limit=1000)["entries"]}

    def feed(self, xml):
        path = self.config / "epggrab/xmltv.sock"
        wait_for(path.exists, "XMLTV socket")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(str(path))
            sock.sendall(xml.encode("utf-8"))
            sock.shutdown(socket.SHUT_WR)

    def image_url(self, ref):
        if ref.startswith(("http://", "file://")):
            return ref
        assert "imagecache/" in ref, ref
        meta = self.config / "imagecache/meta" / ref.rsplit("/", 1)[-1]

        def saved_url():
            try:
                return json.loads(meta.read_text())["url"]
            except (FileNotFoundError, ValueError):
                return None
        # Image IDs are exposed before the cache thread saves their metadata.
        return wait_for(saved_url, "image metadata " + ref)

    def check_images(self, entry, expected):
        for field in ("image",) + IMAGE_FIELDS:
            if field in expected:
                assert field in entry, (field, entry)
                assert self.image_url(entry[field]) == expected[field], (field, entry)
            else:
                # The legacy DVR image property is an empty string, not absent.
                assert not entry.get(field), (field, entry)
                if field in IMAGE_FIELDS:
                    assert field not in entry, (field, entry)


def test(instance, image_base, file_image):
    root = instance.root
    fixture = Path(__file__).parent / "testdata/xmltv/images.xml"
    values = dict(base=image_base, file_image=file_image)
    tomorrow = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
    for i in range(7):
        start = tomorrow + datetime.timedelta(hours=i)
        values["start%d" % i] = start.strftime("%Y%m%d%H%M%S +0000")
        values["stop%d" % i] = (start + datetime.timedelta(minutes=30)).strftime(
            "%Y%m%d%H%M%S +0000")
    xml = Template(fixture.read_text()).substitute(values)
    (root / "fixture.xml").write_text(xml)

    # An internal module lets the same fixture exercise competing priorities.
    grabber = root / "tv_grab_image_test"
    grabber.write_text("#!/bin/sh\n"
                       "if [ \"$1\" = --description ]; then\n"
                       "  echo 'XMLTV typed image test'\n"
                       "else\n"
                       "  cat \"$(dirname \"$0\")/internal.xml\"\n"
                       "fi\n")
    grabber.chmod(0o700)
    (root / "internal.xml").write_text(xml)
    instance.start()
    # HTSP hides channels without services. A disabled, URL-free IPTV mux
    # supplies the mapping without tuners, discovery or stream connections.
    network = instance.api("mpegts/network/create", **{
        "class": "iptv_network",
        "conf": dict(networkname="Metadata only", enabled=False,
                     scan_create=False, skipinitscan=True, service_sid=1),
    })["uuid"]
    mux = instance.api("mpegts/network/mux_create", uuid=network,
                       conf=dict(enabled=0, iptv_muxname="Metadata only"))["uuid"]
    service = instance.api("mpegts/service/grid", hidemode="none")["entries"][0]["uuid"]
    parent = instance.api("channel/create", conf=dict(name="Image Test", enabled=True,
                                                     services=[service]))["uuid"]
    modules = instance.api("epggrab/module/list")["entries"]
    external = next(m["uuid"] for m in modules if m["title"] == "External: XMLTV")
    internal = next(m["uuid"] for m in modules if "XMLTV typed image test" in m["title"])
    props = instance.api("idnode/load", uuid=external)["entries"][0]["params"]
    assert next(p["value"] for p in props if p["id"] == "image_types") == []
    instance.save(external, enabled=True)
    instance.feed(xml)
    # Newly discovered grabber channels are linked after the first parse.
    wait_for(lambda: any(e["channels"] for e in
                         instance.api("epggrab/channel/grid", limit=100)["entries"]),
             "XMLTV channel mapping")
    instance.feed(xml)
    events = wait_for(lambda: instance.events() if len(instance.events()) == 7 else None,
                      "disabled import")
    for entry in events.values():
        assert all(field not in entry for field in IMAGE_FIELDS), entry
    assert events["Both"]["image"] == image_base + "/icon.png"
    assert events["Icon only"]["image"] == image_base + "/icon-only.png"
    print("PASS: default-off import, unchanged first-icon handling", flush=True)

    instance.api("imagecache/config/save", node=dict(enabled=True))
    instance.save(external, image_types=["poster", "backdrop", "still"])
    instance.feed(xml)
    wait_for(lambda: instance.events()["Both"].get("imageStill"), "enabled import")
    events = instance.events()
    expected = {
        "Both": dict(image=image_base + "/icon.png",
                     imagePoster=image_base + "/poster-first.png",
                     imageBackdrop=image_base + "/backdrop-landscape-first.png",
                     imageStill=image_base + "/still-landscape-first.png"),
        "Icon only": dict(image=image_base + "/icon-only.png"),
        "Typed only": dict(imagePoster=image_base + "/typed-poster.png",
                           imageBackdrop=image_base + "/typed-backdrop-first.png",
                           imageStill=file_image),
        "No images": {},
        "Ignored images": {},
        "Shared image": {field: image_base + "/shared.png"
                         for field in ("image",) + IMAGE_FIELDS},
        "Updated images": dict(image=image_base + "/update-icon.png",
                               imagePoster=image_base + "/update-poster.png",
                               imageBackdrop=image_base + "/update-backdrop.png",
                               imageStill=image_base + "/update-still.png"),
    }
    for title, images in expected.items():
        instance.check_images(events[title], images)
    assert len({events["Shared image"][field] for field in ("image",) + IMAGE_FIELDS}) == 1
    for ref in events["Both"].values():
        if isinstance(ref, str) and ref.startswith("imagecache/"):
            def fetched():
                try:
                    with instance.opener.open(instance.base + "/" + ref, timeout=5) as reply:
                        return reply.read() == PNG
                except OSError:
                    return False
            wait_for(fetched, "cached artwork " + ref)
    print("PASS: type/order/orientation selection, ignored tags, cache and URL dedup", flush=True)

    with contextlib.closing(HTSPClient(instance.htsp_port)) as htsp:
        for title, images in expected.items():
            instance.check_images(htsp.event("eventAdd", events[title]["eventId"]), images)
        print("PASS: HTSP eventAdd including absent fields", flush=True)

        for title in ("Both", "No images", "Updated images"):
            instance.api("dvr/entry/create_by_event", event_id=events[title]["eventId"],
                         config_uuid="")
            message = htsp.receive_until(lambda m: m.get("method") == "dvrEntryAdd" and
                                         m.get("title") == title)
            instance.check_images(message, expected[title])
            if "imageBackdrop" in expected[title]:
                assert message["fanartImage"] == message["imageBackdrop"], message
            else:
                assert "fanartImage" not in message, message
        recordings = instance.recordings()
        for title in recordings:
            instance.check_images(recordings[title], expected[title])
        # tvhmeta must still see an empty lookup slot, not the HTSP fallback.
        assert not recordings["Both"]["fanart_image"]
        lookup = instance.api("idnode/load", uuid=recordings["Both"]["uuid"],
                              list="uuid,image,fanart_image", grid=1)["entries"][0]
        assert lookup["image"] and not lookup["fanart_image"], lookup
        instance.save(recordings["Both"]["uuid"], fanart_image=image_base + "/lookup.png")
        message = htsp.receive_until(lambda m: m.get("method") == "dvrEntryUpdate" and
                                     m.get("title") == "Both" and
                                     instance.image_url(m["fanartImage"]) == image_base + "/lookup.png")
        instance.check_images(message, expected["Both"])
        assert instance.image_url(instance.recordings()["Both"]["fanart_image"]) == image_base + "/lookup.png"
        print("PASS: DVR grids/HTSP add/update, copied artwork and fanart precedence", flush=True)

        updated = ET.fromstring(xml)
        programme = next(p for p in updated.findall("programme")
                         if p.findtext("title") == "Updated images")
        for image in list(programme.findall("image")):
            if image.get("type") == "backdrop":
                programme.remove(image)
            elif image.get("type") == "poster":
                image.text = image_base + "/poster-changed.png"
        instance.feed(ET.tostring(updated, encoding="unicode"))
        event_id = events["Updated images"]["eventId"]
        changed = dict(expected["Updated images"], imagePoster=image_base + "/poster-changed.png")
        del changed["imageBackdrop"]
        message = htsp.event("eventUpdate", event_id)
        instance.check_images(message, changed)
        instance.check_images(instance.events()["Updated images"], changed)
        instance.check_images(instance.recordings()["Updated images"], expected["Updated images"])
        expected["Updated images"] = changed
        print("PASS: image-only EPG update/removal and unchanged DVR snapshot", flush=True)

        # A partial selection clears only deselected types on the next import.
        htsp.messages.clear()
        instance.save(external, image_types=["poster"])
        instance.feed(ET.tostring(updated, encoding="unicode"))
        message = htsp.event("eventUpdate", events["Both"]["eventId"])
        poster_only = {key: value for key, value in expected["Both"].items()
                       if key in ("image", "imagePoster")}
        instance.check_images(message, poster_only)
        instance.check_images(instance.events()["Both"], poster_only)
        instance.save(external, image_types=[])
        htsp.messages.clear()
        instance.feed(xml)
        instance.check_images(htsp.event("eventUpdate", events["Both"]["eventId"]),
                              dict(image=image_base + "/icon.png"))
        print("PASS: individual type selection and disabling removes EPG types", flush=True)

    # Restore all types before exercising priority and cloning.
    instance.save(external, image_types=["poster", "backdrop", "still"], priority=4)
    instance.feed(xml)
    wait_for(lambda: instance.events()["Both"].get("imageStill"), "restore images")
    expected["Updated images"] = dict(image=image_base + "/update-icon.png",
                                      imagePoster=image_base + "/update-poster.png",
                                      imageBackdrop=image_base + "/update-backdrop.png",
                                      imageStill=image_base + "/update-still.png")
    competing = xml.replace("poster-first.png", "priority-poster.png")
    (root / "internal.xml").write_text(competing)
    instance.save(internal, enabled=True, priority=1,
                  image_types=["poster", "backdrop", "still"])
    instance.api("epggrab/internal/rerun", rerun=1)
    # Log completion is needed here: a rejected grab produces no API change.
    marker = (str(grabber) + ": parse took").encode("utf-8")
    # Internal grabbers have a two-minute startup grace period.
    wait_for(lambda: marker in (root / "tvheadend.log").read_bytes(),
             "internal grab", timeout=150)
    grabbed_channel = next(c for c in instance.api("epggrab/channel/grid")["entries"]
                           if c["modid"] == str(grabber))
    instance.save(grabbed_channel["uuid"], channels=[parent])
    count = (root / "tvheadend.log").read_bytes().count(marker)
    instance.api("epggrab/internal/rerun", rerun=1)
    wait_for(lambda: (root / "tvheadend.log").read_bytes().count(marker) > count,
             "mapped lower-priority grab")
    instance.check_images(instance.events()["Both"], expected["Both"])
    instance.save(internal, priority=5)
    instance.api("epggrab/internal/rerun", rerun=1)
    wait_for(lambda: instance.image_url(instance.events()["Both"]["imagePoster"]) ==
             image_base + "/priority-poster.png", "higher-priority import")
    expected["Both"]["imagePoster"] = image_base + "/priority-poster.png"
    print("PASS: lower-priority rejection and higher-priority replacement", flush=True)

    clone = instance.api("channel/create", conf=dict(name="Image Clone", enabled=True,
                                                    epg_parent=parent,
                                                    services=[service]))["uuid"]
    cloned = wait_for(lambda: [e for e in instance.api("epg/events/grid", limit=1000)["entries"]
                               if e["channelName"] == "Image Clone"], "EPG clone")
    assert len(cloned) == 7, cloned
    for entry in cloned:
        instance.check_images(entry, expected[entry["title"]])
    print("PASS: EPG parent/child cloning", flush=True)

    instance.save(internal, enabled=False)

    def channels_saved():
        try:
            return all(json.loads((instance.config / "channel/config" / uuid).read_text())
                       ["services"] == [service] for uuid in (parent, clone))
        except (OSError, ValueError):
            return False
    # Pending channel saves during teardown can see already-unlinked services.
    wait_for(channels_saved, "channel service mappings saved")
    # The auto-created service and its channel mappings are stored in the mux.
    # Save it after both channels exist so HTSP can see them after restart.
    mux_config = instance.config / "input/iptv/networks" / network / "muxes" / mux
    mtime = mux_config.stat().st_mtime_ns
    instance.save(mux, iptv_muxname="Saved metadata only")
    wait_for(lambda: mux_config.stat().st_mtime_ns != mtime, "fixture mux saved")
    instance.stop()
    assert (instance.config / "epgdb.v3").exists(), "EPG database not saved"
    saved = [json.loads(p.read_text()) for p in (instance.config / "dvr/log").iterdir()]
    both = next(e for e in saved if "Both" in e.get("title", {}).values())
    assert both["imagePoster"] == image_base + "/poster-first.png", both
    assert both["fanart_image"] == image_base + "/lookup.png", both
    updated = next(e for e in saved if "Updated images" in e.get("title", {}).values())
    assert updated["imageBackdrop"] == image_base + "/update-backdrop.png", updated
    assert not updated.get("fanart_image"), updated
    instance.start()
    channels = instance.api("channel/grid")["entries"]
    assert all(c["services"] == [service] for c in channels), channels
    events = instance.api("epg/events/grid", limit=1000)["entries"]
    assert len(events) == 14, events
    for entry in events:
        instance.check_images(entry, expected[entry["title"]])
    recordings = instance.recordings()
    assert not recordings["Updated images"]["fanart_image"]
    for title in recordings:
        # The recording poster remains the original external-provider snapshot.
        images = dict(expected[title])
        if title == "Both":
            images["imagePoster"] = image_base + "/poster-first.png"
        instance.check_images(recordings[title], images)
    with contextlib.closing(HTSPClient(instance.htsp_port)) as htsp:
        for entry in events:
            instance.check_images(htsp.event("eventAdd", entry["eventId"]), expected[entry["title"]])
        for title in recordings:
            message = htsp.receive_until(lambda m: m.get("method") == "dvrEntryAdd" and
                                         m.get("title") == title)
            images = dict(expected[title])
            if title == "Both":
                images["imagePoster"] = image_base + "/poster-first.png"
                assert instance.image_url(message["fanartImage"]) == image_base + "/lookup.png"
            elif title == "Updated images":
                assert message["fanartImage"] == message["imageBackdrop"], message
            instance.check_images(message, images)
    instance.stop()
    print("PASS: EPG/DVR persistence, raw config URLs, HTSP after restart", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--binary", type=Path, default=Path("build.linux/tvheadend"))
    parser.add_argument("--http-port", type=int, default=19981)
    parser.add_argument("--htsp-port", type=int, default=19982)
    parser.add_argument("--tmpdir", type=Path, help="Parent for the temporary directory")
    args = parser.parse_args()
    binary = args.binary.resolve(strict=True)
    assert args.http_port != args.htsp_port
    # A collision fails before starting a child; never test an existing service.
    for port in (args.http_port, args.htsp_port):
        assert 1024 <= port <= 65535 and port not in (9981, 9982)
        with socket.socket() as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
    with tempfile.TemporaryDirectory(prefix="tvh-images-", dir=args.tmpdir) as directory:
        root = Path(directory)
        instance = Instance(binary, root, args.http_port, args.htsp_port)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), ImageHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        image = root / "still.png"
        image.write_bytes(PNG)
        try:
            test(instance, "http://127.0.0.1:%d" % server.server_port, image.as_uri())
        except Exception:
            print((root / "tvheadend.log").read_text(errors="replace")[-12000:])
            raise
        finally:
            instance.stop()
            server.shutdown()
            server.server_close()
            thread.join()
    print("PASS: child stopped and temporary configuration removed", flush=True)


if __name__ == "__main__":
    main()
