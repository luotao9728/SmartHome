"""RGB LED strip driver (three MOSFET channels).

Included as the worked example for a multi-property device: colour parsing,
effects and gamma correction, all without blocking the network loop.
"""

from ..compat import asyncio, sleep_ms
from ..device import DeviceError
from .base import Driver, make_pwm
from .pwm_led_strip import gamma_lut

MAX_DUTY = 65535


def parse_color(value) -> tuple:
    text = str(value or "").strip()
    if len(text) == 7 and text[0] == "#":
        text = text[1:]
    if len(text) != 6:
        raise DeviceError("colour must look like #RRGGBB")
    try:
        red = int(text[0:2], 16)
        green = int(text[2:4], 16)
        blue = int(text[4:6], 16)
    except ValueError:
        raise DeviceError("colour must look like #RRGGBB")
    return red, green, blue


def to_hex(red: int, green: int, blue: int) -> str:
    return "#%02x%02x%02x" % (max(0, min(255, int(red))), max(0, min(255, int(green))), max(0, min(255, int(blue))))


class RgbLedStrip(Driver):
    type = "rgb_led_strip"

    settable = ("power", "brightness", "color", "effect")

    def __init__(self, uid, config=None, *, name=None):
        super().__init__(uid, config, name=name)
        self.pins = (
            int(self.config.get("pin_r", 4)),
            int(self.config.get("pin_g", 5)),
            int(self.config.get("pin_b", 6)),
        )
        self.frequency = int(self.config.get("frequency_hz", 1000))
        gamma = max(1.0, min(3.0, float(self.config.get("gamma", 2.2))))
        self._table = gamma_lut(gamma, MAX_DUTY)
        self._channels = [None, None, None]
        self._effect_task = None
        self._rgb = (255, 215, 163)
        self.state = {"power": False, "brightness": 100, "color": "#ffd7a3", "effect": "solid"}

    # -- hardware --------------------------------------------------------- #
    def setup(self) -> None:
        self._channels = [
            make_pwm(pin, frequency=self.frequency, duty_u16=0) for pin in self.pins
        ]

    def teardown(self) -> None:
        if self._effect_task is not None:
            self._effect_task.cancel()
            self._effect_task = None
        for channel in self._channels:
            if channel is not None:
                try:
                    channel.duty_u16(0)
                    channel.deinit()
                except Exception:  # noqa: BLE001
                    pass
        self._channels = [None, None, None]

    def _write(self, red: int, green: int, blue: int) -> None:
        for channel, value in zip(self._channels, (red, green, blue)):
            if channel is None:
                continue
            try:
                channel.duty_u16(int(value))
            except (OSError, ValueError) as exc:
                raise DeviceError("PWM write failed: %s" % (exc,))

    def _scaled(self, red: int, green: int, blue: int):
        if not self.state.get("power"):
            return 0, 0, 0
        factor = max(0, min(100, int(self.state.get("brightness") or 0)))

        def scale(channel: int) -> int:
            # Channels are 0..255 and the gamma table is 0..100 %, so convert to
            # a percentage first, then apply the brightness factor.  Clamping
            # keeps stray values from indexing past the table.
            level = max(0, min(100, int(int(channel) * factor / 255)))
            return self._table[level]

        return scale(red), scale(green), scale(blue)

    def _render(self) -> None:
        self._write(*self._scaled(*self._rgb))

    # -- protocol --------------------------------------------------------- #
    def apply(self, props: dict) -> None:
        if "color" in props:
            self._rgb = parse_color(props.pop("color"))
            self.state["color"] = to_hex(*self._rgb)
        if "brightness" in props:
            brightness = int(props.pop("brightness"))
            if brightness < 0 or brightness > 100:
                raise DeviceError("brightness must be 0..100")
            self.state["brightness"] = brightness
        if "power" in props:
            self.state["power"] = bool(props.pop("power"))
        if "effect" in props:
            effect = str(props.pop("effect"))
            if effect not in ("solid", "breathe", "strobe", "rainbow"):
                raise DeviceError("unknown effect %r" % (effect,))
            self.state["effect"] = effect
        if props:
            raise DeviceError("unsupported properties %s" % (sorted(props),))

        if self._effect_task is not None:
            self._effect_task.cancel()
            self._effect_task = None
        effect = self.state.get("effect", "solid")
        if effect == "solid" or not self.state.get("power"):
            self._render()
            return
        self._effect_task = self.spawn(self._effect(effect))

    async def _effect(self, effect: str) -> None:
        try:
            step = 0
            while True:
                if effect == "strobe":
                    visible = step % 2 == 0
                    if visible:
                        self._render()
                    else:
                        self._write(0, 0, 0)
                    await sleep_ms(120)
                elif effect == "breathe":
                    level = (step % 40) / 40.0
                    if level > 0.5:
                        level = 1.0 - level
                    factor = int(level * 2 * 100)
                    red, green, blue = self._rgb
                    values = self._scaled(red, green, blue)
                    self._write(*(value * factor // 100 for value in values))
                    await sleep_ms(50)
                elif effect == "rainbow":
                    import colorsys

                    red, green, blue = colorsys.hsv_to_rgb((step % 360) / 360.0, 1.0, 1.0)
                    values = self._scaled(int(red * 255), int(green * 255), int(blue * 255))
                    self._write(*values)
                    await sleep_ms(30)
                step += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.log("effect error: %r" % (exc,))

    async def action_identify(self) -> None:
        was = (self.state["power"], self.state["brightness"])
        try:
            self.state["power"] = True
            for red, green, blue in ((255, 255, 255), (255, 0, 0), (0, 255, 0), (0, 0, 255)):
                self._write(*self._scaled(red, green, blue))
                await sleep_ms(200)
        finally:
            self.state["power"], self.state["brightness"] = was
            self._render()

    def snapshot(self) -> dict:
        return {
            "power": bool(self.state.get("power")),
            "brightness": int(self.state.get("brightness") or 0),
            "color": to_hex(*self._rgb),
            "effect": str(self.state.get("effect", "solid")),
        }
