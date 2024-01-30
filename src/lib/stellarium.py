from __future__ import annotations
import logging
import math
import struct

from astropy.coordinates import ICRS, SkyCoord
import astropy.units as u
import trio

from .. import telescope_control as tc

_log = logging.getLogger(__name__)

def decode_ra(x: int):
    """Convert Stellarium wire RA value to fractional seconds"""
    return (x % 0xFFFFFFFF) / 0xFFFFFFFF * 86_400


def decode_dec(x: int):
    """Convert Stellarium wire DEC value to radians"""
    return x / 0x40000000 * math.pi / 2


def encode_ra(x: float):
    """Convert RA (fractional seconds) to Stellarium wire format"""
    return round((float(x) % 86_400) / 86_400 * 0xFFFFFFFF)


def encode_dec(x: float):
    """Convert DEC (radians) to Stellarium wire format"""
    return int(x * 2 / math.pi * 0x40000000)


async def serve(host: str, port: int, telescope: tc.TelescopeControl):
    async def handler(stream: trio.SocketStream):
        _log.info("Off world DHD and MALP Connected")
        try:
            async with trio.open_nursery() as n:
                n.start_soon(_report_position_loop, stream, telescope)
                n.start_soon(_receive_target_loop, stream, telescope)
        except (trio.BrokenResourceError, EndOfStream):
            _log.info("Off world DHD and MALP disconnected")
        except Exception as e:
            _log.error("Disconnecting off world DHD and MALP", exc_info=e)

    await trio.serve_tcp(handler, port=port, host=host)


async def _report_position_loop(
    stream: trio.SocketStream, telescope: tc.TelescopeControl
):
    while True:
        pos = telescope.current_skycoord()
        await _report_position(stream, pos)
        await trio.sleep(0.5)


async def _report_position(stream: trio.SocketStream, pos: SkyCoord):
    # FIXME: Send a real time value
    t_raw = 0

    ra: float = pos.ra.to(u.hourangle) / (24 * u.hourangle) * 86_400  # pyright: ignore
    dec: float = pos.dec.to(u.rad).value  # pyright: ignore

    await stream.send_all(
        struct.pack(
            "<hhQLll",
            24,
            0,
            t_raw,
            encode_ra(ra),
            encode_dec(dec),
            0,
        )
    )


class EndOfStream(Exception):
    pass


async def receive_exactly(stream: trio.SocketStream, count: int):
    data = b""
    while len(data) < count:
        with trio.move_on_after(1):
            more = await stream.receive_some(count - len(data))
            if not more:
                raise EndOfStream
            data += more
    return data


async def _receive_target_loop(
    stream: trio.SocketStream, telescope: tc.TelescopeControl
):
    while True:
        if not await _read_target(stream, telescope):
            await trio.sleep(0.1)


async def _read_target(stream: trio.SocketStream, telescope: tc.TelescopeControl):
    # Read msg length and type
    header = await receive_exactly(stream, 4)
    if not header:
        return False

    msglen, msgtype = struct.unpack("<hh", header)

    # The protocol only defines one message type: 0
    assert msgtype == 0
    # Read the rest of the message (don't include the header count)
    body = await receive_exactly(stream, msglen - 4)

    # Unpack raw data
    _, ra_raw, dec_raw = struct.unpack("<QLl", body)
    # TODO: Use the time given by Stellarium for something?

    coord = SkyCoord(
        (decode_ra(ra_raw) / 3600) * u.hourangle,  # pyright: ignore
        decode_dec(dec_raw) * u.rad,
        frame=ICRS,
    )

    _log.info(f"Manually dialing Stargate Coordinates from off world DHD: {coord}")
    telescope.track(tc.FixedTarget(coord))

    return True
