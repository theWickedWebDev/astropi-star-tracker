from __future__ import annotations

from dataclasses import dataclass
import logging
from enum import IntEnum
import math
from queue import Queue
from threading import Condition, Lock, Thread
import time
from typing import Protocol, TypeAlias
from typing_extensions import assert_never

import numpy as np

from .activity import Activity as _Activity, ActivityStatus
from .lib.nsleep import nsleep
from .motion import (
    pulse_times_linaccel,
    pulse_times_trapz,
    travel_linaccel,
    trapz_opt_v_c_and_t_to_intercept,
    trapz_v_c_to_intercept_at_t,
)

_log = logging.getLogger(__name__)

# TODO: In Stepper, track an error term for desired fractional steps so abutting
# activities with very slow movements can work perfectly.  Right now this gets
# worked around in TelescopeControl by issuing very long run_constant commands
# that guarantee a whole number of steps will occur.  This is inconvenient, and
# it would be better if Stepper handled fractional moves itself.


class _PulseFn(Protocol):
    def __call__(self, stepper: Stepper, direction: StepDir, /) -> None:
        ...


@dataclass(frozen=True)
class StepperConfig:
    min_sleep_ns: int
    max_speed: float  # steps/s
    max_accel: float  # steps/s/s
    max_decel: float  # steps/s/s
    pulse: _PulseFn
    max_interval_ns: int = 250_000_000


class StepDir(IntEnum):
    FWD = 1
    NOP = 0
    REV = -1


class _StepperActivity(_Activity):
    _goal: _Goal

    def __init__(self, goal: _Goal, cond: Condition):
        super().__init__(cond)
        self._goal = goal

    def __repr__(self):
        return f"{self.__class__.__name__}({self._goal!r}, {self._status.name})"


@dataclass
class _Intercept:
    target: int
    target_velocity: float
    final_velocity: float


@dataclass
class _InterceptPrecomputed:
    params: InterceptParams
    start_ns: int


@dataclass
class _RunConstant:
    velocity: float
    deadline_ns: int


# TODO: Rename to _Noop?  Maybe it could be eliminated?  Maybe _Idle should spin
# down to zero velocity, if needed?
@dataclass
class _Idle:
    pass


@dataclass
class _Stop:
    pass


_Goal: TypeAlias = _InterceptPrecomputed | _Intercept | _RunConstant | _Idle | _Stop


_MotionQueue: TypeAlias = Queue[tuple[int, StepDir] | _StepperActivity]


@dataclass
class _RunState:
    plan_thread: Thread
    run_thread: Thread


@dataclass
class _PlanContext:
    commit_pos: float
    commit_vel: float
    commit_deadline: int


class _StateFn(Protocol):
    def __call__(
        self, stepper: Stepper, motion: _MotionQueue, ctx: _PlanContext
    ) -> _StateFn | None:
        ...


class Stepper:
    _config: StepperConfig
    _position: int
    _velocity: float

    _lock: Lock
    _run_state: _RunState | None
    _activities: Queue[_StepperActivity]
    _activity_fallback_cond: Condition

    def __init__(
        self,
        config: StepperConfig,
        position: int = 0,
        velocity: float = 0,
    ):
        self._config = config
        self._position = position
        self._velocity = velocity

        self._lock = Lock()
        self._run_state = None
        self._activities = Queue()
        self._activity_fallback_cond = Condition()

    @property
    def config(self):
        return self._config

    @property
    def position(self):
        with self._lock:
            return self._position

    @property
    def velocity(self):
        with self._lock:
            return self._velocity

    def goto(
        self,
        target: int,
        final_velocity: float = 0,
        cond: Condition | None = None,
    ):
        return self._put_activity(_Intercept(target, 0, final_velocity), cond)

    def idle(self, cond: Condition | None = None):
        return self._put_activity(_Idle(), cond)

    def intercept_precomputed(
        self,
        params: InterceptParams,
        start_ns: int,
        cond: Condition | None = None,
    ):
        return self._put_activity(_InterceptPrecomputed(params, start_ns), cond)

    def intercept(
        self,
        target: int,
        target_velocity: float,
        final_velocity: float | None = None,
        cond: Condition | None = None,
    ):
        if final_velocity is None:
            final_velocity = target_velocity
        return self._put_activity(
            _Intercept(target, target_velocity, final_velocity), cond
        )

    def run_constant(
        self,
        velocity: float,
        deadline_ns: int,
        cond: Condition | None = None,
    ):
        return self._put_activity(_RunConstant(velocity, deadline_ns), cond)

    def _put_activity(self, goal: _Goal, cond: Condition | None) -> _Activity:
        activity = _StepperActivity(goal, cond or self._activity_fallback_cond)
        self._activities.put(activity)
        return activity

    def start(self):
        with self._lock:
            if self._run_state is not None:
                return

            # TODO: Make plan ahead steps configurable
            motion: _MotionQueue = Queue(maxsize=4)

            run_state = _RunState(
                plan_thread=Thread(target=_with_error_printing(self._plan), args=[motion]),
                run_thread=Thread(target=_with_error_printing(self._move), args=[motion]),
            )
            self._run_state = run_state

            for t in [run_state.plan_thread, run_state.run_thread]:
                t.start()

    # TODO: Should start/stop take a Condition and return an Activity?
    def stop(self, timeout: float | None = None):
        deadline = None
        if timeout is not None:
            deadline = time.time() + timeout

        with self._lock:
            run_state = self._run_state
            if run_state is None:
                return

        self._put_activity(_Stop(), None).wait_for(ActivityStatus.done)

        for t in [run_state.plan_thread, run_state.run_thread]:
            t.join(deadline - time.time() if deadline is not None else None)

        with self._lock:
            self._run_state = None

    def _plan(self, motion: _MotionQueue):
        with self._lock:
            ctx = _PlanContext(
                self._position,
                self._velocity,
                time.time_ns(),
            )

        statefn: _StateFn | None = _plan_dispatch
        while statefn is not None:
            statefn = statefn(self, motion, ctx)

    def _move(self, motion: _MotionQueue):
        while True:
            item = motion.get()
            if isinstance(item, _StepperActivity):
                with item._cond:
                    match item._status:
                        case ActivityStatus.ABORTING:
                            item._status = ActivityStatus.ABORTED
                        case ActivityStatus.ACTIVE:
                            item._status = ActivityStatus.COMPLETE
                        case _:
                            raise RuntimeError(
                                f"unexpected activity status: {item._status} on {item._goal}"
                            )

                    item._cond.notify_all()

                motion.task_done()
                if isinstance(item._goal, _Stop):
                    return
                continue

            try:
                deadline, d = item
                now = time.time_ns()

                sleep_ns = deadline - now
                if sleep_ns < self._config.min_sleep_ns:
                    if d != StepDir.NOP:
                        # FIXME: Better telemetry
                        _log.warn(f"running behind: {sleep_ns / 1_000_000_000}")
                    sleep_ns = self._config.min_sleep_ns

                nsleep(sleep_ns)

                if d != StepDir.NOP:
                    self.config.pulse(self, d)

                with self._lock:
                    self._position += d
                    # FIXME: Calculate velocity
                    self._velocity = 0
            finally:
                motion.task_done()


def _with_error_printing(fn):
    def wrapper(*args, **kwargs):
        try:
            fn(*args, **kwargs)
        except Exception:
            import traceback

            traceback.print_exc()

    return wrapper


@dataclass(frozen=True)
class InterceptParams:
    # TODO: Capture inputs also?

    delta: float
    t: float
    v_c: float
    a_in: float
    a_out: float
    p_f: float
    v_f: float


def compute_intercept(
    config: StepperConfig,
    position: float,
    velocity: float,
    target: float,
    target_velocity: float,
    final_velocity: float,
    t: float | None = None,
) -> InterceptParams:
    scratch_delta = target - position

    if scratch_delta == 0:
        return InterceptParams(
            delta=0,
            t=0 if t is None else t,
            v_c=0,
            a_in=0,
            a_out=0,
            p_f=position,
            v_f=final_velocity,
        )

    a_in = math.copysign(config.max_accel, scratch_delta)
    a_out = -math.copysign(config.max_decel, scratch_delta)

    if t is None:
        v_c, t = trapz_opt_v_c_and_t_to_intercept(
            config.max_speed,
            a_in,
            a_out,
            position,
            velocity,
            final_velocity,
            target,
            target_velocity,
        )
    else:
        v_c = trapz_v_c_to_intercept_at_t(
            a_in=a_in,
            a_out=a_out,
            p_i=position,
            v_i=velocity,
            v_f=final_velocity,
            q_i=target,
            u=target_velocity,
            t=t,
        )

    p_f = target + t * target_velocity
    delta = p_f - position
    return InterceptParams(
        delta=delta,
        t=t,
        v_c=v_c,
        a_in=a_in,
        a_out=a_out,
        p_f=p_f,
        v_f=final_velocity,
    )


def _plan_dispatch(stepper: Stepper, motion: _MotionQueue, ctx: _PlanContext):
    activity = stepper._activities.get()
    with activity._cond:
        assert activity._status == ActivityStatus.PENDING

    match activity._goal:
        case _Idle() | _Stop() as g:
            with activity._cond:
                activity._status = ActivityStatus.ACTIVE
                activity._cond.notify_all()
            motion.put(activity)
            if isinstance(g, _Stop):
                return None
            return _plan_dispatch
        case _Intercept() | _InterceptPrecomputed():
            return _plan_intercept(activity)
        case _RunConstant():
            return _plan_run_constant(activity)
        case _:
            assert_never(activity._goal)


def _plan_intercept(activity: _StepperActivity) -> _StateFn:
    assert isinstance(activity._goal, (_Intercept, _InterceptPrecomputed))
    goal = activity._goal

    def plan_intercept(stepper: Stepper, motion: _MotionQueue, ctx: _PlanContext):
        with activity._cond:
            assert activity._status == ActivityStatus.PENDING

            activity._status = ActivityStatus.ACTIVE
            activity._cond.notify_all()

        now = time.time_ns()
        # TODO: Need to incorporate Config.min_sleep_ns?
        ctx.commit_deadline = max(ctx.commit_deadline, now)
        match goal:
            case _InterceptPrecomputed(params, start_ns):
                # Just extract params and start_ns
                pass
            case _Intercept():
                start_ns = ctx.commit_deadline

                t0 = (start_ns - now) / 1_000_000_000
                target_t0 = goal.target + t0 * goal.target_velocity

                params = compute_intercept(
                    stepper._config,
                    ctx.commit_pos,
                    ctx.commit_vel,
                    target_t0,
                    goal.target_velocity,
                    goal.final_velocity,
                )

        if params.delta == 0:
            motion.put(activity)
            return _plan_dispatch

        dir = StepDir.FWD if params.delta > 0 else StepDir.REV
        deadlines = (
            start_ns
            + pulse_times_trapz(
                ctx.commit_vel,
                params.v_f,
                params.v_c,
                params.a_in,
                params.a_out,
                # TODO: Handle fractional positions, like in run_constant.
                round(params.delta),
            )
            * 1_000_000_000
        ).astype(np.int64)

        if len(deadlines) == 0:
            motion.put(activity)
            return _plan_dispatch

        velocities = np.empty_like(deadlines, dtype=np.float64)
        velocities[0] = ctx.commit_vel
        velocities[1:] = int(dir) * 1_000_000_000 / (deadlines[1:] - deadlines[:-1])

        for deadline, velocity in zip(deadlines, velocities):
            with activity._cond:
                if activity._canceled:
                    return _plan_abort(activity)

            for d in _nop_deadlines(
                ctx.commit_deadline,
                deadline,
                stepper.config.max_interval_ns,
            ):
                with activity._cond:
                    if activity._canceled:
                        return _plan_abort(activity)
                motion.put((d, StepDir.NOP))

            ctx.commit_deadline = deadline
            ctx.commit_pos += dir
            ctx.commit_vel = velocity
            motion.put((deadline, dir))
        else:
            motion.put(activity)
            return _plan_dispatch

    return plan_intercept


def _plan_run_constant(activity: _StepperActivity) -> _StateFn:
    assert isinstance(activity._goal, _RunConstant)
    goal = activity._goal

    def plan_run_constant(stepper: Stepper, motion: _MotionQueue, ctx: _PlanContext):
        with activity._cond:
            assert activity._status == ActivityStatus.PENDING
            activity._status = ActivityStatus.ACTIVE
            activity._cond.notify_all()

        ctx.commit_deadline = max(ctx.commit_deadline, time.time_ns())

        if goal.velocity == 0:
            if ctx.commit_deadline < goal.deadline_ns:
                ctx.commit_deadline = goal.deadline_ns
                motion.put((goal.deadline_ns, StepDir.NOP))
            motion.put(activity)
            return _plan_dispatch

        # TODO: Deal with quantization errors (should be pretty small)
        ideal_interval = int(abs(1_000_000_000 / goal.velocity))
        dir = StepDir.FWD if goal.velocity > 0 else StepDir.REV

        interval = ideal_interval
        next_int_pos = _next_int(ctx.commit_pos, dir)
        next_int_t = (next_int_pos - ctx.commit_pos) / goal.velocity
        next_int_interval = round(1_000_000_000 * next_int_t)
        if next_int_interval != 0:
            interval = next_int_interval

        done = False
        while not done:
            with activity._cond:
                if activity._canceled:
                    return _plan_abort(activity)

            deadline = round(ctx.commit_deadline + interval)
            interval = ideal_interval
            if deadline >= goal.deadline_ns:
                done = True
                if deadline > goal.deadline_ns:
                    dir = StepDir.NOP
                deadline = goal.deadline_ns

            for d in _nop_deadlines(
                ctx.commit_deadline,
                deadline,
                stepper.config.max_interval_ns,
            ):
                with activity._cond:
                    if activity._canceled:
                        return _plan_abort(activity)
                _commit_const_vel(ctx, d, goal.velocity)
                motion.put((d, StepDir.NOP))

            _commit_const_vel(ctx, deadline, goal.velocity)
            motion.put((deadline, dir))

        motion.put(activity)
        return _plan_dispatch

    return plan_run_constant


def _commit_const_vel(ctx: _PlanContext, deadline: int, v: float):
    dp = v * (deadline - ctx.commit_deadline) / 1_000_000_000
    ctx.commit_deadline = deadline
    ctx.commit_pos += dp
    ctx.commit_vel = v


def _next_int(x: float, dir: StepDir):
    assert dir != StepDir.NOP

    tp = np.floor(x)

    if x == tp:
        return int(x)

    adj = tp - x
    if dir == StepDir.FWD:
        return int(x + adj + 1)

    return int(x + adj)


def _plan_abort(activity: _StepperActivity) -> _StateFn:
    def plan_abort(stepper: Stepper, motion: _MotionQueue, ctx: _PlanContext):
        with activity._cond:
            activity._status = ActivityStatus.ABORTING
            activity._cond.notify_all()

        ctx.commit_deadline = max(ctx.commit_deadline, time.time_ns())

        if ctx.commit_vel == 0:
            motion.put(activity)
            return _plan_dispatch

        cfg = stepper._config

        dir = StepDir.FWD if ctx.commit_vel > 0 else StepDir.REV
        a_out = -math.copysign(cfg.max_decel, ctx.commit_vel)
        steps_frac = travel_linaccel(ctx.commit_vel, 0, a_out)
        # TODO: It would be better to round away from zero, but it's not
        # convenient with numpy or the std lib.  So, we just always add one
        # extra step.
        steps = int(np.trunc(steps_frac) + math.copysign(1, steps_frac))

        deadlines = (
            ctx.commit_deadline
            + pulse_times_linaccel(steps, ctx.commit_vel, a_out) * 1_000_000_000
        ).astype(np.int64)

        velocities = np.empty_like(deadlines, dtype=np.float64)
        velocities[0] = ctx.commit_vel
        velocities[1:] = (
            (1 if dir == StepDir.FWD else -1)
            * 1_000_000_000
            / (deadlines[1:] - deadlines[:-1])
        )

        for deadline, velocity in zip(deadlines, velocities):
            ctx.commit_deadline = deadline
            ctx.commit_pos += dir
            ctx.commit_vel = velocity
            motion.put((deadline, dir))
        else:
            motion.put(activity)
            return _plan_dispatch

    return plan_abort


def _nop_deadlines(
    from_ns: int,
    to_ns: int,
    max_interval_ns: int,
):
    return np.arange(from_ns + max_interval_ns, to_ns, max_interval_ns)
