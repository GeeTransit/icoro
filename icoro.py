from collections import deque
from types import coroutine



class IBase:

    @classmethod
    def _check_actions(cls, actions, locals_) -> None:
        if not isinstance(actions, dict):
            raise TypeError("'actions' is not an instance of 'dict'")
        for name in cls._reserved:
            if name in actions:
                raise ValueError(f"{name!r} is in 'actions'")
            if name not in locals_:
                raise ValueError(f"{name!r} is not in 'locals_'")
            actions[name] = locals_[name]

    @staticmethod
    def _action_adder(actions):
        def _adder(func, name=None):
            if name is None:
                name = func.__name__
            actions[name] = func
            return func
        return _adder

    @staticmethod
    def _action_getter(actions):
        async def _getter(name, args, kwargs):
            return await actions[name](*args, **kwargs)
        return _getter

    @staticmethod
    @coroutine
    def _icall(name, *args, **kwargs):
        # Yield a call request to the intercept of this coroutine
        return (yield (name, args, kwargs))

    @staticmethod
    @coroutine
    def _suspend(data):
        # Yield 'data' to the runner of this coroutine
        return (yield data)

    @classmethod
    async def suspend(cls, data=None):
        """
        Internal function.

        This should only be used by subclasses.
        Suspend with 'data' to the coroutine intercept.
        """
        return await cls._suspend(data)



class IStack(IBase):
    """
    A suspendable coroutine runner with pluggable actions.

    A single coroutine will be run with a stack that contains the
    running coroutines. They can append to the stack to wait for it
    with a lower stack size to overcome the recursion limit.
    """

    _reserved = ("stack_add",)

    @classmethod
    async def run(cls, coro, *, actions):
        """
        Run 'coro' with the supplied 'actions'.

        This will get the requested action and run the action directly.
        The action can yield, meaning this can be used above another
        event loop.
        """

        async def stack_add(coro):
            stack.append(coro)

        cls._check_actions(actions, locals())

        get_action = cls._action_getter(actions)
        stack = [coro]
        val = None
        exc = None

        while True:
            try:
                if exc:
                    req = stack[-1].throw(exc)
                else:
                    req = stack[-1].send(val)
                try:
                    val = await get_action(*req)
                    exc = None
                except Exception as e:
                    val = None
                    exc = e

            except BaseException as e:
                if len(stack) > 1:
                    del stack[-1]
                    if isinstance(e, StopIteration):
                        val, exc = e.value, None
                    else:
                        val, exc = None, e
                else:
                    if isinstance(e, StopIteration):
                        return e.value
                    else:
                        raise

    @classmethod
    async def stack_add(cls, coro):
        """Add 'coro' to the stack and await for its result."""
        return await cls._icall("stack_add", coro)



class IRotator(IBase):
    """
    A suspendable coroutine rotator with pluggable actions.

    Multiple coroutines can be run cooperatively using dictionaries
    containing their info. They can suspend for other coroutines,
    create a new coroutine that will be run with it, and check on
    other coroutines' info.
    """

    _reserved = (
        "new_coro",
        "wait_coro",
        "this_id",
        "this_info",
        "all_info",
        "schedule",
    )

    @classmethod
    async def run(cls, coro, *, actions):
        """
        Run 'coro' with the supplied 'actions'.

        This will get the requested action and run the action directly.
        The action can yield, meaning this can be used above another
        event loop.
        """
        def _suspend():
            cinfo["active"] = False

        def _pull_active():
            nonlocal active
            active = False

        def _check_info(info):
            if not isinstance(info, dict):
                raise TypeError("Coro info not a dict")
            if "coro" not in info:
                raise ValueError("'coro' not in coro info")
            info_setdefault = info.setdefault
            info_setdefault("val", None)
            info_setdefault("exc", None)
            info_setdefault("active", True)
            info_setdefault("terminated", False)
            info_setdefault("waiting_coros", [])

        def _new_coro_adder():
            id_ = 0
            info = None
            while True:
                coro = yield info
                coro_info[id_] = info = {
                    "val": None,
                    "exc": None,
                    "coro": coro,
                    "active": True,
                    "terminated": False,
                    "waiting_coros": []
                }
                id_ += 1

        async def new_coro(coro, /):
            return coro_adder.send(coro)

        async def wait_coro(info, /):
            if not info["terminated"]:
                info["waiting_coros"].append(cinfo)
                _suspend()

        async def this_id():
            return cid

        async def this_info():
            return cinfo

        async def all_info():
            return coro_info

        async def schedule():
            _pull_active()

        cls._check_actions(actions, locals())
        get_action = cls._action_getter(actions)

        coro_adder = _new_coro_adder()
        coro_adder.send(None)
        coro_info = {}
        main_info = coro_adder.send(coro)

        wait_dict = {}

        while not main_info["terminated"]:
            for cid, cinfo in list(reversed(coro_info.items())):
                try:
                    _check_info(cinfo)
                    active = cinfo["active"]
                    while active and cinfo["active"]:
                        if exc := cinfo["exc"]:
                            req = cinfo["coro"].throw(exc)
                        else:
                            req = cinfo["coro"].send(cinfo["val"])
                        try:
                            cinfo["val"] = await get_action(*req)
                            cinfo["exc"] = None
                        except Exception as e:
                            cinfo["val"] = None
                            cinfo["exc"] = e

                except BaseException as e:
                    cinfo["active"] = False
                    cinfo["terminated"] = True
                    if isinstance(e, StopIteration):
                        cinfo["val"], cinfo["exc"] = e.value, None
                    else:
                        cinfo["val"], cinfo["exc"] = None, e
                    for info in cinfo["waiting_coros"]:
                        if not info["terminated"]:
                            info["active"] = True
                            info["val"] = None
                            info["exc"] = None
                    del coro_info[cid]
                    if not isinstance(e, Exception):
                        raise

        return cls.get_result(main_info)

    @staticmethod
    def get_result(info, /):
        """Return the 'val' of 'info', or raise 'exc' of 'info'."""
        if exc := info.get("exc", False):
            raise exc
        return info.get("val")

    @classmethod
    async def new_coro(cls, coro, /):
        """Add 'coro' to the coro infos and return its info."""
        return await cls._icall("new_coro", coro)

    @classmethod
    async def wait_coro(cls, info, /):
        """Suspend until 'info' is terminated."""
        return await cls._icall("wait_coro", info)

    @classmethod
    async def this_id(cls, /):
        """Get the current coro's id."""
        return await cls._icall("this_id")

    @classmethod
    async def this_info(cls, /):
        """Get the current coro's info."""
        return await cls._icall("this_info")

    @classmethod
    async def all_info(cls, /):
        """Get a dict of all running coros and their info."""
        return await cls._icall("all_info")

    @classmethod
    async def schedule(cls, /):
        """Suspend for a cycle."""
        await cls._icall("schedule")
