from __future__ import annotations

import pytest

from harnesslab.adapters import DemoAdapter
from harnesslab.conformance import run_conformance
from harnesslab.runtime import HarnessRuntime
from harnesslab.tools import build_demo_registry


@pytest.mark.asyncio
async def test_demo_adapter_passes_reference_matrix() -> None:
    runtime = HarnessRuntime(build_demo_registry())
    runtime.register_adapter(DemoAdapter())

    report = await run_conformance(runtime)

    assert report.passed == report.total == 10
    assert {check.status for check in report.checks} == {"passed"}
