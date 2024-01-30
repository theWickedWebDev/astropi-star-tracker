from __future__ import annotations
import argparse
import logging
import logging.config
import os
import signal

from astropy.coordinates import EarthLocation, HADec, SkyCoord
from astropy.time import Time
import astropy.units as u
import trio

from .lib import stellarium
from .lib.nsleep import nsleep
from .stepper import StepDir, Stepper, StepperConfig
from . import appserver
from . import telescope_control as tc

_log = logging.getLogger(__name__)

try:
    with open(os.path.join(os.path.dirname(__file__), "logging.json"), "r") as f:
        import json
        import logging.config

        logging.config.dictConfig(json.load(f))
except FileNotFoundError:
    pass


STEPHEN_HOUSE = EarthLocation(
    lat=42.8164989 * u.deg,  # pyright: ignore
    lon=-71.0638871 * u.deg,  # pyright: ignore
    height=52.32 * u.m,  # pyright: ignore
)

def orientation_from_skycoord(coord: SkyCoord) -> tc.TelescopeOrientation:
    tcoord = coord.transform_to(HADec(obstime=Time.now(), location=STEPHEN_HOUSE))
    return tcoord.ha, tcoord.dec


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--virtual",
        action="store_true",
        default=False,
        help="When set, run in virtual mode (only serving to Stellarium, no motor control)",
    )

    args = parser.parse_args()

    if args.virtual:
        bearing_pulse = virtual_pulse("bearing")
        dec_pulse = virtual_pulse("dec")
    else:
        from .lib import motor

        bearing_pulse = rpi_pulse(motor.RA_PINS)
        dec_pulse = rpi_pulse(motor.DEC_PINS, fwd=0, rev=1)

    MAX_SPEED=1200
    MAX_ACCEL=600
    MAX_DECEL=600

    telescope = tc.TelescopeControl(
        config=tc.Config(
            bearing_axis=tc.StepperAxis(
                motor_steps=800,
                gear_ratio=4 * 4 * 4 * 4,  # / (1.00985 + .00985 + .00985 + .00985),
                config=StepperConfig(
                    min_sleep_ns=50_000,
                    max_speed=MAX_SPEED,
                    max_accel=MAX_ACCEL,
                    max_decel=MAX_DECEL,
                    pulse=bearing_pulse,
                ),
            ),
            declination_axis=tc.StepperAxis(
                motor_steps=800,
                gear_ratio=4 * 4 * 4 * 4,
                config=StepperConfig(
                    min_sleep_ns=50_000,
                    max_speed=MAX_SPEED,
                    max_accel=MAX_ACCEL,
                    max_decel=MAX_DECEL,
                    pulse=dec_pulse,
                ),
            ),
            location=STEPHEN_HOUSE,
        ),
    )

    app = appserver.create_app(telescope)

    try:
        with trio.open_signal_receiver(signal.SIGTERM) as sigs:
            async with trio.open_nursery() as n:
                n.start_soon(telescope.run)
                n.start_soon(stellarium.serve, "0.0.0.0", 10001, telescope)
                n.start_soon(app.run_task, "0.0.0.0", 8765)

                async for sig in sigs:
                    if sig == signal.SIGTERM:
                        n.cancel_scope.cancel()

    except KeyboardInterrupt:
        pass


def virtual_pulse(name: str):
    def pulse(stepper: Stepper, direction: StepDir):
        _log.debug(f"pulse {name}: {direction}")

    return pulse


def rpi_pulse(pins, fwd=1, rev=0):
    from .lib import motor

    def pulse(stepper: Stepper, direction: StepDir):
        match direction:
            case StepDir.FWD:
                motor.step(pins, fwd)
            case StepDir.REV:
                motor.step(pins, rev)

    return pulse


if __name__ == "__main__":
    print('ᐰ Stargate SG-1 Deep Space Telescope')
    trio.run(main)
