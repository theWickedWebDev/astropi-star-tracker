from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import functools
import logging
import math
import multiprocessing as mp
import multiprocessing.connection as mpc
import multiprocessing.synchronize as mps
from queue import Queue
from threading import Condition, Event, Thread
import time
from typing import Any, Protocol, TypeAlias
from typing_extensions import assert_never
from uuid import UUID, uuid4

import astropy.units as u
from astropy.coordinates import (
    EarthLocation,
    SkyCoord,
    HADec,
    ICRS,
    get_body,
    solar_system_ephemeris,
)
from astropy.time import Time
import numpy as np
import trio

from .activity import Activity as _Activity, ActivityStatus
from .motion import trapz_v_c_to_intercept_at_t
from .stepper import Stepper, StepperConfig, compute_intercept

TelescopeOrientation: TypeAlias = tuple[u.Quantity["angle"], u.Quantity["angle"]]
StatusChannel: TypeAlias = trio.MemoryReceiveChannel[tuple[ActivityStatus, Any]]
_SendStatusChannel: TypeAlias = trio.MemorySendChannel[tuple[ActivityStatus, Any]]

@dataclass(eq=False)
class _Track:
    target: Target
    id: UUID = field(default_factory=uuid4)


@dataclass(eq=False)
class _Idle:
    id: UUID = field(default_factory=uuid4)


@dataclass(eq=False)
class _Stop:
    id: UUID = field(default_factory=uuid4)


@dataclass(eq=False)
class _Calibrate:
    bearing: u.Quantity["angle"]
    dec: u.Quantity["angle"]
    id: UUID = field(default_factory=uuid4)


@dataclass(eq=False)
class _CalibrateRelSteps:
    bearing: int
    dec: int
    id: UUID = field(default_factory=uuid4)


_Goal: TypeAlias = _Track | _Idle | _Stop


class _TelescopeActivity(_Activity):
    _goal: _Goal

    def __init__(self, goal: _Goal, cond: Condition):
        super().__init__(cond)
        self._goal = goal

    def __repr__(self):
        return f"{self.__class__.__name__}({self._goal!r}, {self._status.name})"


@dataclass(frozen=True)
class StepperAxis:
    motor_steps: int
    gear_ratio: float
    config: StepperConfig


@dataclass(frozen=True)
class Config:
    bearing_axis: StepperAxis
    declination_axis: StepperAxis

    location: EarthLocation
    predict_ns: int = 30_000_000_000
    publish_interval: float = 0.25


class Busy(Exception):
    pass


class Target(Protocol):
    def coordinate(self, time: Time, location: EarthLocation) -> SkyCoord:
        ...


@dataclass
class FixedTarget(Target):
    coord: SkyCoord

    def coordinate(self, time: Time, location: EarthLocation):
        return self.coord


@dataclass
class SolarSystemTarget(Target):
    name: str

    def coordinate(self, time: Time, location: EarthLocation):
        with solar_system_ephemeris.set("builtin"):
            return get_body(self.name, time, location)


@dataclass
class MPCQueryTarget(Target):
    name: str

    def coordinate(self, time: Time, location: EarthLocation):
        from astroquery.mpc import MPC

        eph = MPC.get_ephemeris(  # pyright: ignore
            self.name,
            start=time,
            location=location,
            number=1,
        )

        return SkyCoord(
            ra=eph["RA"].to(u.hourangle)[0],
            dec=eph["Dec"].to(u.deg)[0],
            frame=ICRS,
        )

class TelescopeControl:
    _config: Config
    _conn: mpc.Connection | None
    _orientation: TelescopeOrientation
    _target: Target | None
    _log: logging.Logger
    _status_chans: dict[UUID, _SendStatusChannel]

    def __init__(self, config: Config):
        self._config = config
        self._conn = None
        self._orientation = (
            0 * u.hourangle,  # pyright: ignore
            0 * u.deg,  # pyright: ignore
        )
        self._target = None
        self._log = logging.getLogger(__name__)
        self._status_chans = {}

    @property
    def config(self):
        return self._config

    @property
    def orientation(self):
        return self._orientation

    @property
    def target(self):
        return self._target

    def track(self, target: Target) -> StatusChannel:
        return self._put_message(_Track(target))

    def current_skycoord(self):
        bearing, dec = self._orientation

        return SkyCoord(
            bearing,
            dec,
            frame=HADec(obstime=Time.now(), location=self._config.location),
        ).transform_to(ICRS)

    def calibrate(self, target: Target, track: bool = True) -> StatusChannel:
        coord = target.coordinate(Time.now(), self.config.location)
        tcoord = coord.transform_to(
            HADec(obstime=Time.now(), location=self.config.location)
        )

        bearing: u.Quantity["angle"] = tcoord.ha  # pyright: ignore
        dec: u.Quantity["angle"] = tcoord.dec  # pyright: ignore

        chan = self._put_message(_Calibrate(bearing, dec))
        if track:
            self._put_message(_Track(target))
        return chan

    def calibrate_rel_steps(self, bearing: int, dec: int) -> StatusChannel:
        return self._put_message(_CalibrateRelSteps(bearing, dec))

    async def run(self):
        conn, child_conn = mp.Pipe()
        # The type definitions for mp.Pipe are different  on Unix and Windows.
        # They are nominally incompatible so there is a type error, but
        # structurally compatible, so all uses succeed.  Just ignore the error.
        # TODO: Define a protocol that defines the part of the [Pipe]Connection
        # interface that we rely on?
        self._conn = conn  # pyright: ignore
        stop = mp.Event()

        async with trio.open_nursery() as n:
            n.start_soon(
                functools.partial(
                    trio.to_thread.run_sync,
                    self._run_multiprocess,
                    conn,
                    child_conn,
                    stop,
                )
            )

            try:
                await trio.sleep_forever()
            except:
                stop.set()
                raise

    def _put_message(self, msg: _InputMessage) -> StatusChannel:
        if self._conn is None:
            raise RuntimeError("must be running to issue commands")

        send_ch, recv_ch = trio.open_memory_channel[tuple[ActivityStatus, Any]](math.inf)
        self._status_chans[msg.id] = send_ch
        self._conn.send(msg)
        return recv_ch

    def _run_multiprocess(
        self,
        conn: mpc.Connection,
        child_conn: mpc.Connection,
        stop: mps.Event,
    ):
        proc = mp.Process(
            target=_with_error_printing(_mp_main), args=[self.config, child_conn]
        )
        proc.start()

        child_had_error = False
        stop_deadline = None
        while proc.exitcode is None and (
            stop_deadline is None or time.time() < stop_deadline
        ):
            if stop.is_set():
                stop_deadline = time.time() + 5
                self._log.info("stopping")
                self._put_message(_Stop())
                stop.clear()

            if conn.poll(1):
                msg: _OutputMessage = conn.recv()
                match msg:
                    case _Log(record):
                        self._log.callHandlers(record)
                    case _PublishOrientation(orientation):

                        def update():
                            self._orientation = orientation

                        trio.from_thread.run_sync(update)
                    case _PublishTarget(target):

                        def update():
                            self._target = target

                        trio.from_thread.run_sync(update)
                    case _ChildError():
                        child_had_error = True
                        stop.set()
                    case _RequestProgress():

                        def update():
                            try:
                                chan = self._status_chans[msg.request.id]
                            except KeyError:
                                self._log.error(f"no channel found for request {msg.request}")
                            else:
                                chan.send_nowait((msg.status, msg.extra))
                                if msg.status.done():
                                    self._status_chans.pop(msg.request.id)
                                    chan.close()

                        trio.from_thread.run_sync(update)
                    case _:
                        assert_never(msg)

        if proc.exitcode is None:
            self._log.error("timed out waiting for telescope control to stop")
            proc.kill()

        if child_had_error:
            raise Exception("telescope error (see above)") from None


@dataclass
class _PublishTarget:
    target: Target | None


@dataclass
class _PublishOrientation:
    orientation: TelescopeOrientation


@dataclass
class _Log:
    record: logging.LogRecord


@dataclass
class _ChildError:
    pass


@dataclass
class _RequestProgress:
    request: _InputMessage
    status: ActivityStatus
    extra: Any = None


_InputMessage: TypeAlias = _Calibrate | _CalibrateRelSteps | _Goal
_OutputMessage: TypeAlias = _PublishTarget | _PublishOrientation | _Log | _ChildError | _RequestProgress


class StateFn(Protocol):
    def __call__(
        self,
        ctx: _RunContext,
        conn: mpc.Connection,
    ) -> StateFn | None:
        ...


@dataclass
class _RunContext:
    config: Config
    bearing_motor: Stepper
    dec_motor: Stepper
    # Logger objects are not multiprocess safe, so we create a new Logger in the
    # child process.
    log: logging.Logger
    bearing_offset: int = 0
    dec_offset: int = 0
    target: Target | None = None
    activity: _TelescopeActivity | None = None
    stop: Event = field(default_factory=Event)
    cond: Condition = field(default_factory=Condition)
    activity_cond: Condition = field(default_factory=Condition)

    @property
    def bearing_steps(self):
        return self.bearing_motor.position + self.bearing_offset

    @property
    def dec_steps(self):
        return self.dec_motor.position + self.dec_offset


def _with_error_printing(fn):
    def wrapper(*args, **kwargs):
        try:
            fn(*args, **kwargs)
        except Exception:
            import traceback

            traceback.print_exc()

    return wrapper


def _mp_main(config: Config, conn: mpc.Connection):
    log = logging.Logger(__name__ + ".mp", logging.getLogger(__name__).level)

    class LogHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            conn.send(_Log(record))

    log.addHandler(LogHandler())

    log.debug("starting")

    ctx = _RunContext(
        config=config,
        bearing_motor=Stepper(config.bearing_axis.config),
        dec_motor=Stepper(config.declination_axis.config),
        log=log,
    )

    ctx.bearing_motor.start()
    ctx.dec_motor.start()

    activity_queue = Queue()

    threads = [
        Thread(target=target, args=args)
        for target, args in [
            (_with_error_printing(_publish_state), [ctx, conn]),
            (_with_error_printing(_read_goals), [ctx, conn, activity_queue]),
            (_with_error_printing(_watch_activities), [conn, activity_queue]),
        ]
    ]

    for t in threads:
        t.start()

    log.debug("running")
    try:
        _run(ctx, conn)
    except Exception as e:
        conn.send(_ChildError())
        raise e
    finally:
        ctx.stop.set()

        for t in threads:
            t.join()

        log.debug("threads complete")

        ctx.bearing_motor.stop()
        log.debug("bearing motor stopped")

        ctx.dec_motor.stop()
        log.debug("dec motor stopped")

    log.debug(f"stopped")

    import os, signal

    # FIXME: Sometimes (intermittently) the multiprocess child fails to exit,
    # even though this function has returned.  So, instead, just kill ourselves.
    os.kill(os.getpid(), signal.SIGKILL)


# TODO: Rename this?
def _run(ctx: _RunContext, conn: mpc.Connection):
    statefn: StateFn | None = _run_dispatch
    while statefn is not None:
        statefn = statefn(ctx, conn)


def _finalize_activity(activity: _TelescopeActivity):
    with activity._cond:
        match activity._status:
            case ActivityStatus.PENDING | ActivityStatus.ABORTING:
                activity._status = ActivityStatus.ABORTED
            case ActivityStatus.ACTIVE:
                activity._status = ActivityStatus.COMPLETE
            case ActivityStatus.ABORTED | ActivityStatus.COMPLETE:
                pass
            case _:
                assert_never(activity._status)
        activity._cond.notify_all()


def _clear_activity(ctx: _RunContext, activity: _TelescopeActivity):
    with ctx.cond:
        if ctx.activity is activity:
            ctx.activity = None
            ctx.cond.notify()


def _run_dispatch(ctx: _RunContext, conn: mpc.Connection) -> StateFn:
    with ctx.cond:
        ctx.cond.wait_for(lambda: ctx.activity is not None)
        activity = ctx.activity
        assert activity is not None

    with activity._cond:
        activity._status = ActivityStatus.ACTIVE
        activity._cond.notify_all()

    match activity._goal:
        case _Track():
            return _run_track(activity)
        case _Stop():
            return _run_stop(activity)
        case _Idle():
            return _run_idle(activity)
        case _:
            assert_never(activity._goal)


def _read_goals(ctx: _RunContext, conn: mpc.Connection, activity_queue: Queue[_Activity]):
    while True:
        msg: _InputMessage = conn.recv()
        match msg:
            case (_Track() | _Stop() | _Idle()) as goal:
                ctx.log.debug(f"received goal {goal}")
                with ctx.cond:
                    if ctx.activity is not None:
                        ctx.log.debug(f"canceling activity: {ctx.activity}")
                        ctx.activity.cancel()
                    activity = _TelescopeActivity(goal, ctx.activity_cond)
                    activity_queue.put(activity)
                    ctx.activity = activity
                    ctx.log.debug(f"set activity: {ctx.activity}")
                    ctx.cond.notify()

                if isinstance(goal, _Stop):
                    break
            case _Calibrate(bearing, dec):
                with ctx.cond:
                    ctx.bearing_offset = round(
                        _angle_to_steps(ctx.config.bearing_axis, bearing)
                        - ctx.bearing_motor.position
                    )
                    ctx.dec_offset = round(
                        _angle_to_steps(ctx.config.declination_axis, dec)
                        - ctx.dec_motor.position
                    )
                ctx.log.debug(f"calibrated: {ctx.bearing_offset}, {ctx.dec_offset}")
                conn.send(_RequestProgress(msg, ActivityStatus.COMPLETE))
            case _CalibrateRelSteps(bearing, dec):
                with ctx.cond:
                    ctx.bearing_offset += bearing
                    ctx.dec_offset += dec
                ctx.log.debug(f"calibrated: {ctx.bearing_offset}, {ctx.dec_offset}")
                conn.send(_RequestProgress(msg, ActivityStatus.COMPLETE))
            case _:
                assert_never(msg)

def _watch_activities(conn: mpc.Connection, queue: Queue[_Activity]):
    while True:
        activity = queue.get()

        with activity._cond:
            status = activity._status

        while True:
            conn.send(_RequestProgress(activity._goal, status))

            if status.done():
                break

            with activity._cond:
                activity._cond.wait_for(lambda: activity._status != status)
                status = activity._status

        if isinstance(activity._goal, _Stop):
            break


_: StateFn = _run_dispatch

_ActivityGroup = list[_Activity]


def _ag_cancel_all(groups: list[_ActivityGroup]):
    for group in groups:
        for act in group:
            act.cancel()

    for group in groups:
        for act in group:
            act.wait_for(ActivityStatus.done)


def _ag_wait_one_group(parent: _TelescopeActivity, groups: list[_ActivityGroup]):
    if len(groups) == 0:
        raise ValueError("activity_groups must not be empty")

    for act in groups[0]:
        while not act.wait_for(ActivityStatus.done, timeout=0.5):
            if parent._canceled:
                with parent._cond:
                    parent._status = ActivityStatus.ABORTING
                    parent._cond.notify_all()
                _ag_cancel_all(groups)
                with parent._cond:
                    parent._status = ActivityStatus.ABORTED
                    parent._cond.notify_all()
                return None
    return groups.pop(0)


def _run_track(activity: _TelescopeActivity) -> StateFn:
    assert isinstance(activity._goal, _Track)
    goal = activity._goal

    def run_track(ctx: _RunContext, conn: mpc.Connection):
        if activity._canceled:
            _finalize_activity(activity)
            _clear_activity(ctx, activity)
            return _run_dispatch

        with ctx.cond:
            ctx.target = goal.target
            ctx.cond.notify_all()

        try:
            predict_dt_ns = ctx.config.predict_ns
            predict_dt = predict_dt_ns * u.nanosecond  # pyright: ignore

            # Predict target location a short time in the future (to leave time
            # for the initial calculations)
            planned_to_ns = time.time_ns() + 500_000_000
            planned_to_time = Time(
                datetime.fromtimestamp(planned_to_ns / 1_000_000_000, timezone.utc)
            )

            tgt_bearing_steps, tgt_dec_steps = _predict_pos(
                ctx, goal.target, planned_to_time
            )

            tgt_bearing_vel, tgt_dec_vel = _predict_vel(
                ctx, goal.target, planned_to_time, predict_dt
            )

            bearing_kwargs = {
                "config": ctx.bearing_motor.config,
                "position": ctx.bearing_steps,
                "velocity": ctx.bearing_motor.velocity,
                "target": tgt_bearing_steps,
                "target_velocity": tgt_bearing_vel,
                "final_velocity": tgt_bearing_vel,
            }
            bearing_params = compute_intercept(**bearing_kwargs)

            dec_kwargs = {
                "config": ctx.dec_motor.config,
                "position": ctx.dec_steps,
                "velocity": ctx.dec_motor.velocity,
                "target": tgt_dec_steps,
                "target_velocity": tgt_dec_vel,
                "final_velocity": tgt_dec_vel,
            }
            dec_params = compute_intercept(**dec_kwargs)

            activity_groups: list[_ActivityGroup] = []

            if bearing_params.t < dec_params.t:
                bearing_params = compute_intercept(**bearing_kwargs, t=dec_params.t)
            elif bearing_params.t > dec_params.t:
                dec_params = compute_intercept(**dec_kwargs, t=bearing_params.t)
            else:
                ctx.log.debug("lucky you! synchronicity.")

            intercept_group = [
                ctx.bearing_motor.intercept_precomputed(bearing_params, planned_to_ns),
                ctx.dec_motor.intercept_precomputed(dec_params, planned_to_ns),
            ]

            planned_to_ns += round(max(dec_params.t, bearing_params.t) * 1_000_000_000)
            activity_groups.append(intercept_group)

            while True:
                # NOTE: Assuming velocity doesn't change too much, so we don't need
                # a ramp.  For more dynamic objects, the situation would be more
                # complex.

                planned_to_time = Time(
                    datetime.fromtimestamp(planned_to_ns / 1_000_000_000, timezone.utc)
                )
                tgt_bearing_vel, tgt_dec_vel = _predict_vel(
                    ctx, goal.target, planned_to_time, predict_dt
                )

                ctx.log.debug(
                    f"planning tracking segment: {predict_dt_ns / 1_000_000_000:.2f} s"
                )

                planned_to_ns += predict_dt_ns
                activity_groups.append(
                    [
                        ctx.bearing_motor.run_constant(tgt_bearing_vel, planned_to_ns),
                        ctx.dec_motor.run_constant(tgt_dec_vel, planned_to_ns),
                    ]
                )

                match _ag_wait_one_group(activity, activity_groups):
                    case None:
                        if activity_groups[0] == intercept_group:
                            ctx.log.info("canceled while intercepting")
                        else:
                            ctx.log.info("canceled while tracking")
                        break
                    case completed if completed is intercept_group:
                        with activity._cond:
                            status = activity._status
                        conn.send(_RequestProgress(goal, status, "slew_complete"))
                    case _:
                        pass

            _finalize_activity(activity)
            _clear_activity(ctx, activity)
            return _run_dispatch
        finally:
            with ctx.cond:
                ctx.target = None
                ctx.cond.notify_all()

    return run_track


def _predict_pos_raw(config: Config, target: Target, t: Time):
    """returns values in angle / time"""

    frame = HADec(obstime=t, location=config.location)
    hadec = target.coordinate(t, config.location).transform_to(frame)

    bearing = hadec.ha.to(u.rad)  # pyright: ignore
    dec = hadec.dec.to(u.rad)  # pyright: ignore

    return bearing, dec


def _predict_pos(ctx: _RunContext, target: Target, t: Time):
    """returns values in steps"""
    bearing, dec = _predict_pos_raw(ctx.config, target, t)

    return (
        _angle_to_steps(ctx.config.bearing_axis, bearing),
        _angle_to_steps(ctx.config.declination_axis, dec),
    )


def _predict_vel(
    ctx: _RunContext, target: Target, t: Time, predict_dt: u.Quantity["time"]
):
    """returns values in steps / s"""
    t0 = t
    t1 = t + predict_dt

    tgt_bearing_t0, tgt_dec_t0 = _predict_pos_raw(ctx.config, target, t0)
    tgt_bearing_t1, tgt_dec_t1 = _predict_pos_raw(ctx.config, target, t1)

    ha_vel = (tgt_bearing_t1 - tgt_bearing_t0) / predict_dt.to(u.s).value
    dec_vel = (tgt_dec_t1 - tgt_dec_t0) / predict_dt.to(u.s).value

    bearing_vel_steps = (
        ha_vel.to(u.rad) / _angle_per_step(ctx.config.bearing_axis).to(u.rad)
    ).value
    dec_vel_steps = (
        dec_vel.to(u.rad) / _angle_per_step(ctx.config.declination_axis).to(u.rad)
    ).value

    return bearing_vel_steps, dec_vel_steps


def _angle_per_step(axis: StepperAxis) -> u.Quantity["angle"]:
    return 2 * np.pi / (axis.motor_steps * axis.gear_ratio) * u.rad


def _angle_to_steps(axis: StepperAxis, a: u.Quantity["angle"]) -> int:
    return round(a.to(u.rad).value / _angle_per_step(axis).to(u.rad).value)


def _steps_to_angle(axis: StepperAxis, steps: int) -> u.Quantity["angle"]:
    return steps * _angle_per_step(axis)


def _run_idle(activity: _TelescopeActivity) -> StateFn:
    assert isinstance(activity._goal, _Idle)

    def run_idle(ctx: _RunContext, conn: mpc.Connection):
        ctx.log.info("going idle")
        return _run_dispatch

    return run_idle


def _run_stop(activity: _TelescopeActivity) -> StateFn:
    assert isinstance(activity._goal, _Stop)

    def run_stop(ctx: _RunContext, conn: mpc.Connection):
        ctx.log.info("stopping")
        ctx.stop.set()
        return None

    return run_stop


def _publish_state(ctx: _RunContext, conn: mpc.Connection):
    prev_bearing_steps = None
    prev_dec_steps = None
    prev_target = None

    cfg = ctx.config

    while not ctx.stop.wait(cfg.publish_interval):
        with ctx.cond:
            target = ctx.target
            bearing_steps = ctx.bearing_steps
            dec_steps = ctx.dec_steps

        if bearing_steps != prev_bearing_steps or dec_steps != prev_dec_steps:
            prev_bearing_steps = bearing_steps
            prev_dec_steps = dec_steps

            orientation = (
                _steps_to_angle(cfg.bearing_axis, bearing_steps),
                _steps_to_angle(cfg.declination_axis, dec_steps),
            )
            conn.send(_PublishOrientation(orientation))
        if target is not prev_target:
            prev_target = target
            conn.send(_PublishTarget(target))
