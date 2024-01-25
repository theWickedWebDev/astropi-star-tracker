from typing import Any
from quart import current_app, request

from astropy.coordinates import ICRS, SkyCoord
from astropy.time import Time
import trio

from .. import telescope_control as tc
from ..activity import ActivityStatus
from ._blueprint import api
from .response import returnResponse

# TODO: Update the API to get things from telescope.config (and rebuild the
# config, stop the telescope, restart the telescope as needed).

KEY_TELESCOPE = "telescope"


def get_telescope() -> tc.TelescopeControl:
    telescope = current_app.config[KEY_TELESCOPE]
    assert isinstance(telescope, tc.TelescopeControl)
    return telescope


async def _final_status(chan: tc.StatusChannel) -> tuple[ActivityStatus, Any] | tuple[None, None]:
    status = None, None
    async for status in chan:
        pass
    return status

async def _wait_slew_complete(chan: tc.StatusChannel) -> tuple[ActivityStatus, Any] | tuple[None, None]:
    status = None, None
    async for status in chan:
        if status[1] == "slew_complete":
            break
    return status


@api.route("/calibrate/", methods=["POST"])
async def calibrate():
    try:
        _ra = request.args.get("ra")
        _dec = request.args.get("dec")

        status, _ = await _final_status(
            get_telescope().calibrate(
                tc.FixedTarget(SkyCoord(ra=_ra, dec=_dec, frame=ICRS))
            )
        )

        # ActivityStatus.COMPLETE = good, ActivityStatus.ABORTED = maybe bad,
        # None = something went wrong (the command never produced any status)
        print(status)

        return await returnResponse({"calibrated": True, "ra": _ra, "dec": _dec}, 200)

    except:
        return await returnResponse({"calibrated": False, "ra": _ra, "dec": _dec}, 400)


@api.route("/calibrate/by_name/", methods=["POST"])
async def calibrate_by_name():
    try:
        _name = request.args.get("name")
        status, _ = await _final_status(
            get_telescope().calibrate(tc.FixedTarget(SkyCoord.from_name(_name)))
        )

        # ActivityStatus.COMPLETE = good, ActivityStatus.ABORTED = maybe bad,
        # None = something went wrong (the command never produced any status)
        print(status)

        return await returnResponse({"calibrated": True, "name": _name}, 200)
    except:
        return await returnResponse({"calibrated": False, "name": _name}, 400)


@api.route("/calibrate/bump/", methods=["POST"])
async def calibrate_bump():
    try:
        bearing = int(request.args.get("bearing", "0"))
        dec = int(request.args.get("dec", "0"))
        sync = _bool_type(True)(request.args.get("sync", "true"))

        telescope = get_telescope()
        chan = telescope.calibrate_rel_steps(bearing, dec)
        target = telescope.target
        if sync and target is not None:
            # NOTE: telescope.track also returns a channel of statuses, but it
            # is more difficult to use it, since the track operation could last
            # forever.
            telescope.track(target)

        # NOTE: It's important to wait for the final status _after_ issuing the
        # track command, so we don't cause an unwanted delay.
        status, _ = await _final_status(chan)
        # ActivityStatus.COMPLETE = good, ActivityStatus.ABORTED = maybe bad,
        # None = something went wrong (the command never produced any status)
        print(status)

        return await returnResponse(
            {"calibrated": True, "bearing": bearing, "dec": dec}, 200
        )
    except:
        return await returnResponse({"calibrated": False}, 400)


@api.route("/calibrate/solar_system_object/", methods=["POST"])
async def calibrate_solar_system_object():
    try:
        _name = request.args.get("name")

        status, _ = await _final_status(
            get_telescope().calibrate(tc.SolarSystemTarget(_name))
        )
        # ActivityStatus.COMPLETE = good, ActivityStatus.ABORTED = maybe bad,
        # None = something went wrong (the command never produced any status)
        print(status)

        return await returnResponse(
            {
                "calibrated": True,
                "object": _name,
            },
            200,
        )
    except:
        return await returnResponse(
            {
                "calibrated": False,
                "object": _name,
            },
            400,
        )


@api.route("/goto/", methods=["POST"])
async def goto():
    try:
        _ra = request.args.get("ra")
        _dec = request.args.get("dec")

        status, extra = await _wait_slew_complete(
            get_telescope().track(tc.FixedTarget(SkyCoord(ra=_ra, dec=_dec, frame=ICRS)))
        )
        # FIXME: Inspect status, extra to make sure they are success values.
        _ = status, extra

        return await returnResponse({"goto": True, "ra": _ra, "dec": _dec}, 200)

    except:
        return await returnResponse({"goto": False, "ra": _ra, "dec": _dec}, 400)


@api.route("/goto/by_name/", methods=["POST"])
async def goto_by_name():
    try:
        _name = request.args.get("name")

        status, extra = await _wait_slew_complete(
            get_telescope().track(tc.FixedTarget(SkyCoord.from_name(_name)))
        )
        # FIXME: Inspect status, extra to make sure they are success values.
        _ = status, extra

        return await returnResponse({"goto": True, "name": _name}, 200)
    except:
        return await returnResponse({"goto": False, "name": _name}, 400)


@api.route("/goto/mpc/", methods=["POST"])
async def goto_by_mpc_query():
    try:
        name = request.args.get("name")
        assert name is not None
        status, extra = await _wait_slew_complete(
            get_telescope().track(tc.MPCQueryTarget(name))
        )
        # FIXME: Inspect status, extra to make sure they are success values.
        _ = status, extra

        return await returnResponse({"goto": True, "name": name}, 200)
    except:
        return await returnResponse({"goto": False}, 400)


@api.route("/goto/solar_system_object/", methods=["POST"])
async def goto_solar_system_object():
    try:
        _name = request.args.get("name")
        assert _name is not None
        status, extra = await _wait_slew_complete(
            get_telescope().track(tc.SolarSystemTarget(_name))
        )
        # FIXME: Inspect status, extra to make sure they are success values.
        _ = status, extra

        return await returnResponse(
            {
                "goto": True,
                "object": _name,
            },
            200,
        )
    except:
        return await returnResponse(
            {
                "goto": False,
                "object": _name,
            },
            400,
        )


def _bool_type(bare_value: bool):
    def parse(s: str) -> bool:
        match s:
            case "":
                return bare_value
            case "true":
                return True
            case "false":
                return False
            case _:
                raise ValueError(f"{s!r} is not a valid boolean value")

    return parse
