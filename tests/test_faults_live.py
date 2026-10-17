"""All 12 fault classes against the real Crusoe API through real node processes (NANO, thinking off)."""
import asyncio
import os

import pytest

from config import settings
from tests.fleet_scenarios import Env, check_outcome, run_all
from tests.fleet_support import make_supervisor
from understudy.fleet import faults
from understudy.fleet.referee import Referee

pytestmark = pytest.mark.live

# Captured at import so a test that edits the environment cannot swap the key out from under these nodes.
REAL_KEY = os.environ.get(settings.API_KEY_ENV)
if not REAL_KEY or os.environ.get("SKIP_LIVE") == "1":
    pytest.skip("needs CRUSOE_API_KEY (and SKIP_LIVE unset)", allow_module_level=True)


@pytest.fixture(scope="module")
def live_outcomes(tmp_path_factory):
    os.environ[settings.API_KEY_ENV] = REAL_KEY
    model = settings.fleet_model_id()
    tmp = tmp_path_factory.mktemp("live-faults")
    sup = make_supervisor(tmp, 12, settings.CRUSOE_BASE_URL, model=model, prefix="live")
    env = Env(sup, Referee(sup.port_of, model=model), settings.CRUSOE_BASE_URL, model)
    return asyncio.run(run_all(env, sup.node_ids()))


@pytest.mark.parametrize("fault_class", faults.ALL_CLASSES)
def test_live_inject_fails_verify_and_known_fix_passes(live_outcomes, fault_class):
    if fault_class == "log_permission" and os.geteuid() == 0:
        pytest.skip("chmod does not restrict root")
    check_outcome(live_outcomes[fault_class])
