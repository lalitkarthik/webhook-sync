"""The crash harness proves a crash happened: a crash test fails unless the first server exited
with the crash code (A15), so a test can't pass by crashing nowhere."""

import pytest
from crash.harness import CrashRun
from processes import stop

from webhook_sync.crashpoints import CRASH_EXIT_CODE

CAPTURE = "Tj9sMcl7hE9PaT"


async def test_a_crash_test_fails_when_the_server_did_not_exit_with_the_crash_code(
    crash: CrashRun,
) -> None:
    async with crash.server() as unarmed:
        [answered] = await crash.razorpay.deliver(unarmed.client, crash.razorpay.delivery(CAPTURE))
        with pytest.raises(pytest.fail.Exception, match="the crash point was never reached"):
            await unarmed.crashed(within=1)
        stop(unarmed.process)
        with pytest.raises(
            pytest.fail.Exception, match=f"not with the crash code {CRASH_EXIT_CODE}"
        ):
            await unarmed.crashed()

    assert answered.status_code == 202
