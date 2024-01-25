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


async def _final_status(
    chan: trio.MemoryReceiveChannel[ActivityStatus],
) -> ActivityStatus | None:
    status = None
    async for status in chan:
        pass
    return status


@api.route("/calibrate/", methods=["POST"])
async def calibrate():
    try:
        _ra = request.args.get("ra")
        _dec = request.args.get("dec")

        status = await _final_status(
            get_telescope().calibrate(
                tc.FixedTarget(SkyCoord(ra=_ra, dec=_dec, frame=ICRS))
            )
        )

        # ActivityStatus.COMPLETE = good, ActivityStatus.ABORTED = maybe bad
        print(status)

        return await returnResponse({"calibrated": True, "ra": _ra, "dec": _dec}, 200)

    except:
        return await returnResponse({"calibrated": False, "ra": _ra, "dec": _dec}, 400)


@api.route("/calibrate/by_name/", methods=["POST"])
async def calibrate_by_name():
    try:
        _name = request.args.get("name")
        status = await _final_status(
            get_telescope().calibrate(tc.FixedTarget(SkyCoord.from_name(_name)))
        )

        # ActivityStatus.COMPLETE = good, ActivityStatus.ABORTED = maybe bad
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
        status = await _final_status(chan)
        # ActivityStatus.COMPLETE = good, ActivityStatus.ABORTED = maybe bad
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


        status = await _final_status(
            get_telescope().calibrate(tc.SolarSystemTarget(_name))
        )
        # ActivityStatus.COMPLETE = good, ActivityStatus.ABORTED = maybe bad
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

        # REVIEW: Here, I'm guessing we'll need something fancier than what
        # currently exists.  This `track` consists of a slew followed by
        # tracking. It doesn't make sense to notify about the tracking part,
        # since it lasts indefinitely, but callers might want to know when the
        # slew part has finished, and currently there is no way to get that
        # information.
        get_telescope().track(tc.FixedTarget(SkyCoord(ra=_ra, dec=_dec, frame=ICRS)))

        return await returnResponse({"goto": True, "ra": _ra, "dec": _dec}, 200)

    except:
        return await returnResponse({"goto": False, "ra": _ra, "dec": _dec}, 400)


@api.route("/goto/by_name/", methods=["POST"])
async def goto_by_name():
    try:
        _name = request.args.get("name")

        # REVIEW: See comment on `goto`.
        get_telescope().track(tc.FixedTarget(SkyCoord.from_name(_name)))

        return await returnResponse({"goto": True, "name": _name}, 200)
    except:
        return await returnResponse({"goto": False, "name": _name}, 400)


@api.route("/goto/mpc/", methods=["POST"])
async def goto_by_mpc_query():
    try:
        name = request.args.get("name")
        assert name is not None
        # REVIEW: See comment on `goto`.
        get_telescope().track(tc.MPCQueryTarget(name))

        return await returnResponse({"goto": True, "name": name}, 200)
    except:
        return await returnResponse({"goto": False}, 400)


@api.route("/goto/solar_system_object/", methods=["POST"])
async def goto_solar_system_object():
    try:
        _name = request.args.get("name")
        # REVIEW: See comment on `goto`.
        get_telescope().track(tc.SolarSystemTarget(_name))
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
