import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import anyio
import anyio.abc
import coolname
import pydantic
import pytest
from typing_extensions import Literal

import prefect
from prefect.flow_runners import (
    DockerFlowRunner,
    FlowRunner,
    SubprocessFlowRunner,
    UniversalFlowRunner,
    lookup_flow_runner,
    register_flow_runner,
    PYTHON_VERSION_STRING,
)
from prefect.orion.schemas.core import FlowRunnerSettings
from prefect.orion.schemas.data import DataDocument
from prefect.utilities.compat import AsyncMock


@contextmanager
def temporary_settings(**kwargs):
    """
    Temporarily override a setting in `prefect.settings`

    Example:
        >>> from prefect import settings
        >>> with temporary_settings(PREFECT_ORION_HOST="foo"):
        >>>    assert settings.orion_host == "foo"
        >>> assert settings.orion_host is None
    """
    old_env = os.environ.copy()
    for setting in kwargs:
        os.environ[setting] = kwargs[setting]

    from prefect import settings
    from prefect.utilities.settings import Settings

    new_settings = Settings()
    old_settings = settings.copy()

    for field in settings.__fields__:
        object.__setattr__(settings, field, getattr(new_settings, field))

    try:
        yield settings
    finally:
        for setting in kwargs:
            if old_env.get(setting):
                os.environ[setting] = old_env[setting]
            else:
                os.environ.pop(setting)

        for field in settings.__fields__:
            object.__setattr__(settings, field, getattr(old_settings, field))


@pytest.fixture
def venv_environment_path(tmp_path):
    """
    Generates a temporary venv environment with development dependencies installed
    """

    environment_path = tmp_path / "test"

    # Create the virtual environment
    subprocess.check_output([sys.executable, "-m", "venv", str(environment_path)])

    # Install prefect within the virtual environment
    subprocess.check_output(
        [
            str(environment_path / "bin" / "python"),
            "-m",
            "pip",
            "install",
            "-e",
            f"{prefect.__root_path__}[dev]",
        ]
    )

    return environment_path


@pytest.fixture
def virtualenv_environment_path(tmp_path):
    """
    Generates a temporary virtualenv environment with development dependencies installed
    """
    pytest.importorskip("virtualenv")

    environment_path = tmp_path / "test"

    # Create the virtual environment
    subprocess.check_output(["virtualenv", str(environment_path)])

    # Install prefect within the virtual environment
    subprocess.check_output(
        [
            str(environment_path / "bin" / "python"),
            "-m",
            "pip",
            "install",
            "-e",
            f"{prefect.__root_path__}[dev]",
        ]
    )

    return environment_path


@pytest.fixture
def conda_environment_path(tmp_path):
    """
    Generates a temporary anaconda environment with development dependencies installed

    Will not be usable by `--name`, only `--prefix`.
    """
    if not shutil.which("conda"):
        pytest.skip("`conda` is not installed.")

    environment_path = tmp_path / f"test-{coolname.generate_slug(2)}"

    # Create the conda environment with a matching python version up to `minor`
    # We cannot match up to `micro` because it is not always available in conda
    v = sys.version_info
    python_version = f"{v.major}.{v.minor}"
    subprocess.check_output(
        [
            "conda",
            "create",
            "-y",
            "--prefix",
            str(environment_path),
            f"python={python_version}",
        ]
    )

    # Install prefect within the virtual environment
    subprocess.check_output(
        [
            "conda",
            "run",
            "--prefix",
            str(environment_path),
            "pip",
            "install",
            "-e",
            f"{prefect.__root_path__}[dev]",
        ]
    )

    return environment_path


@pytest.fixture
async def python_executable_test_deployment(orion_client):
    """
    A deployment for a flow that returns the current python executable path for
    testing that flows are run with the correct python version
    """

    @prefect.flow
    def my_flow():
        import sys

        return sys.executable

    flow_id = await orion_client.create_flow(my_flow)

    flow_data = DataDocument.encode("cloudpickle", my_flow)

    deployment_id = await orion_client.create_deployment(
        flow_id=flow_id,
        name="python_executable_test_deployment",
        flow_data=flow_data,
    )

    return deployment_id


@pytest.fixture
async def os_environ_test_deployment(orion_client):
    """
    A deployment for a flow that returns the current environment variables for testing
    that flows are run with environment variables populated.
    """

    @prefect.flow
    def my_flow():
        import os

        return os.environ

    flow_id = await orion_client.create_flow(my_flow)

    flow_data = DataDocument.encode("cloudpickle", my_flow)

    deployment_id = await orion_client.create_deployment(
        flow_id=flow_id,
        name="os_environ_test_deployment",
        flow_data=flow_data,
    )

    return deployment_id


@pytest.fixture
async def prefect_settings_test_deployment(orion_client):
    """
    A deployment for a flow that returns the current prefect settings object
    """

    @prefect.flow
    def my_flow():
        import prefect

        return prefect.settings

    flow_id = await orion_client.create_flow(my_flow)

    flow_data = DataDocument.encode("cloudpickle", my_flow)

    deployment_id = await orion_client.create_deployment(
        flow_id=flow_id,
        name="prefect_settings_test_deployment",
        flow_data=flow_data,
    )

    return deployment_id


class TestFlowRunner:
    def test_has_no_type(self):
        with pytest.raises(pydantic.ValidationError):
            FlowRunner()

    async def test_does_not_implement_submission(self):
        with pytest.raises(NotImplementedError):
            await FlowRunner(typename="test").submit_flow_run(None, None)

    def test_logger_based_on_name(self):
        assert FlowRunner(typename="foobar").logger.name == "prefect.flow_runner.foobar"


class TestFlowRunnerRegistration:
    def test_register_and_lookup(self):
        @register_flow_runner
        class TestFlowRunnerConfig(FlowRunner):
            typename: Literal["test"] = "test"

        assert lookup_flow_runner("test") == TestFlowRunnerConfig

    def test_to_settings(self):
        flow_runner = UniversalFlowRunner(env={"foo": "bar"})
        assert flow_runner.to_settings() == FlowRunnerSettings(
            type="universal", config={"env": {"foo": "bar"}}
        )

    def test_from_settings(self):
        settings = FlowRunnerSettings(type="universal", config={"env": {"foo": "bar"}})
        assert FlowRunner.from_settings(settings) == UniversalFlowRunner(
            env={"foo": "bar"}
        )


class TestUniversalFlowRunner:
    def test_unner_type(self):
        assert UniversalFlowRunner().typename == "universal"

    async def test_raises_submission_error(self):
        with pytest.raises(
            RuntimeError,
            match="universal flow runner cannot be used to submit flow runs",
        ):
            await UniversalFlowRunner().submit_flow_run(None, None)


class TestSubprocessFlowRunner:
    def test_runner_type(self):
        assert SubprocessFlowRunner().typename == "subprocess"

    async def test_creates_subprocess_then_marks_as_started(
        self, monkeypatch, flow_run
    ):
        monkeypatch.setattr("anyio.open_process", AsyncMock())
        fake_status = MagicMock(spec=anyio.abc.TaskStatus)
        # By raising an exception when started is called we can assert the process
        # is opened before this time
        fake_status.started.side_effect = RuntimeError("Started called!")

        with pytest.raises(RuntimeError, match="Started called!"):
            await SubprocessFlowRunner().submit_flow_run(flow_run, fake_status)

        fake_status.started.assert_called_once()
        anyio.open_process.assert_awaited_once()

    async def test_creates_subprocess_with_current_python_executable(
        self, monkeypatch, flow_run
    ):
        monkeypatch.setattr(
            "anyio.open_process",
            # TODO: Consider more robust mocking for opened processes
            AsyncMock(side_effect=RuntimeError("Exit without streaming from process.")),
        )
        with pytest.raises(RuntimeError, match="Exit without streaming"):
            await SubprocessFlowRunner().submit_flow_run(flow_run, MagicMock())

        anyio.open_process.assert_awaited_once_with(
            [sys.executable, "-m", "prefect.engine", flow_run.id.hex],
            stderr=subprocess.STDOUT,
            env=os.environ,
        )

    @pytest.mark.parametrize(
        "condaenv",
        ["test", Path("/test"), Path("~/test")],
        ids=["by name", "by abs path", "by home path"],
    )
    async def test_creates_subprocess_with_conda_command(
        self, monkeypatch, flow_run, condaenv
    ):
        monkeypatch.setattr(
            "anyio.open_process",
            # TODO: Consider more robust mocking for opened processes
            AsyncMock(side_effect=RuntimeError("Exit without streaming from process.")),
        )

        with pytest.raises(RuntimeError, match="Exit without streaming"):
            await SubprocessFlowRunner(condaenv=condaenv).submit_flow_run(
                flow_run, MagicMock()
            )

        name_or_prefix = (
            ["--name", condaenv]
            if not isinstance(condaenv, Path)
            else ["--prefix", str(condaenv.expanduser().resolve())]
        )

        anyio.open_process.assert_awaited_once_with(
            [
                "conda",
                "run",
                *name_or_prefix,
                "python",
                "-m",
                "prefect.engine",
                flow_run.id.hex,
            ],
            stderr=subprocess.STDOUT,
            env=os.environ,
        )

    async def test_creates_subprocess_with_virtualenv_command_and_env(
        self, monkeypatch, flow_run
    ):
        # PYTHONHOME must be unset in the subprocess
        monkeypatch.setenv("PYTHONHOME", "FOO")

        monkeypatch.setattr(
            "anyio.open_process",
            # TODO: Consider more robust mocking for opened processes
            AsyncMock(side_effect=RuntimeError("Exit without streaming from process.")),
        )
        with pytest.raises(RuntimeError, match="Exit without streaming"):
            await SubprocessFlowRunner(virtualenv="~/fakevenv").submit_flow_run(
                flow_run, MagicMock()
            )

        # Replicate expected generation of virtual environment call
        virtualenv_path = Path("~/fakevenv").expanduser()
        python_executable = str(virtualenv_path / "bin" / "python")
        expected_env = os.environ.copy()
        expected_env["PATH"] = (
            str(virtualenv_path / "bin") + os.pathsep + expected_env["PATH"]
        )
        expected_env.pop("PYTHONHOME")
        expected_env["VIRTUAL_ENV"] = str(virtualenv_path)

        anyio.open_process.assert_awaited_once_with(
            [
                python_executable,
                "-m",
                "prefect.engine",
                flow_run.id.hex,
            ],
            stderr=subprocess.STDOUT,
            env=expected_env,
        )

    async def test_executes_flow_run_with_system_python(
        self, python_executable_test_deployment, orion_client
    ):
        fake_status = MagicMock(spec=anyio.abc.TaskStatus)

        flow_run = await orion_client.create_flow_run_from_deployment(
            python_executable_test_deployment
        )

        happy_exit = await SubprocessFlowRunner().submit_flow_run(flow_run, fake_status)

        assert happy_exit
        fake_status.started.assert_called_once()
        state = (await orion_client.read_flow_run(flow_run.id)).state
        runtime_python = await orion_client.resolve_datadoc(state.result())
        assert runtime_python == sys.executable

    @pytest.mark.service("environment")
    async def test_executes_flow_run_in_virtualenv(
        self,
        flow_run,
        orion_client,
        virtualenv_environment_path,
        python_executable_test_deployment,
    ):
        flow_run = await orion_client.create_flow_run_from_deployment(
            python_executable_test_deployment
        )

        happy_exit = await SubprocessFlowRunner(
            virtualenv=virtualenv_environment_path
        ).submit_flow_run(flow_run, MagicMock(spec=anyio.abc.TaskStatus))

        assert happy_exit
        state = (await orion_client.read_flow_run(flow_run.id)).state
        runtime_python = await orion_client.resolve_datadoc(state.result())
        assert runtime_python == str(virtualenv_environment_path / "bin" / "python")

    @pytest.mark.service("environment")
    async def test_executes_flow_run_in_venv(
        self,
        flow_run,
        orion_client,
        venv_environment_path,
        python_executable_test_deployment,
    ):
        flow_run = await orion_client.create_flow_run_from_deployment(
            python_executable_test_deployment
        )

        happy_exit = await SubprocessFlowRunner(
            virtualenv=venv_environment_path
        ).submit_flow_run(flow_run, MagicMock(spec=anyio.abc.TaskStatus))

        assert happy_exit
        state = (await orion_client.read_flow_run(flow_run.id)).state
        runtime_python = await orion_client.resolve_datadoc(state.result())
        assert runtime_python == str(venv_environment_path / "bin" / "python")

    @pytest.mark.service("environment")
    async def test_executes_flow_run_in_conda_environment(
        self,
        flow_run,
        orion_client,
        conda_environment_path,
        python_executable_test_deployment,
    ):
        flow_run = await orion_client.create_flow_run_from_deployment(
            python_executable_test_deployment
        )

        happy_exit = await SubprocessFlowRunner(
            condaenv=conda_environment_path,
            stream_output=True,
        ).submit_flow_run(flow_run, MagicMock(spec=anyio.abc.TaskStatus))

        assert happy_exit
        state = (await orion_client.read_flow_run(flow_run.id)).state
        runtime_python = await orion_client.resolve_datadoc(state.result())
        assert runtime_python == str(conda_environment_path / "bin" / "python")

    @pytest.mark.parametrize("stream_output", [True, False])
    async def test_stream_output_controls_local_printing(
        self, deployment, capsys, orion_client, stream_output
    ):
        flow_run = await orion_client.create_flow_run_from_deployment(deployment.id)

        assert await SubprocessFlowRunner(stream_output=stream_output).submit_flow_run(
            flow_run, MagicMock(spec=anyio.abc.TaskStatus)
        )

        output = capsys.readouterr()
        assert output.err == "", "stderr is never populated"

        if not stream_output:
            assert output.out == ""
        else:
            assert "Beginning flow run" in output.out, "Log from the engine is present"
            assert "\n\n" not in output.out, "Line endings are not double terminated"


class TestDockerFlowRunner:
    @pytest.fixture(autouse=True)
    def skip_if_docker_is_not_installed(self):
        pytest.importorskip("docker")

    @pytest.fixture
    def mock_docker_client(self, monkeypatch):
        docker = pytest.importorskip("docker")

        mock = MagicMock(spec=docker.DockerClient)
        mock.version.return_value = {"Version": "20.10"}

        monkeypatch.setattr(
            "prefect.flow_runners.DockerFlowRunner._get_client",
            MagicMock(return_value=mock),
        )
        return mock

    def test_runner_type(self):
        assert DockerFlowRunner().typename == "docker"

    async def test_creates_container_then_marks_as_started(
        self, flow_run, mock_docker_client, hosted_orion
    ):
        fake_status = MagicMock(spec=anyio.abc.TaskStatus)
        # By raising an exception when started is called we can assert the process
        # is opened before this time
        fake_status.started.side_effect = RuntimeError("Started called!")

        with pytest.raises(RuntimeError, match="Started called!"):
            with temporary_settings(PREFECT_ORION_HOST=hosted_orion):
                await DockerFlowRunner().submit_flow_run(flow_run, fake_status)

        fake_status.started.assert_called_once()
        mock_docker_client.containers.create.assert_called_once()
        # The returned container is started
        mock_docker_client.containers.create().start.assert_called_once()

    async def test_container_name_matches_flow_run_name(
        self, mock_docker_client, flow_run, hosted_orion
    ):
        flow_run.name = "hello-flow-run"
        with temporary_settings(PREFECT_ORION_HOST=hosted_orion):
            await DockerFlowRunner().submit_flow_run(flow_run, MagicMock())
        mock_docker_client.containers.create.assert_called_once()
        call_name = mock_docker_client.containers.create.call_args[1].get("name")
        assert call_name == "hello-flow-run"

    @pytest.mark.parametrize(
        "run_name,container_name",
        [
            ("_flow_run", "flow_run"),
            ("...flow_run", "flow_run"),
            ("._-flow_run", "flow_run"),
            ("9flow-run", "9flow-run"),
            ("-flow.run", "flow.run"),
            ("flow*run", "flow-run"),
            ("flow9.-foo_bar^x", "flow9.-foo_bar-x"),
        ],
    )
    async def test_container_name_creates_valid_name(
        self, mock_docker_client, flow_run, hosted_orion, run_name, container_name
    ):
        flow_run.name = run_name
        with temporary_settings(PREFECT_ORION_HOST=hosted_orion):
            await DockerFlowRunner().submit_flow_run(flow_run, MagicMock())
        mock_docker_client.containers.create.assert_called_once()
        call_name = mock_docker_client.containers.create.call_args[1].get("name")
        assert call_name == container_name

    async def test_container_name_falls_back_to_id(
        self, mock_docker_client, flow_run, hosted_orion
    ):
        flow_run.name = "--__...."  # All invalid characters
        with temporary_settings(PREFECT_ORION_HOST=hosted_orion):
            await DockerFlowRunner().submit_flow_run(flow_run, MagicMock())
        mock_docker_client.containers.create.assert_called_once()
        call_name = mock_docker_client.containers.create.call_args[1].get("name")
        assert call_name == flow_run.id

    @pytest.mark.parametrize("collision_count", (0, 1, 5))
    async def test_container_name_includes_index_on_conflict(
        self, mock_docker_client, flow_run, hosted_orion, collision_count
    ):
        import docker.errors

        flow_run.name = "flow-run-name"

        if collision_count:
            # Add the basic name first
            existing_names = [f"{flow_run.name}"]
            for i in range(1, collision_count):
                existing_names.append(f"{flow_run.name}-{i}")
        else:
            existing_names = []

        def fail_if_name_exists(*args, **kwargs):
            if kwargs.get("name") in existing_names:
                raise docker.errors.APIError(
                    "Conflict. The container name 'foobar' is already in use"
                )
            return MagicMock()  # A container

        mock_docker_client.containers.create.side_effect = fail_if_name_exists

        with temporary_settings(PREFECT_ORION_HOST=hosted_orion):
            await DockerFlowRunner().submit_flow_run(flow_run, MagicMock())

        assert mock_docker_client.containers.create.call_count == collision_count + 1
        call_name = mock_docker_client.containers.create.call_args[1].get("name")
        expected_name = (
            f"{flow_run.name}"
            if not collision_count
            else f"{flow_run.name}-{collision_count}"
        )
        assert call_name == expected_name

    async def test_container_creation_failure_reraises_if_not_name_conflict(
        self, mock_docker_client, flow_run, hosted_orion
    ):
        import docker.errors

        mock_docker_client.containers.create.side_effect = docker.errors.APIError(
            "test error"
        )

        with temporary_settings(PREFECT_ORION_HOST=hosted_orion):
            with pytest.raises(docker.errors.APIError, match="test error"):
                await DockerFlowRunner().submit_flow_run(flow_run, MagicMock())

    async def test_builds_development_image_if_none_specified_and_does_not_exist(
        self, flow_run, mock_docker_client, hosted_orion, monkeypatch
    ):
        import docker.errors

        monkeypatch.setattr(
            "prefect.flow_runners.DockerFlowRunner._get_orion_image_tag",
            MagicMock(return_value="dev-image-tag"),
        )

        mock_docker_client.images.get = MagicMock(
            side_effect=docker.errors.ImageNotFound("")
        )

        with temporary_settings(PREFECT_ORION_HOST=hosted_orion):
            await DockerFlowRunner().submit_flow_run(flow_run, MagicMock())

        mock_docker_client.images.get.assert_called_once_with("dev-image-tag")
        mock_docker_client.images.build.assert_called_once_with(
            path=str(prefect.__root_path__),
            tag="dev-image-tag",
            buildargs={"PYTHON_VERSION": PYTHON_VERSION_STRING},
        )

    async def test_skips_image_build_if_exists_already(
        self, flow_run, mock_docker_client, hosted_orion, monkeypatch
    ):
        monkeypatch.setattr(
            "prefect.flow_runners.DockerFlowRunner._get_orion_image_tag",
            MagicMock(return_value="dev-image-tag"),
        )

        with temporary_settings(PREFECT_ORION_HOST=hosted_orion):
            await DockerFlowRunner().submit_flow_run(flow_run, MagicMock())

        mock_docker_client.images.get.assert_called_once_with("dev-image-tag")
        mock_docker_client.images.build.assert_not_called()
        call_image = mock_docker_client.containers.create.call_args[1].get("image")
        assert call_image == "dev-image-tag"

    async def test_skips_image_build_if_exists_already(
        self, flow_run, mock_docker_client, hosted_orion, monkeypatch
    ):
        monkeypatch.setattr(
            "prefect.flow_runners.DockerFlowRunner._get_orion_image_tag",
            MagicMock(return_value="dev-image-tag"),
        )

        with temporary_settings(PREFECT_ORION_HOST=hosted_orion):
            await DockerFlowRunner().submit_flow_run(flow_run, MagicMock())

        mock_docker_client.images.get.assert_called_once_with("dev-image-tag")
        mock_docker_client.images.build.assert_not_called()
        _, kwargs = mock_docker_client.containers.create.call_args
        assert kwargs["image"] == "dev-image-tag"

    async def test_uses_image_setting(self, mock_docker_client, flow_run, hosted_orion):
        with temporary_settings(PREFECT_ORION_HOST=hosted_orion):
            await DockerFlowRunner(image="foo").submit_flow_run(flow_run, MagicMock())
        mock_docker_client.containers.create.assert_called_once()
        call_image = mock_docker_client.containers.create.call_args[1].get("image")
        assert call_image == "foo"

    async def test_uses_volumes_setting(
        self, mock_docker_client, flow_run, hosted_orion
    ):
        with temporary_settings(PREFECT_ORION_HOST=hosted_orion):
            await DockerFlowRunner(volumes=["a:b", "c:d"]).submit_flow_run(
                flow_run, MagicMock()
            )
        mock_docker_client.containers.create.assert_called_once()
        call_volumes = mock_docker_client.containers.create.call_args[1].get("volumes")
        assert "a:b" in call_volumes
        assert "c:d" in call_volumes

    @pytest.mark.parametrize("networks", [[], ["a"], ["a", "b"]])
    async def test_uses_network_setting(
        self, mock_docker_client, flow_run, hosted_orion, networks
    ):

        with temporary_settings(PREFECT_ORION_HOST=hosted_orion):
            await DockerFlowRunner(networks=networks).submit_flow_run(
                flow_run, MagicMock()
            )
        mock_docker_client.containers.create.assert_called_once()
        call_network = mock_docker_client.containers.create.call_args[1].get("network")

        if not networks:
            assert not call_network
        else:
            assert call_network == networks[0]

        # Additional networks must be added after
        if len(networks) <= 1:
            mock_docker_client.networks.get.assert_not_called()
        else:
            for network_name in networks[1:]:
                mock_docker_client.networks.get.assert_called_with(network_name)

            # network.connect called with the created container
            mock_docker_client.networks.get().connect.assert_called_with(
                mock_docker_client.containers.create()
            )

    async def test_includes_prefect_labels(
        self, mock_docker_client, flow_run, hosted_orion
    ):
        with temporary_settings(PREFECT_ORION_HOST=hosted_orion):
            await DockerFlowRunner().submit_flow_run(flow_run, MagicMock())

        mock_docker_client.containers.create.assert_called_once()
        call_labels = mock_docker_client.containers.create.call_args[1].get("labels")
        assert call_labels == {
            "io.prefect.flow-run-id": str(flow_run.id),
        }

    async def test_uses_label_setting(self, mock_docker_client, flow_run, hosted_orion):
        with temporary_settings(PREFECT_ORION_HOST=hosted_orion):
            await DockerFlowRunner(labels={"foo": "FOO", "bar": "BAR"}).submit_flow_run(
                flow_run, MagicMock()
            )
        mock_docker_client.containers.create.assert_called_once()
        call_labels = mock_docker_client.containers.create.call_args[1].get("labels")
        assert "foo" in call_labels and "bar" in call_labels
        assert call_labels["foo"] == "FOO"
        assert call_labels["bar"] == "BAR"
        assert "io.prefect.flow-run-id" in call_labels, "prefect labels still included"

    async def test_uses_env_setting(self, mock_docker_client, flow_run, hosted_orion):
        with temporary_settings(PREFECT_ORION_HOST=hosted_orion):
            await DockerFlowRunner(env={"foo": "FOO", "bar": "BAR"}).submit_flow_run(
                flow_run, MagicMock()
            )
        mock_docker_client.containers.create.assert_called_once()
        call_env = mock_docker_client.containers.create.call_args[1].get("environment")
        assert "foo" in call_env and "bar" in call_env
        assert call_env["foo"] == "FOO"
        assert call_env["bar"] == "BAR"

    async def test_replaces_localhost_with_dockerhost_in_env(
        self, mock_docker_client, flow_run, hosted_orion
    ):
        with temporary_settings(PREFECT_ORION_HOST=hosted_orion):
            await DockerFlowRunner().submit_flow_run(flow_run, MagicMock())
        mock_docker_client.containers.create.assert_called_once()
        call_env = mock_docker_client.containers.create.call_args[1].get("environment")
        assert "PREFECT_ORION_HOST" in call_env
        assert call_env["PREFECT_ORION_HOST"] == hosted_orion.replace(
            "localhost", "host.docker.internal"
        )

    async def test_does_not_override_user_provided_host(
        self, mock_docker_client, flow_run, hosted_orion
    ):
        with temporary_settings(PREFECT_ORION_HOST=hosted_orion):
            await DockerFlowRunner(
                env={"PREFECT_ORION_HOST": "http://localhost/api"}
            ).submit_flow_run(flow_run, MagicMock())
        mock_docker_client.containers.create.assert_called_once()
        call_env = mock_docker_client.containers.create.call_args[1].get("environment")
        assert call_env.get("PREFECT_ORION_HOST") == "http://localhost/api"

    async def test_adds_docker_host_gateway(
        self, mock_docker_client, flow_run, hosted_orion
    ):
        with temporary_settings(PREFECT_ORION_HOST=hosted_orion):
            await DockerFlowRunner().submit_flow_run(flow_run, MagicMock())

        mock_docker_client.containers.create.assert_called_once()
        call_extra_hosts = mock_docker_client.containers.create.call_args[1].get(
            "extra_hosts"
        )
        assert call_extra_hosts == {"host.docker.internal": "host-gateway"}

    @pytest.mark.parametrize("docker_engine_version", ["0.25.10", "19.1.1"])
    async def test_warns_if_docker_version_does_not_support_host_gateway(
        self, mock_docker_client, flow_run, hosted_orion, docker_engine_version
    ):
        mock_docker_client.version.return_value = {"Version": docker_engine_version}
        with temporary_settings(PREFECT_ORION_HOST=hosted_orion):
            with pytest.warns(
                UserWarning,
                match=(
                    "`host.docker.internal` could not be automatically resolved.*"
                    f"feature is not supported on Docker Engine v{docker_engine_version}"
                ),
            ):
                await DockerFlowRunner().submit_flow_run(flow_run, MagicMock())

        mock_docker_client.containers.create.assert_called_once()
        call_extra_hosts = mock_docker_client.containers.create.call_args[1].get(
            "extra_hosts"
        )
        assert not call_extra_hosts

    async def test_raises_on_submission_with_ephemeral_api(
        self, mock_docker_client, flow_run
    ):
        with pytest.raises(
            RuntimeError,
            match="cannot be used with an ephemeral server",
        ):
            await DockerFlowRunner().submit_flow_run(flow_run, MagicMock())

    async def test_no_raise_on_submission_with_hosted_api(
        self, mock_docker_client, flow_run, hosted_orion
    ):
        with temporary_settings(
            PREFECT_ORION_HOST=hosted_orion,
        ):
            await DockerFlowRunner().submit_flow_run(flow_run, MagicMock())

    @pytest.mark.service("docker")
    async def test_executes_flow_run_with_hosted_api(
        self,
        flow_run,
        orion_client,
        hosted_orion,
        prefect_settings_test_deployment,
    ):
        fake_status = MagicMock(spec=anyio.abc.TaskStatus)

        flow_run = await orion_client.create_flow_run_from_deployment(
            prefect_settings_test_deployment
        )

        with temporary_settings(PREFECT_ORION_HOST=hosted_orion):
            assert await DockerFlowRunner().submit_flow_run(flow_run, fake_status)

        fake_status.started.assert_called_once()
        flow_run = await orion_client.read_flow_run(flow_run.id)
        runtime_settings = await orion_client.resolve_datadoc(flow_run.state.result())
        assert runtime_settings.orion_host == hosted_orion.replace(
            "localhost", "host.docker.internal"
        )

    @pytest.mark.service("docker")
    async def test_executing_flow_run_has_rw_access_to_volumes(
        self,
        flow_run,
        orion_client,
        hosted_orion,
        tmp_path,
    ):
        @prefect.flow
        def my_flow():
            Path("/root/mount/writefile").resolve().write_text("bar")
            return Path("/root/mount/readfile").resolve().read_text()

        flow_id = await orion_client.create_flow(my_flow)

        flow_data = DataDocument.encode("cloudpickle", my_flow)

        deployment_id = await orion_client.create_deployment(
            flow_id=flow_id,
            name="prefect_file_test_deployment",
            flow_data=flow_data,
        )

        fake_status = MagicMock(spec=anyio.abc.TaskStatus)

        flow_run = await orion_client.create_flow_run_from_deployment(deployment_id)

        # Write to a file that the flow will read from
        (tmp_path / "readfile").write_text("foo")

        with temporary_settings(PREFECT_ORION_HOST=hosted_orion):
            assert await DockerFlowRunner(
                volumes=[f"{tmp_path}:/root/mount"]
            ).submit_flow_run(flow_run, fake_status)

        fake_status.started.assert_called_once()
        flow_run = await orion_client.read_flow_run(flow_run.id)
        file_contents = await orion_client.resolve_datadoc(flow_run.state.result())
        assert file_contents == "foo"

        assert (tmp_path / "writefile").read_text() == "bar"

    @pytest.mark.service("docker")
    async def test_executing_flow_run_has_environment_variables(
        self,
        flow_run,
        orion_client,
        hosted_orion,
        os_environ_test_deployment,
    ):
        fake_status = MagicMock(spec=anyio.abc.TaskStatus)

        flow_run = await orion_client.create_flow_run_from_deployment(
            os_environ_test_deployment
        )

        with temporary_settings(PREFECT_ORION_HOST=hosted_orion):
            assert await DockerFlowRunner(
                env={"TEST_FOO": "foo", "TEST_BAR": "bar"}
            ).submit_flow_run(flow_run, fake_status)

        fake_status.started.assert_called_once()
        flow_run = await orion_client.read_flow_run(flow_run.id)
        flow_run_environ = await orion_client.resolve_datadoc(flow_run.state.result())
        assert "TEST_FOO" in flow_run_environ and "TEST_BAR" in flow_run_environ
        assert flow_run_environ["TEST_FOO"] == "foo"
        assert flow_run_environ["TEST_BAR"] == "bar"


# The following tests are for configuration options and can test all relevant types


@pytest.mark.parametrize(
    "runner_type", [UniversalFlowRunner, SubprocessFlowRunner, DockerFlowRunner]
)
class TestFlowRunnerConfigEnv:
    def test_flow_runner_env_config(self, runner_type):
        assert runner_type(env={"foo": "bar"}).env == {"foo": "bar"}

    def test_flow_runner_env_config_casts_to_strings(self, runner_type):
        assert runner_type(env={"foo": 1}).env == {"foo": "1"}

    def test_flow_runner_env_config_errors_if_not_castable(self, runner_type):
        with pytest.raises(pydantic.ValidationError):
            runner_type(env={"foo": object()})

    def test_flow_runner_env_to_settings(self, runner_type):
        runner = runner_type(env={"foo": "bar"})
        settings = runner.to_settings()
        assert settings.config["env"] == runner.env


@pytest.mark.parametrize("runner_type", [SubprocessFlowRunner, DockerFlowRunner])
class TestFlowRunnerConfigStreamOutput:
    def test_flow_runner_stream_output_config(self, runner_type):
        assert runner_type(stream_output=True).stream_output == True

    def test_flow_runner_stream_output_config_casts_to_bool(self, runner_type):
        assert runner_type(stream_output=1).stream_output == True

    def test_flow_runner_stream_output_config_errors_if_not_castable(self, runner_type):
        with pytest.raises(pydantic.ValidationError):
            runner_type(stream_output=object())

    @pytest.mark.parametrize("value", [True, False])
    def test_flow_runner_stream_output_to_settings(self, runner_type, value):
        runner = runner_type(stream_output=value)
        settings = runner.to_settings()
        assert settings.config["stream_output"] == value


@pytest.mark.parametrize("runner_type", [SubprocessFlowRunner])
class TestFlowRunnerConfigCondaEnv:
    @pytest.mark.parametrize("value", ["test", Path("test")])
    def test_flow_runner_condaenv_config(self, runner_type, value):
        assert runner_type(condaenv=value).condaenv == value

    def test_flow_runner_condaenv_config_casts_to_string(self, runner_type):
        assert runner_type(condaenv=1).condaenv == "1"

    @pytest.mark.parametrize("value", [f"~{os.sep}test", f"{os.sep}test"])
    def test_flow_runner_condaenv_config_casts_to_path(self, runner_type, value):
        assert runner_type(condaenv=value).condaenv == Path(value)

    def test_flow_runner_condaenv_config_errors_if_not_castable(self, runner_type):
        with pytest.raises(pydantic.ValidationError):
            runner_type(condaenv=object())

    @pytest.mark.parametrize("value", ["test", Path("test")])
    def test_flow_runner_condaenv_to_settings(self, runner_type, value):
        runner = runner_type(condaenv=value)
        settings = runner.to_settings()
        assert settings.config["condaenv"] == value

    def test_flow_runner_condaenv_cannot_be_provided_with_virtualenv(self, runner_type):
        with pytest.raises(
            pydantic.ValidationError, match="cannot provide both a conda and virtualenv"
        ):
            runner_type(condaenv="foo", virtualenv="bar")


@pytest.mark.parametrize("runner_type", [SubprocessFlowRunner])
class TestFlowRunnerConfigVirtualEnv:
    def test_flow_runner_virtualenv_config(self, runner_type):
        path = Path("~").expanduser()
        assert runner_type(virtualenv=path).virtualenv == path

    def test_flow_runner_virtualenv_config_casts_to_path(self, runner_type):
        assert runner_type(virtualenv="~/test").virtualenv == Path("~/test")
        assert (
            Path("~/test") != Path("~/test").expanduser()
        ), "We do not want to expand user at configuration time"

    def test_flow_runner_virtualenv_config_errors_if_not_castable(self, runner_type):
        with pytest.raises(pydantic.ValidationError):
            runner_type(virtualenv=object())

    def test_flow_runner_virtualenv_to_settings(self, runner_type):
        runner = runner_type(virtualenv=Path("~/test"))
        settings = runner.to_settings()
        assert settings.config["virtualenv"] == Path("~/test")


@pytest.mark.parametrize("runner_type", [DockerFlowRunner])
class TestFlowRunnerConfigVolumes:
    def test_flow_runner_volumes_config(self, runner_type):
        volumes = ["a:b", "c:d"]
        assert runner_type(volumes=volumes).volumes == volumes

    def test_flow_runner_volumes_config_does_not_expand_paths(self, runner_type):
        assert runner_type(volumes=["~/a:b"]).volumes == ["~/a:b"]

    def test_flow_runner_volumes_config_casts_to_list(self, runner_type):
        assert type(runner_type(volumes={"a:b", "c:d"}).volumes) == list

    def test_flow_runner_volumes_config_errors_if_invalid_format(self, runner_type):
        with pytest.raises(
            pydantic.ValidationError, match="Invalid volume specification"
        ):
            runner_type(volumes=["a"])

    def test_flow_runner_volumes_config_errors_if_invalid_type(self, runner_type):
        with pytest.raises(pydantic.ValidationError):
            runner_type(volumes={"a": "b"})

    def test_flow_runner_volumes_to_settings(self, runner_type):
        runner = runner_type(volumes=["a:b", "c:d"])
        settings = runner.to_settings()
        assert settings.config["volumes"] == ["a:b", "c:d"]


@pytest.mark.parametrize("runner_type", [DockerFlowRunner])
class TestFlowRunnerConfigNetworks:
    def test_flow_runner_networks_config(self, runner_type):
        networks = ["a", "b"]
        assert runner_type(networks=networks).networks == networks

    def test_flow_runner_networks_config_casts_to_list(self, runner_type):
        assert type(runner_type(networks={"a", "b"}).networks) == list

    def test_flow_runner_networks_config_errors_if_invalid_type(self, runner_type):
        with pytest.raises(pydantic.ValidationError):
            runner_type(volumes={"foo": "bar"})

    def test_flow_runner_networks_to_settings(self, runner_type):
        runner = runner_type(networks=["a", "b"])
        settings = runner.to_settings()
        assert settings.config["networks"] == ["a", "b"]


@pytest.mark.parametrize("runner_type", [DockerFlowRunner])
class TestFlowRunnerConfigAutoRemove:
    def test_flow_runner_auto_remove_config(self, runner_type):
        assert runner_type(auto_remove=True).auto_remove == True

    def test_flow_runner_auto_remove_config_casts_to_bool(self, runner_type):
        assert runner_type(auto_remove=1).auto_remove == True

    def test_flow_runner_auto_remove_config_errors_if_not_castable(self, runner_type):
        with pytest.raises(pydantic.ValidationError):
            runner_type(auto_remove=object())

    @pytest.mark.parametrize("value", [True, False])
    def test_flow_runner_auto_remove_to_settings(self, runner_type, value):
        runner = runner_type(auto_remove=value)
        settings = runner.to_settings()
        assert settings.config["auto_remove"] == value


@pytest.mark.parametrize("runner_type", [DockerFlowRunner])
class TestFlowRunnerConfigImage:
    def test_flow_runner_image_config(self, runner_type):
        value = "foo"
        assert runner_type(image=value).image == value

    def test_flow_runner_image_config_casts_to_string(self, runner_type):
        assert runner_type(image=1).image == "1"

    def test_flow_runner_image_config_errors_if_not_castable(self, runner_type):
        with pytest.raises(pydantic.ValidationError):
            runner_type(image=object())

    def test_flow_runner_image_to_settings(self, runner_type):
        runner = runner_type(image="test")
        settings = runner.to_settings()
        assert settings.config["image"] == "test"


@pytest.mark.parametrize("runner_type", [DockerFlowRunner])
class TestFlowRunnerConfigLabels:
    def test_flow_runner_labels_config(self, runner_type):
        assert runner_type(labels={"foo": "bar"}).labels == {"foo": "bar"}

    def test_flow_runner_labels_config_casts_to_strings(self, runner_type):
        assert runner_type(labels={"foo": 1}).labels == {"foo": "1"}

    def test_flow_runner_labels_config_errors_if_not_castable(self, runner_type):
        with pytest.raises(pydantic.ValidationError):
            runner_type(labels={"foo": object()})

    def test_flow_runner_labels_to_settings(self, runner_type):
        runner = runner_type(labels={"foo": "bar"})
        settings = runner.to_settings()
        assert settings.config["labels"] == runner.labels
