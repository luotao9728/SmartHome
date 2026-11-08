"""Device base class.

A device type is a subclass of :class:`Device` plus a JSON manifest in the hub's
``device_types/`` directory.  The base class handles all protocol plumbing --
state, acking, reporting, reconciliation -- so a driver only implements what is
physically different::

    class Relay(Device):
        type = "relay"                  # manifest id
        def setup(self):   ...
        def apply(self, props):   ...   # optionally raise DeviceError
        def teardown(self):  ...
        async def loop(self):    ...    # optional background work

Anything a subclass raises as :class:`DeviceError` becomes a negative ack, which
the hub surfaces in the UI instead of silently doing nothing.
"""

from .compat import asyncio, create_task


class DeviceError(Exception):
    """Raised by a driver when a command cannot be applied."""


class Unavailable(DeviceError):
    """Raised when the hardware is missing (sensor not wired, pin busy, ...)."""


class Device:
    """Base class for every controllable thing on a node."""

    #: manifest id in ``device_types/``; must be set by subclasses
    type = ""
    #: how often the device re-reports its state (seconds); 0 disables
    report_interval = 0.0
    #: how often ``loop()`` runs (seconds); 0 disables the background task
    poll_interval = 0.0

    def __init__(self, uid: str, config=None, *, name=None):
        if not uid:
            raise ValueError("a device needs a uid")
        if not self.type:
            raise ValueError("%s must define a type id" % (type(self).__name__,))
        self.uid = uid
        self.config = dict(config or {})
        self.name = name or uid
        self.state = {}
        self._dirty = False
        self._agent = None
        self._task = None
        self._running = False

    # -- driver hooks (override these) ------------------------------------ #
    def setup(self) -> None:
        """Initialise hardware.  Called once when the device starts."""

    def teardown(self) -> None:
        """Release hardware.  Must not raise."""

    def apply(self, props: dict) -> None:
        """Apply validated, settable properties.

        Raise :class:`DeviceError` to reject the command.  Update ``self.state``
        with what the hardware actually does (which may differ from the
        request).
        """

    async def loop(self) -> None:
        """Background work, run every ``poll_interval`` seconds."""

    def describe(self) -> dict:
        """Extra fields sent with ``hello`` for this device."""
        return {"name": self.name, "config": self.config}

    # -- framework -------------------------------------------------------- #
    def attach(self, agent) -> None:
        self._agent = agent

    def log(self, message: str) -> None:
        """Log through the agent when attached, otherwise to stdout.

        Drivers can then be unit tested (and exercised under a bare
        MicroPython build) without a live agent.
        """
        if self._agent is not None:
            self._agent.log("%s: %s" % (self.uid, message))
        else:
            try:
                print("[%s] %s" % (self.uid, message))
            except Exception:  # noqa: BLE001
                pass

    def spawn(self, coro):
        """Run a background task, tracked by the agent when available."""
        try:
            task = create_task(coro)
        except RuntimeError:
            # No event loop: close the coroutine so nothing leaks, then let the
            # caller see the failure.
            close = getattr(coro, "close", None)
            if close is not None:
                close()
            raise
        if self._agent is not None:
            self._agent.track(task)
        return task

    def request_report(self) -> None:
        if self._agent is not None:
            self._agent.request_report(self)

    async def emit(self, name: str, data=None) -> None:
        """Raise a protocol event (ignored when no agent is attached)."""
        if self._agent is not None:
            await self._agent.event(self, name, data)

    def set_state(self, values: dict, *, report: bool = True) -> None:
        """Merge values into the reported state and notify the hub."""
        changed = False
        for key, value in values.items():
            if key not in self.state or self.state[key] != value:
                self.state[key] = value
                changed = True
        if changed:
            self._dirty = True
            if report:
                self.request_report()

    def snapshot(self) -> dict:
        return dict(self.state)

    # -- lifecycle -------------------------------------------------------- #
    async def start(self) -> None:
        self._running = True
        try:
            self.setup()
        except DeviceError as exc:
            self.log("unavailable: %s" % (exc,))
            self.state["_error"] = str(exc)
        if self.poll_interval and type(self).loop is not Device.loop:
            self._task = self.spawn(self._run_loop())

    async def _run_loop(self) -> None:
        interval = max(0.05, float(self.poll_interval))
        while self._running:
            try:
                await self.loop()
            except DeviceError as exc:
                self.log("loop: %s" % (exc,))
            except Exception as exc:  # noqa: BLE001 - never kill the node
                if _is_cancelled(exc):
                    raise
                self.log("loop crashed: %r" % (exc,))
            await asyncio.sleep(interval)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            self._task = None
        try:
            self.teardown()
        except Exception:  # noqa: BLE001
            pass


def _is_cancelled(exc: BaseException) -> bool:
    return type(exc).__name__ in ("CancelledError", "GeneratorExit")


class DeviceGroup(Device):
    """A device that owns several child devices (e.g. a 4-channel relay board)."""

    def __init__(self, uid: str, config=None, *, name=None):
        super().__init__(uid, config, name=name)
        self.children = []

    def add_child(self, device: Device) -> None:
        self.children.append(device)

    async def start(self) -> None:
        await super().start()
        for child in self.children:
            await child.start()

    async def stop(self) -> None:
        for child in self.children:
            await child.stop()
        await super().stop()
