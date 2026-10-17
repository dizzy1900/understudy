import asyncio
import os
import random

import pytest

from config import settings
from tests.fleet_scenarios import Env, check_outcome, run_all
from tests.fleet_support import TEST_MODEL, FakeUpstream, make_supervisor
from understudy.fleet import faults
from understudy.fleet.nodeconfig import ConfigError, canonical_config, dump_config, parse_config_text
from understudy.fleet.referee import Referee


def test_class_numbering_and_seed_ranges():
    assert faults.CLASS_NUMBERS["model_id_case"] == 1
    assert faults.CLASS_NUMBERS["leaky_worker"] == 10
    assert faults.CLASS_NUMBERS["log_permission"] == 11
    assert faults.CLASS_NUMBERS["upstream_storm"] == 12
    assert len(faults.TRAIN_CLASSES) == 10 and len(faults.HELDOUT_CLASSES) == 2
    assert faults.resolve_class(11) == "log_permission"
    assert faults.resolve_class("3") == "starved_tokens"
    assert faults.seed_kind(0) == faults.seed_kind(9999) == "train"
    assert faults.seed_kind(100_000) == "heldout"
    assert faults.seed_kind(50_000) == "other"
    rng = random.Random(1)
    assert all(faults.seed_kind(faults.random_seed("train", rng)) == "train" for _ in range(50))
    assert all(faults.seed_kind(faults.random_seed("heldout", rng)) == "heldout" for _ in range(50))
    with pytest.raises(faults.FaultError):
        faults.resolve_class("nope")
    with pytest.raises(faults.FaultError):
        faults.resolve_class(13)


def test_every_syntax_slip_fails_to_parse_and_reports_a_line():
    text = dump_config(canonical_config("n", 9101, "m/x"))
    kinds = set()
    for seed in range(200):
        broken, kind = faults._break_yaml(text, random.Random(seed))
        kinds.add(kind)
        with pytest.raises(ConfigError) as err:
            parse_config_text(broken)
        assert err.value.line >= 1
    assert len(kinds) >= 4


def test_fault_inputs_are_safe_and_effective():
    # The gateway accepts a bare "thinking" object, so it cannot be the injected parameter.
    assert "thinking" not in {name for name, _ in faults.FORBIDDEN_PARAMS}
    for url in faults.STALE_ENDPOINTS:
        host = url.split("//")[1].split("/")[0]
        assert host == "api.inference.crusoecloud.com" or host.endswith(".invalid")


@pytest.fixture(scope="module")
def offline_outcomes(tmp_path_factory):
    os.environ["CRUSOE_API_KEY"] = "test-key"
    mp = pytest.MonkeyPatch()
    mp.setattr(faults, "STALE_ENDPOINTS", tuple(u for u in faults.STALE_ENDPOINTS if u.split("//")[1].split("/")[0].endswith(".invalid")))
    upstream = FakeUpstream().start()
    try:
        tmp = tmp_path_factory.mktemp("faults")
        sup = make_supervisor(tmp, 12, upstream.base_url)
        env = Env(sup, Referee(sup.port_of, model=TEST_MODEL, wait_s=1.0), upstream.base_url, TEST_MODEL)
        yield asyncio.run(run_all(env, sup.node_ids()))
    finally:
        upstream.stop()
        mp.undo()


@pytest.mark.parametrize("fault_class", faults.ALL_CLASSES)
def test_inject_fails_verify_and_known_fix_passes(offline_outcomes, fault_class):
    if fault_class == "log_permission" and os.geteuid() == 0:
        pytest.skip("chmod does not restrict root")
    check_outcome(offline_outcomes[fault_class])
