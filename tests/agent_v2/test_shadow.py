import pytest

from agent_core.runner import make_runtime

from agent_fakes import FakeLlm, FakeRepositories, make_dependencies, make_request


def test_retired_shadow_request_cannot_start_a_graph_run():
    request = make_request(shadow=True, question="previous shadow client")
    with pytest.raises(ValueError, match="shadow runs are retired"):
        make_runtime(request, make_dependencies(FakeLlm(), FakeRepositories(), embedding=False))
