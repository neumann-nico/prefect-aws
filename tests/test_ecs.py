import json
from functools import partial
from typing import Callable, Dict, List
from unittest.mock import MagicMock

import anyio
import pytest
import yaml
from moto import mock_ec2, mock_ecs, mock_logs
from moto.ec2.utils import generate_instance_identity_document
from prefect.logging.configuration import setup_logging
from prefect.utilities.asyncutils import run_sync_in_worker_thread
from pydantic import ValidationError

from prefect_aws.ecs import (
    ECS_DEFAULT_CPU,
    ECS_DEFAULT_MEMORY,
    PREFECT_ECS_CONTAINER_NAME,
    ECSTask,
    get_container,
    get_prefect_container,
)

setup_logging()


BASE_TASK_DEFINITION_YAML = """
containerDefinitions:
- cpu: 1024
  image: prefecthq/prefect:2.1.0-python3.8
  memory: 2048
  name: prefect
family: prefect
"""

BASE_TASK_DEFINITION = yaml.safe_load(BASE_TASK_DEFINITION_YAML)


def inject_moto_patches(moto_mock, patches: Dict[str, List[Callable]]):
    def injected_call(method, patch_list, *args, **kwargs):
        for patch in patch_list:
            result = patch(method, *args, **kwargs)
        return result

    for account in moto_mock.backends:
        for region in moto_mock.backends[account]:
            backend = moto_mock.backends[account][region]

            for attr, attr_patches in patches.items():
                original_method = getattr(backend, attr)
                setattr(
                    backend, attr, partial(injected_call, original_method, attr_patches)
                )


def patch_describe_tasks_add_prefect_container(describe_tasks, *args, **kwargs):
    """
    Adds the minimal prefect container to moto's task description.
    """
    result = describe_tasks(*args, **kwargs)
    for task in result:
        task.containers = [{"name": PREFECT_ECS_CONTAINER_NAME}]
    return result


def patch_run_task(mock, run_task, *args, **kwargs):
    """
    Track calls to `run_task` by calling a mock as well.
    """
    mock(*args, **kwargs)
    return run_task(*args, **kwargs)


def patch_calculate_task_resource_requirements(
    _calculate_task_resource_requirements, task_definition
):
    """
    Adds support for non-EC2 execution modes to moto's calculation of task definition.
    """
    for container_definition in task_definition.container_definitions:
        container_definition.setdefault("memory", 0)
    return _calculate_task_resource_requirements(task_definition)


def add_ec2_instance_to_ecs_cluster(session, cluster_name):
    ecs_client = session.client("ecs")
    ec2_client = session.client("ec2")
    ec2_resource = session.resource("ec2")

    ecs_client.create_cluster(clusterName=cluster_name)

    images = ec2_client.describe_images()
    image_id = images["Images"][0]["ImageId"]

    test_instance = ec2_resource.create_instances(
        ImageId=image_id, MinCount=1, MaxCount=1
    )[0]

    ecs_client.register_container_instance(
        cluster=cluster_name,
        instanceIdentityDocument=json.dumps(
            generate_instance_identity_document(test_instance)
        ),
    )


def create_test_ecs_cluster(ecs_client, cluster_name) -> str:
    """
    Create an ECS cluster and return its ARN
    """
    return ecs_client.create_cluster(clusterName=cluster_name)["cluster"]["clusterArn"]


def describe_task(ecs_client, task_arn, **kwargs) -> dict:
    """
    Describe a single ECS task
    """
    return ecs_client.describe_tasks(tasks=[task_arn], **kwargs)["tasks"][0]


async def stop_task(ecs_client, task_arn, **kwargs):
    """
    Stop an ECS task.

    Additional keyword arguments are passed to `ECSClient.stop_task`.
    """
    task = await run_sync_in_worker_thread(describe_task, ecs_client, task_arn)
    # Check that the task started successfully
    assert task["lastStatus"] == "RUNNING", "Task should be RUNNING before stopping"
    print("Stopping task...")
    await run_sync_in_worker_thread(ecs_client.stop_task, task=task_arn, **kwargs)


def describe_task_definition(ecs_client, task):
    return ecs_client.describe_task_definition(
        taskDefinition=task["taskDefinitionArn"]
    )["taskDefinition"]


async def run_then_stop_task(task: ECSTask, **kwargs) -> str:
    """
    Run an ECS Task then stop it.

    Moto will not advance the state of tasks, so `ECSTask.run` would hang forever if
    the run is created successfully and not stopped.

    Additional keyword arguments are passed to `ECSClient.stop_task`
    """
    session = task.aws_credentials.get_boto3_session()

    with anyio.fail_after(20):
        async with anyio.create_task_group() as tg:
            task_arn = await tg.start(task.run)
            # Stop the task after it starts to prevent the test from running forever
            tg.start_soon(partial(stop_task, session.client("ecs"), task_arn, **kwargs))

    return task_arn


@pytest.fixture(autouse=True)
def patch_task_watch_poll_interval(monkeypatch):
    # Patch the poll interval to be way shorter for speed during testing!
    monkeypatch.setattr(ECSTask.__fields__["task_watch_poll_interval"], "default", 0.05)


@pytest.fixture
def ecs_mocks(aws_credentials):
    with mock_ecs() as ecs:
        with mock_ec2():
            inject_moto_patches(
                ecs,
                {
                    # Add a container when describing any task
                    "describe_tasks": [patch_describe_tasks_add_prefect_container],
                    # Fix moto internal resource requirement calculations
                    "_calculate_task_resource_requirements": [
                        patch_calculate_task_resource_requirements
                    ],
                },
            )

            session = aws_credentials.get_boto3_session()
            create_test_ecs_cluster(session.client("ecs"), "default")

            # NOTE: Even when using FARGATE, moto requires container instances to be
            #       registered. This differs from AWS behavior.
            add_ec2_instance_to_ecs_cluster(session, "default")

            yield ecs


@pytest.mark.usefixtures("ecs_mocks")
@pytest.mark.parametrize("launch_type", ["EC2", "FARGATE", "FARGATE_SPOT"])
async def test_launch_types(aws_credentials, launch_type: str):
    task = ECSTask(
        aws_credentials=aws_credentials,
        auto_deregister_task_definition=False,
        command=["prefect", "version"],
        launch_type=launch_type,
    )
    print(task.preview())

    session = aws_credentials.get_boto3_session()
    ecs_client = session.client("ecs")

    task_arn = await run_then_stop_task(task)

    task = describe_task(ecs_client, task_arn)
    task_definition = describe_task_definition(ecs_client, task)

    if launch_type != "FARGATE_SPOT":
        assert launch_type in task_definition["compatibilities"]
        assert task["launchType"] == launch_type
    else:
        assert "FARGATE" in task_definition["compatibilities"]
        # FARGATE SPOT requires a null launch type
        assert not task.get("launchType")
        # Instead, it requires a capacity provider strategy but this is not supported
        # by moto and is not present on the task even when provided
        # assert task["capacityProviderStrategy"] == [
        #     {"capacityProvider": "FARGATE_SPOT", "weight": 1}
        # ]

    requires_capabilities = task_definition.get("requiresCompatibilities", [])
    if launch_type != "EC2":
        assert "FARGATE" in requires_capabilities
    else:
        assert not requires_capabilities


@pytest.mark.usefixtures("ecs_mocks")
@pytest.mark.parametrize("launch_type", ["EC2", "FARGATE", "FARGATE_SPOT"])
@pytest.mark.parametrize(
    "cpu,memory", [(None, None), (1024, None), (None, 2048), (2048, 4096)]
)
async def test_cpu_and_memory(aws_credentials, launch_type: str, cpu: int, memory: int):
    task = ECSTask(
        aws_credentials=aws_credentials,
        auto_deregister_task_definition=False,
        command=["prefect", "version"],
        launch_type=launch_type,
        cpu=cpu,
        memory=memory,
    )
    print(task.preview())

    session = aws_credentials.get_boto3_session()
    ecs_client = session.client("ecs")

    task_arn = await run_then_stop_task(task)

    task = describe_task(ecs_client, task_arn)
    task_definition = describe_task_definition(ecs_client, task)
    container_definition = get_prefect_container(
        task_definition["containerDefinitions"]
    )
    overrides = task["overrides"]
    container_overrides = get_prefect_container(overrides["containerOverrides"])

    if launch_type == "EC2":
        # EC2 requires CPU and memory to be defined at the container level
        assert container_definition["cpu"] == cpu or ECS_DEFAULT_CPU
        assert container_definition["memory"] == memory or ECS_DEFAULT_MEMORY
    else:
        # Fargate requires CPU and memory to be defined at the task definition level
        assert task_definition["cpu"] == str(cpu or ECS_DEFAULT_CPU)
        assert task_definition["memory"] == str(memory or ECS_DEFAULT_MEMORY)

    # We always provide non-null values as overrides on the task run
    assert overrides.get("cpu") == (str(cpu) if cpu else None)
    assert overrides.get("memory") == (str(memory) if memory else None)
    # And as overrides for the Prefect container
    assert container_overrides.get("cpu") == cpu
    assert container_overrides.get("memory") == memory


@pytest.mark.usefixtures("ecs_mocks")
@pytest.mark.parametrize("launch_type", ["EC2", "FARGATE", "FARGATE_SPOT"])
async def test_network_mode_default(aws_credentials, launch_type: str):
    task = ECSTask(
        aws_credentials=aws_credentials,
        auto_deregister_task_definition=False,
        command=["prefect", "version"],
        launch_type=launch_type,
    )
    print(task.preview())

    session = aws_credentials.get_boto3_session()
    ecs_client = session.client("ecs")

    task_arn = await run_then_stop_task(task)

    task = describe_task(ecs_client, task_arn)
    task_definition = describe_task_definition(ecs_client, task)

    if launch_type == "EC2":
        assert task_definition["networkMode"] == "bridge"
    else:
        assert task_definition["networkMode"] == "awsvpc"


@pytest.mark.usefixtures("ecs_mocks")
@pytest.mark.parametrize("launch_type", ["EC2", "FARGATE", "FARGATE_SPOT"])
async def test_container_command(aws_credentials, launch_type: str):
    task = ECSTask(
        aws_credentials=aws_credentials,
        auto_deregister_task_definition=False,
        command=["prefect", "version"],
        launch_type=launch_type,
    )
    print(task.preview())

    session = aws_credentials.get_boto3_session()
    ecs_client = session.client("ecs")

    task_arn = await run_then_stop_task(task)

    task = describe_task(ecs_client, task_arn)

    container_overrides = get_prefect_container(task["overrides"]["containerOverrides"])
    assert container_overrides["command"] == ["prefect", "version"]


@pytest.mark.usefixtures("ecs_mocks")
async def test_environment_variables(aws_credentials):
    task = ECSTask(
        aws_credentials=aws_credentials,
        auto_deregister_task_definition=False,
        env={"FOO": "BAR"},
    )
    print(task.preview())

    session = aws_credentials.get_boto3_session()
    ecs_client = session.client("ecs")

    task_arn = await run_then_stop_task(task)

    task = describe_task(ecs_client, task_arn)
    task_definition = describe_task_definition(ecs_client, task)
    prefect_container_definition = get_prefect_container(
        task_definition["containerDefinitions"]
    )
    assert not prefect_container_definition[
        "environment"
    ], "Variables should not be passed until runtime"

    prefect_container_overrides = get_prefect_container(
        task["overrides"]["containerOverrides"]
    )
    expected = [
        {"name": key, "value": value}
        for key, value in ECSTask._base_environment().items()
    ]
    expected.append({"name": "FOO", "value": "BAR"})
    assert prefect_container_overrides.get("environment") == expected


@pytest.mark.usefixtures("ecs_mocks")
async def test_labels(aws_credentials):
    task = ECSTask(
        aws_credentials=aws_credentials,
        auto_deregister_task_definition=False,
        labels={"foo": "bar"},
    )
    print(task.preview())

    session = aws_credentials.get_boto3_session()
    ecs_client = session.client("ecs")

    task_arn = await run_then_stop_task(task)

    task = describe_task(ecs_client, task_arn)
    task_definition = describe_task_definition(ecs_client, task)
    assert not task_definition.get("tags"), "Labels should not be passed until runtime"

    assert task.get("tags") == [{"key": "foo", "value": "bar"}]


@pytest.mark.usefixtures("ecs_mocks")
async def test_container_command_from_task_definition(aws_credentials):
    task = ECSTask(
        aws_credentials=aws_credentials,
        auto_deregister_task_definition=False,
        task_definition={
            "containerDefinitions": [{"name": "prefect", "command": ["echo", "hello"]}]
        },
        command=[],
    )
    print(task.preview())

    session = aws_credentials.get_boto3_session()
    ecs_client = session.client("ecs")

    task_arn = await run_then_stop_task(task)

    task = describe_task(ecs_client, task_arn)

    container_overrides = get_prefect_container(task["overrides"]["containerOverrides"])
    assert "command" not in container_overrides


@pytest.mark.usefixtures("ecs_mocks")
async def test_extra_containers_in_task_definition(aws_credentials):
    task = ECSTask(
        aws_credentials=aws_credentials,
        auto_deregister_task_definition=False,
        task_definition={
            "containerDefinitions": [
                {"name": "secondary", "command": ["echo", "hello"], "image": "alpine"}
            ]
        },
        command=["prefect", "version"],
        image="test",
    )
    print(task.preview())

    session = aws_credentials.get_boto3_session()
    ecs_client = session.client("ecs")

    task_arn = await run_then_stop_task(task)

    task = describe_task(ecs_client, task_arn)
    task_definition = describe_task_definition(ecs_client, task)

    user_container = get_container(task_definition["containerDefinitions"], "secondary")
    assert (
        user_container is not None
    ), "The user-specified container should be present still"
    assert user_container["command"] == ["echo", "hello"]
    assert user_container["image"] == "alpine", "The image should be left unchanged"

    prefect_container = get_prefect_container(task_definition["containerDefinitions"])
    assert prefect_container is not None, "The prefect container should be added"
    assert (
        prefect_container["image"] == "test"
    ), "The prefect container should use the image field"

    container_overrides = task["overrides"]["containerOverrides"]
    user_container_overrides = get_container(container_overrides, "secondary")
    prefect_container_overrides = get_prefect_container(container_overrides)
    assert (
        user_container_overrides is None
    ), "The user container should not be included in overrides"
    assert (
        prefect_container_overrides
    ), "The prefect container should have overrides still"


@pytest.mark.usefixtures("ecs_mocks")
async def test_prefect_container_in_task_definition(aws_credentials):
    task = ECSTask(
        aws_credentials=aws_credentials,
        auto_deregister_task_definition=False,
        task_definition={
            "containerDefinitions": [
                {
                    "name": "prefect",
                    "command": ["should", "be", "gone"],
                    "image": "should-be-gone",
                    "privileged": True,
                }
            ]
        },
        command=["prefect", "version"],
        image="test",
    )
    print(task.preview())

    session = aws_credentials.get_boto3_session()
    ecs_client = session.client("ecs")

    task_arn = await run_then_stop_task(task)

    task = describe_task(ecs_client, task_arn)
    task_definition = describe_task_definition(ecs_client, task)

    prefect_container = get_prefect_container(task_definition["containerDefinitions"])
    assert (
        prefect_container["image"] == "test"
    ), "The prefect container should use the image field"
    assert prefect_container["command"] == [
        "should",
        "be",
        "gone",
    ], "The command should be left unchanged on the task definition"
    assert (
        prefect_container["privileged"] is True
    ), "Extra attributes should be retained"

    container_overrides = get_prefect_container(task["overrides"]["containerOverrides"])
    assert container_overrides["command"] == [
        "prefect",
        "version",
    ], "The command should be passed as an override"


@pytest.mark.usefixtures("ecs_mocks")
async def test_image_in_task_definition(aws_credentials):
    task = ECSTask(
        aws_credentials=aws_credentials,
        auto_deregister_task_definition=False,
        task_definition={
            "containerDefinitions": [
                {
                    "name": "prefect",
                    "image": "use-this-image",
                }
            ]
        },
        command=["prefect", "version"],
        image=None,
    )
    print(task.preview())

    session = aws_credentials.get_boto3_session()
    ecs_client = session.client("ecs")

    task_arn = await run_then_stop_task(task)

    task = describe_task(ecs_client, task_arn)
    task_definition = describe_task_definition(ecs_client, task)

    prefect_container = get_prefect_container(task_definition["containerDefinitions"])
    assert (
        prefect_container["image"] == "use-this-image"
    ), "The prefect container should use the image field"


@pytest.mark.parametrize(
    "task_definition",
    [
        # Empty task definition
        {},
        # Task definnition with prefect container
        {
            "containerDefinitions": [
                {
                    "name": "prefect",
                }
            ]
        },
        # Task definition with other container
        {
            "containerDefinitions": [
                {
                    "name": "foo",
                }
            ]
        },
    ],
)
@pytest.mark.usefixtures("ecs_mocks")
async def test_error_if_null_image_without_image_in_task_definition(
    aws_credentials, task_definition
):
    with pytest.raises(
        ValidationError, match="A value for the `image` field must be provided"
    ):
        ECSTask(
            aws_credentials=aws_credentials,
            auto_deregister_task_definition=False,
            task_definition=task_definition,
            command=["prefect", "version"],
            image=None,
        )


@pytest.mark.usefixtures("ecs_mocks")
@pytest.mark.parametrize("launch_type", ["EC2", "FARGATE", "FARGATE_SPOT"])
async def test_default_cpu_and_memory_in_task_definition(
    aws_credentials, launch_type: str
):
    task = ECSTask(
        aws_credentials=aws_credentials,
        auto_deregister_task_definition=False,
        task_definition={
            "containerDefinitions": [
                {
                    "name": "prefect",
                    "command": ["should", "be", "gone"],
                    "image": "should-be-gone",
                    "cpu": 2048,
                    "memory": 4096,
                }
            ],
            "cpu": "4096",
            "memory": "8192",
        },
        command=["prefect", "version"],
        image="test",
        launch_type=launch_type,
    )
    print(task.preview())

    session = aws_credentials.get_boto3_session()
    ecs_client = session.client("ecs")

    task_arn = await run_then_stop_task(task)

    task = describe_task(ecs_client, task_arn)
    task_definition = describe_task_definition(ecs_client, task)
    container_definition = get_prefect_container(
        task_definition["containerDefinitions"]
    )
    overrides = task["overrides"]
    container_overrides = get_prefect_container(overrides["containerOverrides"])

    # All of these values should be retained
    assert container_definition["cpu"] == 2048
    assert container_definition["memory"] == 4096
    assert task_definition["cpu"] == str(4096)
    assert task_definition["memory"] == str(8192)

    # No values should be overriden at runtime
    assert overrides.get("cpu") is None
    assert overrides.get("memory") is None
    assert container_overrides.get("cpu") is None
    assert container_overrides.get("memory") is None


@pytest.mark.usefixtures("ecs_mocks")
async def test_environment_variables_in_task_definition(aws_credentials):
    # See also, `test_unset_environment_variables_in_task_definition`
    task = ECSTask(
        aws_credentials=aws_credentials,
        auto_deregister_task_definition=False,
        task_definition={
            "containerDefinitions": [
                {
                    "name": "prefect",
                    "environment": [
                        {"name": "BAR", "value": "FOO"},
                        {"name": "OVERRIDE", "value": "OLD"},
                    ],
                }
            ],
        },
        env={"FOO": "BAR", "OVERRIDE": "NEW"},
    )
    print(task.preview())

    session = aws_credentials.get_boto3_session()
    ecs_client = session.client("ecs")

    task_arn = await run_then_stop_task(task)

    task = describe_task(ecs_client, task_arn)
    task_definition = describe_task_definition(ecs_client, task)
    prefect_container_definition = get_prefect_container(
        task_definition["containerDefinitions"]
    )

    assert prefect_container_definition["environment"] == [
        {"name": "BAR", "value": "FOO"},
        {"name": "OVERRIDE", "value": "OLD"},
    ]

    prefect_container_overrides = get_prefect_container(
        task["overrides"]["containerOverrides"]
    )
    expected_base = [
        {"name": key, "value": value}
        for key, value in ECSTask._base_environment().items()
    ]
    assert prefect_container_overrides.get("environment") == expected_base + [
        {"name": "FOO", "value": "BAR"},
        {"name": "OVERRIDE", "value": "NEW"},
    ]


@pytest.mark.usefixtures("ecs_mocks")
async def test_unset_environment_variables_in_task_definition(aws_credentials):
    # In contrast to `test_environment_variables_in_task_definition`, this tests the
    # use of `None` in `ECSTask.env` values to signal _removal_ of an environment
    # variable instead of overriding a value.
    task = ECSTask(
        aws_credentials=aws_credentials,
        auto_deregister_task_definition=False,
        task_definition={
            "containerDefinitions": [
                {
                    "name": "prefect",
                    "environment": [
                        {"name": "FOO", "value": "FOO"},
                        {"name": "BAR", "value": "BAR"},
                    ],
                }
            ]
        },
        env={"FOO": None},
    )
    print(task.preview())

    session = aws_credentials.get_boto3_session()
    ecs_client = session.client("ecs")

    task_arn = await run_then_stop_task(task)

    task = describe_task(ecs_client, task_arn)
    task_definition = describe_task_definition(ecs_client, task)
    prefect_container_definition = get_prefect_container(
        task_definition["containerDefinitions"]
    )
    assert prefect_container_definition["environment"] == [
        {"name": "BAR", "value": "BAR"}
    ], "FOO should be removed from the task definition"

    expected_base = [
        {"name": key, "value": value}
        for key, value in ECSTask._base_environment().items()
    ]
    prefect_container_overrides = get_prefect_container(
        task["overrides"]["containerOverrides"]
    )
    assert (
        prefect_container_overrides.get("environment") == expected_base
    ), "FOO should not be passed at runtime"


@pytest.mark.usefixtures("ecs_mocks")
@pytest.mark.parametrize("provided_as_field", [True, False])
async def test_execution_role_arn_in_task_definition(
    aws_credentials, provided_as_field: bool
):
    session = aws_credentials.get_boto3_session()
    ecs_client = session.client("ecs")

    task = ECSTask(
        aws_credentials=aws_credentials,
        auto_deregister_task_definition=False,
        task_definition={"executionRoleArn": "test"},
        execution_role_arn="override" if provided_as_field else None,
    )
    print(task.preview())

    task_arn = await run_then_stop_task(task)

    task = describe_task(ecs_client, task_arn)
    task_definition = describe_task_definition(ecs_client, task)

    # Check if it is overidden if provided as a field
    assert (
        task_definition["executionRoleArn"] == "test"
        if not provided_as_field
        else "override"
    )


@pytest.mark.usefixtures("ecs_mocks")
@pytest.mark.parametrize("default_cluster", [True, False])
async def test_cluster(aws_credentials, default_cluster: bool):

    session = aws_credentials.get_boto3_session()
    ecs_client = session.client("ecs")

    # Construct a non-default cluster. We build this in either case since otherwise
    # there is only one cluster and there's no choice but to use the default.
    second_cluster_arn = create_test_ecs_cluster(ecs_client, "second-cluster")
    add_ec2_instance_to_ecs_cluster(session, "second-cluster")

    task = ECSTask(
        aws_credentials=aws_credentials,
        auto_deregister_task_definition=False,
        cluster=None if default_cluster else "second-cluster",
    )
    print(task.preview())

    task_arn = await run_then_stop_task(
        # Stopping a task requires the active cluster to be specified
        task,
        cluster="default" if default_cluster else second_cluster_arn,
    )

    task = describe_task(ecs_client, task_arn)

    if default_cluster:
        assert task["clusterArn"].endswith("default")
    else:
        assert task["clusterArn"] == second_cluster_arn


@pytest.mark.usefixtures("ecs_mocks")
async def test_execution_role_arn(aws_credentials):
    session = aws_credentials.get_boto3_session()
    ecs_client = session.client("ecs")

    task = ECSTask(
        aws_credentials=aws_credentials,
        auto_deregister_task_definition=False,
        execution_role_arn="test",
    )
    print(task.preview())

    task_arn = await run_then_stop_task(task)

    task = describe_task(ecs_client, task_arn)
    task_definition = describe_task_definition(ecs_client, task)

    assert task_definition["executionRoleArn"] == "test"


@pytest.mark.usefixtures("ecs_mocks")
async def test_task_role_arn(aws_credentials):
    session = aws_credentials.get_boto3_session()
    ecs_client = session.client("ecs")

    task = ECSTask(
        aws_credentials=aws_credentials,
        auto_deregister_task_definition=False,
        task_role_arn="test",
    )
    print(task.preview())

    task_arn = await run_then_stop_task(task)

    task = describe_task(ecs_client, task_arn)

    assert task["overrides"]["taskRoleArn"] == "test"


@pytest.mark.usefixtures("ecs_mocks")
async def test_network_config_from_vpc_id(aws_credentials):
    session = aws_credentials.get_boto3_session()
    ec2_resource = session.resource("ec2")
    vpc = ec2_resource.create_vpc(CidrBlock="10.0.0.0/16")
    subnet = ec2_resource.create_subnet(CidrBlock="10.0.2.0/24", VpcId=vpc.id)

    task = ECSTask(aws_credentials=aws_credentials, vpc_id=vpc.id)

    # Capture the task run call because moto does not track 'networkConfiguration'
    original_run_task = task._run_task
    mock_run_task = MagicMock(side_effect=original_run_task)
    task._run_task = mock_run_task

    print(task.preview())

    await run_then_stop_task(task)

    network_configuration = mock_run_task.call_args[0][1].get("networkConfiguration")

    # Subnet ids are copied from the vpc
    assert network_configuration == {
        "awsvpcConfiguration": {"subnets": [subnet.id], "assignPublicIp": "ENABLED"}
    }


@pytest.mark.usefixtures("ecs_mocks")
async def test_network_config_from_default_vpc(aws_credentials):
    session = aws_credentials.get_boto3_session()
    ec2_client = session.client("ec2")

    default_vpc_id = ec2_client.describe_vpcs(
        Filters=[{"Name": "isDefault", "Values": ["true"]}]
    )["Vpcs"][0]["VpcId"]
    default_subnets = ec2_client.describe_subnets(
        Filters=[{"Name": "vpc-id", "Values": [default_vpc_id]}]
    )["Subnets"]

    task = ECSTask(aws_credentials=aws_credentials)

    # Capture the task run call because moto does not track 'networkConfiguration'
    original_run_task = task._run_task
    mock_run_task = MagicMock(side_effect=original_run_task)
    task._run_task = mock_run_task

    print(task.preview())

    await run_then_stop_task(task)

    network_configuration = mock_run_task.call_args[0][1].get("networkConfiguration")

    # Subnet ids are copied from the vpc
    assert network_configuration == {
        "awsvpcConfiguration": {
            "subnets": [subnet["SubnetId"] for subnet in default_subnets],
            "assignPublicIp": "ENABLED",
        }
    }


@pytest.mark.usefixtures("ecs_mocks")
@pytest.mark.parametrize("explicit_network_mode", [True, False])
async def test_network_config_is_empty_without_awsvpc_network_mode(
    aws_credentials, explicit_network_mode
):
    task = ECSTask(
        aws_credentials=aws_credentials,
        auto_deregister_task_definition=False,
        # EC2 uses the 'bridge' network mode by default but we want to have test
        # coverage for when it is set on the task definition
        task_definition={"networkMode": "bridge"} if explicit_network_mode else None,
        # FARGATE requires the 'awsvpc' network mode
        launch_type="EC2",
    )

    # Capture the task run call because moto does not track 'networkConfiguration'
    original_run_task = task._run_task
    mock_run_task = MagicMock(side_effect=original_run_task)
    task._run_task = mock_run_task

    print(task.preview())

    await run_then_stop_task(task)

    network_configuration = mock_run_task.call_args[0][1].get("networkConfiguration")
    assert network_configuration is None


@pytest.mark.usefixtures("ecs_mocks")
async def test_network_config_missing_default_vpc(aws_credentials):
    session = aws_credentials.get_boto3_session()
    ec2_client = session.client("ec2")

    default_vpc_id = ec2_client.describe_vpcs(
        Filters=[{"Name": "isDefault", "Values": ["true"]}]
    )["Vpcs"][0]["VpcId"]
    ec2_client.delete_vpc(VpcId=default_vpc_id)

    task = ECSTask(aws_credentials=aws_credentials)

    with pytest.raises(ValueError, match="Failed to find the default VPC"):
        await run_then_stop_task(task)


@pytest.mark.usefixtures("ecs_mocks")
async def test_network_config_from_vpc_with_no_subnets(aws_credentials):
    session = aws_credentials.get_boto3_session()
    ec2_resource = session.resource("ec2")
    vpc = ec2_resource.create_vpc(CidrBlock="172.16.0.0/16")

    task = ECSTask(
        aws_credentials=aws_credentials,
        auto_deregister_task_definition=False,
        vpc_id=vpc.id,
    )
    print(task.preview())

    with pytest.raises(
        ValueError, match=f"Failed to find subnets for VPC with ID {vpc.id}"
    ):
        await run_then_stop_task(task)


@pytest.mark.usefixtures("ecs_mocks")
async def test_logging_requires_execution_role_arn(aws_credentials):
    with pytest.raises(
        ValidationError,
        match="`execution_role_arn` must be provided",
    ):
        ECSTask(
            aws_credentials=aws_credentials,
            command=["prefect", "version"],
            configure_cloudwatch_logs=True,
        )


@pytest.mark.usefixtures("ecs_mocks")
async def test_log_options_requires_logging(aws_credentials):
    with pytest.raises(
        ValidationError,
        match="`configure_cloudwatch_log` must be enabled to use `cloudwatch_logs_options`",  # noqa
    ):
        ECSTask(
            aws_credentials=aws_credentials,
            command=["prefect", "version"],
            configure_cloudwatch_logs=False,
            cloudwatch_logs_options={"foo": " bar"},
        )


@pytest.mark.usefixtures("ecs_mocks")
async def test_logging_requires_execution_role_arn_at_runtime(aws_credentials):
    # In constrast to `test_logging_requires_execution_role_arn`, a task definition
    # has been provided by ARN reference and we do not know if the execution role is
    # missing until runtime.

    session = aws_credentials.get_boto3_session()
    ecs_client = session.client("ecs")
    task_definition_arn = ecs_client.register_task_definition(**BASE_TASK_DEFINITION)[
        "taskDefinition"
    ]["taskDefinitionArn"]

    task = ECSTask(
        aws_credentials=aws_credentials,
        auto_deregister_task_definition=False,
        command=["prefect", "version"],
        configure_cloudwatch_logs=True,
        task_definition_arn=task_definition_arn,
        # This test is launch type agnostic but the task definition we register receives
        # the default network mode type of 'bridge' which is not compatible with FARGATE
        launch_type="EC2",
    )
    with pytest.raises(ValueError, match="An execution role arn must be set"):
        await task.run()


@pytest.mark.usefixtures("ecs_mocks")
async def test_configure_cloudwatch_logging(aws_credentials):
    session = aws_credentials.get_boto3_session()
    ecs_client = session.client("ecs")

    with mock_logs():
        task = ECSTask(
            aws_credentials=aws_credentials,
            auto_deregister_task_definition=False,
            command=["prefect", "version"],
            configure_cloudwatch_logs=True,
            execution_role_arn="test",
        )

    task_arn = await run_then_stop_task(task)
    task = describe_task(ecs_client, task_arn)
    task_definition = describe_task_definition(ecs_client, task)

    for container in task_definition["containerDefinitions"]:
        if container["name"] == "prefect":
            # Assert that the 'prefect' container has logging configured
            assert container["logConfiguration"] == {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-create-group": "true",
                    "awslogs-group": "prefect",
                    "awslogs-region": "us-east-1",
                    "awslogs-stream-prefix": "prefect",
                },
            }
        else:
            # Other containers should not be modifed
            assert "logConfiguration" not in container


@pytest.mark.usefixtures("ecs_mocks")
async def test_cloudwatch_log_options(aws_credentials):
    session = aws_credentials.get_boto3_session()
    ecs_client = session.client("ecs")

    with mock_logs():
        task = ECSTask(
            aws_credentials=aws_credentials,
            auto_deregister_task_definition=False,
            command=["prefect", "version"],
            configure_cloudwatch_logs=True,
            execution_role_arn="test",
            cloudwatch_logs_options={
                "awslogs-stream-prefix": "override-prefix",
                "max-buffer-size": "2m",
            },
        )

    task_arn = await run_then_stop_task(task)
    task = describe_task(ecs_client, task_arn)
    task_definition = describe_task_definition(ecs_client, task)

    for container in task_definition["containerDefinitions"]:
        if container["name"] == "prefect":
            # Assert that the 'prefect' container has logging configured with user
            # provided options
            assert container["logConfiguration"] == {
                "logDriver": "awslogs",
                "options": {
                    "awslogs-create-group": "true",
                    "awslogs-group": "prefect",
                    "awslogs-region": "us-east-1",
                    "awslogs-stream-prefix": "override-prefix",
                    "max-buffer-size": "2m",
                },
            }
        else:
            # Other containers should not be modifed
            assert "logConfiguration" not in container


@pytest.mark.usefixtures("ecs_mocks")
@pytest.mark.parametrize("launch_type", ["FARGATE", "FARGATE_SPOT"])
async def test_bridge_network_mode_warns_on_fargate(aws_credentials, launch_type: str):
    task = ECSTask(
        aws_credentials=aws_credentials,
        auto_deregister_task_definition=False,
        command=["prefect", "version"],
        task_definition={"networkMode": "bridge"},
        launch_type=launch_type,
    )
    with pytest.warns(
        UserWarning,
        match=(
            "Found network mode 'bridge' which is not compatible with launch type "
            f"{launch_type!r}"
        ),
    ):
        await run_then_stop_task(task)


@pytest.mark.usefixtures("ecs_mocks")
async def test_deregister_task_definition(aws_credentials):
    task = ECSTask(
        aws_credentials=aws_credentials,
        auto_deregister_task_definition=True,
    )
    print(task.preview())

    session = aws_credentials.get_boto3_session()
    ecs_client = session.client("ecs")

    task_arn = await run_then_stop_task(task)

    task = describe_task(ecs_client, task_arn)
    with pytest.raises(Exception, match="is not a task_definition"):
        # Oh no it's gone
        describe_task_definition(ecs_client, task)


@pytest.mark.usefixtures("ecs_mocks")
async def test_task_definition_arn(aws_credentials):
    session = aws_credentials.get_boto3_session()
    ecs_client = session.client("ecs")

    task_definition_arn = ecs_client.register_task_definition(**BASE_TASK_DEFINITION)[
        "taskDefinition"
    ]["taskDefinitionArn"]

    task = ECSTask(
        aws_credentials=aws_credentials,
        auto_deregister_task_definition=False,
        task_definition_arn=task_definition_arn,
        launch_type="EC2",
        image=None,
    )
    print(task.preview())
    task_arn = await run_then_stop_task(task)

    task = describe_task(ecs_client, task_arn)
    assert (
        task["taskDefinitionArn"] == task_definition_arn
    ), "The task definition should be used without registering a new one"


@pytest.mark.usefixtures("ecs_mocks")
@pytest.mark.parametrize(
    "overrides",
    [
        {"image": "new-image"},
        {"configure_cloudwatch_logs": True},
    ],
)
async def test_task_definition_arn_with_overrides_that_require_copy(
    aws_credentials, overrides
):
    """
    Any of these overrides should cause the task definition to be copied and
    registered as a new version
    """
    session = aws_credentials.get_boto3_session()
    ecs_client = session.client("ecs")

    task_definition_arn = ecs_client.register_task_definition(
        **BASE_TASK_DEFINITION, executionRoleArn="base"
    )["taskDefinition"]["taskDefinitionArn"]

    task = ECSTask(
        aws_credentials=aws_credentials,
        auto_deregister_task_definition=False,
        task_definition_arn=task_definition_arn,
        launch_type="EC2",
        **overrides,
    )
    print(task.preview())
    task_arn = await run_then_stop_task(task)

    task = describe_task(ecs_client, task_arn)
    assert (
        task["taskDefinitionArn"] != task_definition_arn
    ), "A new task definition should be registered"


@pytest.mark.usefixtures("ecs_mocks")
@pytest.mark.parametrize(
    "overrides",
    [
        {"env": {"FOO": "BAR"}},
        {"command": ["test"]},
        {"labels": {"FOO": "BAR"}},
        {"cpu": 2048},
        {"memory": 4096},
        {"execution_role_arn": "test"},
        {"stream_output": True, "configure_cloudwatch_logs": False},
        {"launch_type": "EXTERNAL"},
        {"cluster": "test"},
        {"task_role_arn": "test"},
        # Note: null environment variables can cause override, but not when missing
        # from the base task definition
        {"env": {"FOO": None}},
    ],
    ids=lambda item: str(set(item.keys())),
)
async def test_task_definition_arn_with_overrides_that_do_not_require_copy(
    aws_credentials, overrides
):
    """
    Any of these overrides should be configured at runtime and not require a new
    task definition to be registered
    """
    session = aws_credentials.get_boto3_session()
    ecs_client = session.client("ecs")

    if "cluster" in overrides:
        cluster_arn = create_test_ecs_cluster(ecs_client, overrides["cluster"])
        add_ec2_instance_to_ecs_cluster(session, overrides["cluster"])
    else:
        cluster_arn = "default"

    task_definition_arn = ecs_client.register_task_definition(**BASE_TASK_DEFINITION,)[
        "taskDefinition"
    ]["taskDefinitionArn"]

    # Set the default launch type for compatibility with the base task definition
    overrides.setdefault("launch_type", "EC2")

    task = ECSTask(
        aws_credentials=aws_credentials,
        auto_deregister_task_definition=False,
        task_definition_arn=task_definition_arn,
        image=None,
        **overrides,
    )
    print(task.preview())
    task_arn = await run_then_stop_task(task, cluster=cluster_arn)

    task = describe_task(ecs_client, task_arn, cluster=cluster_arn)
    assert (
        task["taskDefinitionArn"] == task_definition_arn
    ), "The existing task definition should be used"


@pytest.mark.usefixtures("ecs_mocks")
async def test_deregister_task_definition_does_not_apply_to_linked_arn(aws_credentials):
    session = aws_credentials.get_boto3_session()
    ecs_client = session.client("ecs")

    task_definition_arn = ecs_client.register_task_definition(**BASE_TASK_DEFINITION)[
        "taskDefinition"
    ]["taskDefinitionArn"]

    task = ECSTask(
        aws_credentials=aws_credentials,
        auto_deregister_task_definition=True,
        task_definition_arn=task_definition_arn,
        launch_type="EC2",
        image=None,
    )
    print(task.preview())
    task_arn = await run_then_stop_task(task)

    task = describe_task(ecs_client, task_arn)
    # The task definition can be retrieved still
    assert describe_task_definition(ecs_client, task)


@pytest.mark.usefixtures("ecs_mocks")
async def test_adding_security_groups_to_network_config(aws_credentials):
    session = aws_credentials.get_boto3_session()
    ec2_resource = session.resource("ec2")
    vpc = ec2_resource.create_vpc(CidrBlock="10.0.0.0/16")
    subnet = ec2_resource.create_subnet(CidrBlock="10.0.2.0/24", VpcId=vpc.id)
    ec2_client = session.client("ec2")
    security_group_id = ec2_client.create_security_group(
        GroupName="test", Description="testing"
    )["GroupId"]

    task = ECSTask(
        aws_credentials=aws_credentials,
        vpc_id=vpc.id,
        task_customizations=[
            {
                "op": "add",
                "path": "/networkConfiguration/awsvpcConfiguration/securityGroups",
                "value": [security_group_id],
            },
        ],
    )

    # Capture the task run call because moto does not track 'networkConfiguration'
    original_run_task = task._run_task
    mock_run_task = MagicMock(side_effect=original_run_task)
    task._run_task = mock_run_task

    print(task.preview())

    await run_then_stop_task(task)

    network_configuration = mock_run_task.call_args[0][1].get("networkConfiguration")

    # Subnet ids are copied from the vpc
    assert network_configuration == {
        "awsvpcConfiguration": {
            "subnets": [subnet.id],
            "securityGroups": [security_group_id],
            "assignPublicIp": "ENABLED",
        }
    }


@pytest.mark.usefixtures("ecs_mocks")
async def test_disable_public_ip_in_network_config(aws_credentials):
    session = aws_credentials.get_boto3_session()
    ec2_resource = session.resource("ec2")
    vpc = ec2_resource.create_vpc(CidrBlock="10.0.0.0/16")
    subnet = ec2_resource.create_subnet(CidrBlock="10.0.2.0/24", VpcId=vpc.id)

    task = ECSTask(
        aws_credentials=aws_credentials,
        vpc_id=vpc.id,
        task_customizations=[
            {
                "op": "replace",
                "path": "/networkConfiguration/awsvpcConfiguration/assignPublicIp",
                "value": "DISABLED",
            },
        ],
    )

    # Capture the task run call because moto does not track 'networkConfiguration'
    original_run_task = task._run_task
    mock_run_task = MagicMock(side_effect=original_run_task)
    task._run_task = mock_run_task

    print(task.preview())

    await run_then_stop_task(task)

    network_configuration = mock_run_task.call_args[0][1].get("networkConfiguration")

    # Subnet ids are copied from the vpc
    assert network_configuration == {
        "awsvpcConfiguration": {
            "subnets": [subnet.id],
            "assignPublicIp": "DISABLED",
        }
    }


@pytest.mark.usefixtures("ecs_mocks")
async def test_custom_subnets_in_the_network_configuration(aws_credentials):
    session = aws_credentials.get_boto3_session()
    ec2_resource = session.resource("ec2")
    vpc = ec2_resource.create_vpc(CidrBlock="10.0.0.0/16")
    subnet = ec2_resource.create_subnet(CidrBlock="10.0.2.0/24", VpcId=vpc.id)

    task = ECSTask(
        aws_credentials=aws_credentials,
        task_customizations=[
            {
                "op": "add",
                "path": "/networkConfiguration/awsvpcConfiguration/subnets",
                "value": [subnet.id],
            },
            {
                "op": "add",
                "path": "/networkConfiguration/awsvpcConfiguration/assignPublicIp",
                "value": "DISABLED",
            },
        ],
    )

    # Capture the task run call because moto does not track 'networkConfiguration'
    original_run_task = task._run_task
    mock_run_task = MagicMock(side_effect=original_run_task)
    task._run_task = mock_run_task

    print(task.preview())

    await run_then_stop_task(task)

    network_configuration = mock_run_task.call_args[0][1].get("networkConfiguration")

    # Subnet ids are copied from the vpc
    assert network_configuration == {
        "awsvpcConfiguration": {
            "subnets": [subnet.id],
            "assignPublicIp": "DISABLED",
        }
    }
