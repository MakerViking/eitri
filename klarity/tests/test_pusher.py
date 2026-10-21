import asyncio

from helpers import build_map, live_screen, ready_state
from klarity.pusher import Pusher, TokenBucket


async def test_first_push_sends_everything_then_nothing():
    st = ready_state()
    async with live_screen() as (screen, t):
        pusher = Pusher(screen, st, build_map(), min_interval=0.02)
        screen.set_page(1)
        n = await pusher.push_once()
        assert n == 5  # state, nozzle, bed, progress, pause visibility
        cmds = t.commands()
        assert 't_noz.txt="25 / 0"' in cmds
        assert 't_state.txt="Idle"' in cmds
        assert "j_prog.val=0" in cmds
        assert "vis b_pause,0" in cmds
        t.clear()
        assert await pusher.push_once() == 0 and not t.sent


async def test_only_changed_values_are_resent():
    st = ready_state()
    async with live_screen() as (screen, t):
        pusher = Pusher(screen, st, build_map(), min_interval=0.02)
        screen.set_page(1)
        await pusher.push_once()
        t.clear()
        st.apply({"extruder": {"temperature": 30.2}})
        assert await pusher.push_once() == 1
        assert t.commands() == ['t_noz.txt="30 / 0"']
        t.clear()
        st.apply({"extruder": {"temperature": 30.4}})  # same rendered text
        assert await pusher.push_once() == 0


async def test_page_change_repaints_and_other_pages_use_their_bindings():
    st = ready_state()
    async with live_screen() as (screen, t):
        pusher = Pusher(screen, st, build_map(), min_interval=0.02)
        screen.set_page(1)
        await pusher.push_once()
        t.clear()
        pusher.invalidate()
        assert await pusher.push_once() == 5
        t.clear()
        screen.set_page(2)
        pusher.invalidate()
        await pusher.push_once()
        assert t.commands() == ['t_z.txt="5.0"']


async def test_unknown_page_or_disconnected_pushes_nothing():
    st = ready_state()
    async with live_screen() as (screen, t):
        pusher = Pusher(screen, st, build_map(), min_interval=0.02)
        assert await pusher.push_once() == 0  # page unknown
        screen.set_page(77)
        assert await pusher.push_once() == 0
        assert not t.sent


async def test_run_loop_coalesces_bursts():
    st = ready_state()
    async with live_screen() as (screen, t):
        pusher = Pusher(screen, st, build_map(), min_interval=0.05, full_refresh=60)
        task = asyncio.create_task(pusher.run())
        try:
            screen.set_page(1)
            pusher.invalidate()
            await asyncio.sleep(0.15)
            t.clear()
            for temp in (31.0, 32.0, 33.0, 34.0):  # burst inside one interval
                st.apply({"extruder": {"temperature": temp}})
            await asyncio.sleep(0.2)
            assert t.commands() == ['t_noz.txt="34 / 0"']
        finally:
            task.cancel()


async def test_token_bucket_limits_rate():
    now = 0.0
    sleeps = []

    async def fake_sleep(s):
        nonlocal now
        sleeps.append(s)
        now += s

    bucket = TokenBucket(rate=10, burst=3, clock=lambda: now, sleep=fake_sleep)
    for _ in range(3):
        await bucket.acquire()  # burst is free
    assert not sleeps
    for _ in range(5):
        await bucket.acquire()
    assert round(sum(sleeps), 6) == 0.5  # 5 more tokens at 10/s
