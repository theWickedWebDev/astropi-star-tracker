from threading import Lock
from .nsleep import nsleep
import gpiod
from gpiod.line import Direction, Value

FULL_STEP = "000"
HALF_STEP = "001"
QUARTER_STEP = "010"
EIGHTH_STEP = "011"
SIXTEENTH_STEP = "100"
THIRTY_SECOND_STEP = "101"

RA_ENABLE_PIN = 12
RA_PULSE_PIN = 19
RA_DIRECTION_PIN = 13
RA_MODE_0=16
RA_MODE_1=17
RA_MODE_2=20

DEC_ENABLE_PIN = 4
DEC_PULSE_PIN = 18
DEC_DIRECTION_PIN = 24
DEC_MODE_0=21
DEC_MODE_1=22
DEC_MODE_2=27

MODE=QUARTER_STEP

with gpiod.request_lines(
    "/dev/gpiochip4",
    consumer="microstepping-mode",
    config={
        RA_MODE_0: gpiod.LineSettings(
            direction=Direction.OUTPUT, output_value=Value.ACTIVE
        ),
        RA_MODE_1: gpiod.LineSettings(
            direction=Direction.OUTPUT, output_value=Value.ACTIVE
        ),
        RA_MODE_2: gpiod.LineSettings(
            direction=Direction.OUTPUT, output_value=Value.ACTIVE
        ),
        DEC_MODE_0: gpiod.LineSettings(
            direction=Direction.OUTPUT, output_value=Value.ACTIVE
        ),
        DEC_MODE_1: gpiod.LineSettings(
            direction=Direction.OUTPUT, output_value=Value.ACTIVE
        ),
        DEC_MODE_2: gpiod.LineSettings(
            direction=Direction.OUTPUT, output_value=Value.ACTIVE
        )
    },
) as request:
        request.set_value(RA_MODE_0, Value.ACTIVE if MODE[0] == "1" else Value.INACTIVE)
        request.set_value(RA_MODE_1, Value.ACTIVE if MODE[1] == "1" else Value.INACTIVE)
        request.set_value(RA_MODE_2, Value.ACTIVE if MODE[2] == "1" else Value.INACTIVE)
        request.set_value(DEC_MODE_0, Value.ACTIVE if MODE[0] == "1" else Value.INACTIVE)
        request.set_value(DEC_MODE_1, Value.ACTIVE if MODE[1] == "1" else Value.INACTIVE)
        request.set_value(DEC_MODE_2, Value.ACTIVE if MODE[2] == "1" else Value.INACTIVE)


RA_PINS = dict(
    dir=RA_DIRECTION_PIN,
    en=RA_ENABLE_PIN,
    pul=RA_PULSE_PIN,
)

DEC_PINS = dict(
    dir=DEC_DIRECTION_PIN,
    en=DEC_ENABLE_PIN,
    pul=DEC_PULSE_PIN,
)

PULSE_NS = 50_000

# TODO: Figure out a nicer way to avoid simultaneous pulses clashing.
# _PULSE_LOCK = Lock()


def step(pins, dir):
    # with _PULSE_LOCK:
        _leading(pins, dir)
        nsleep(PULSE_NS)
        _trailing(pins)


def _leading(pins: dict, dir):
    EN_PIN=pins["en"]
    PUL_PIN=pins["pul"]
    DIR_PIN=pins["dir"]

    with gpiod.request_lines(
        "/dev/gpiochip4",
        consumer="stepper_leading_edge",
        config={
            EN_PIN: gpiod.LineSettings(
                direction=Direction.OUTPUT, output_value=Value.ACTIVE
            ),
            PUL_PIN: gpiod.LineSettings(
                direction=Direction.OUTPUT, output_value=Value.ACTIVE
            ),
            DIR_PIN: gpiod.LineSettings(
                direction=Direction.OUTPUT, output_value=Value.ACTIVE
            )
        },
    ) as request:
            request.set_value(EN_PIN, Value.INACTIVE)
            request.set_value(PUL_PIN, Value.INACTIVE)
            request.set_value(DIR_PIN, Value.ACTIVE if dir == 1 else Value.INACTIVE)
  
    

def _trailing(pins: dict):
    EN_PIN=pins["en"]
    PUL_PIN=pins["pul"]

    with gpiod.request_lines(
            "/dev/gpiochip4",
            consumer="stepper_trailing_edgee",
            config={
                PUL_PIN: gpiod.LineSettings(
                    direction=Direction.OUTPUT, output_value=Value.ACTIVE
                ),
                EN_PIN: gpiod.LineSettings(
                    direction=Direction.OUTPUT, output_value=Value.ACTIVE
                )
            },
    ) as request:
            request.set_value(EN_PIN, Value.ACTIVE)
            request.set_value(PUL_PIN, Value.ACTIVE)

