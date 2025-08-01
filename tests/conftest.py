import filelock
import json
import os
import pathlib
import subprocess
import tempfile
import time
from typing import List

import pytest
import requests
import yaml

from smoke_tests.docker import docker_utils
from sqlalchemy import exc as sqlalchemy_exc
from sqlalchemy import orm
from sqlalchemy import text as sqlalchemy_text
import sqlalchemy_adapter

from sky import global_user_state
from sky import sky_logging

# Initialize logger at the top level
logger = sky_logging.init_logger(__name__)

# We need to import all the mock functions here, so that the smoke
# tests can access them.
from common_test_fixtures import aws_config_region
from common_test_fixtures import enable_all_clouds
from common_test_fixtures import mock_aws_backend
from common_test_fixtures import mock_client_requests
from common_test_fixtures import mock_controller_accessible
from common_test_fixtures import mock_job_table_no_job
from common_test_fixtures import mock_job_table_one_job
from common_test_fixtures import mock_queue
from common_test_fixtures import mock_redirect_log_file
from common_test_fixtures import mock_services_no_service
from common_test_fixtures import mock_services_one_service
from common_test_fixtures import mock_stream_utils
from common_test_fixtures import reset_global_state
from common_test_fixtures import skyignore_dir

from sky.server import common as server_common

# Usage: use
#   @pytest.mark.slow
# to mark a test as slow and to skip by default.
# https://docs.pytest.org/en/latest/example/simple.html#control-skipping-of-tests-according-to-command-line-option

# By default, only run generic tests and cloud-specific tests for AWS and Azure,
# due to the cloud credit limit for the development account.
#
# A "generic test" tests a generic functionality (e.g., autostop) that
# should work on any cloud we support. The cloud used for such a test
# is controlled by `--generic-cloud` (typically you do not need to set it).
#
# To only run tests for a specific cloud (as well as generic tests), use
# --aws, --gcp, --azure, or --lambda.
#
# To only run tests for managed jobs (without generic tests), use
# --managed-jobs.
all_clouds_in_smoke_tests = [
    'aws', 'gcp', 'azure', 'lambda', 'cloudflare', 'ibm', 'scp', 'oci', 'do',
    'kubernetes', 'vsphere', 'cudo', 'fluidstack', 'paperspace', 'runpod',
    'vast', 'nebius', 'hyperbolic'
]
default_clouds_to_run = ['aws', 'azure']

# Translate cloud name to pytest keyword. We need this because
# @pytest.mark.lambda is not allowed, so we use @pytest.mark.lambda_cloud
# instead.
cloud_to_pytest_keyword = {
    'aws': 'aws',
    'gcp': 'gcp',
    'azure': 'azure',
    'lambda': 'lambda_cloud',
    'cloudflare': 'cloudflare',
    'ibm': 'ibm',
    'scp': 'scp',
    'oci': 'oci',
    'kubernetes': 'kubernetes',
    'vsphere': 'vsphere',
    'runpod': 'runpod',
    'fluidstack': 'fluidstack',
    'cudo': 'cudo',
    'paperspace': 'paperspace',
    'do': 'do',
    'vast': 'vast',
    'runpod': 'runpod',
    'nebius': 'nebius',
    'hyperbolic': 'hyperbolic'
}

def represent_path(dumper: yaml.Dumper, data: pathlib.PurePath) -> yaml.ScalarNode:
    path_as_string = str(data).replace('\\', '/')
    return dumper.represent_scalar('tag:yaml.org,2002:str', path_as_string)


for path_type in (pathlib.WindowsPath, pathlib.PureWindowsPath):
    for dumper in (yaml.Dumper, yaml.SafeDumper):
        dumper.add_representer(path_type, represent_path)

def pytest_addoption(parser):
    # tests marked as `slow` will be skipped by default, use --runslow to run
    parser.addoption('--runslow',
                     action='store_true',
                     default=False,
                     help='run slow tests.')
    for cloud in all_clouds_in_smoke_tests:
        parser.addoption(f'--{cloud}',
                         action='store_true',
                         default=False,
                         help=f'Only run {cloud.upper()} tests.')
    parser.addoption('--managed-jobs',
                     action='store_true',
                     default=False,
                     help='Only run tests for managed jobs.')
    parser.addoption('--serve',
                     action='store_true',
                     default=False,
                     help='Only run tests for sky serve.')
    parser.addoption('--tpu',
                     action='store_true',
                     default=False,
                     help='Only run tests for TPU.')
    parser.addoption(
        '--generic-cloud',
        type=str,
        choices=all_clouds_in_smoke_tests,
        help='Cloud to use for generic tests. If the generic cloud is '
             'not within the clouds to be run, it will be reset to the first '
             'cloud in the list of the clouds to be run.')

    parser.addoption('--terminate-on-failure',
                     dest='terminate_on_failure',
                     action='store_true',
                     default=True,
                     help='Terminate test VMs on failure.')
    parser.addoption('--no-terminate-on-failure',
                     dest='terminate_on_failure',
                     action='store_false',
                     help='Do not terminate test VMs on failure.')
    parser.addoption(
        '--remote-server',
        action='store_true',
        default=False,
        help='Run tests against a remote server in Docker container.')
    # Custom options for backward compatibility tests
    parser.addoption(
        '--need-launch',
        action='store_true',
        default=False,
        help='Whether to launch clusters in tests',
    )
    parser.addoption(
        '--base-branch',
        type=str,
        default='master',
        help='Base branch to test backward compatibility against',
    )
    parser.addoption(
        '--controller-cloud',
        type=str,
        default=None,
        help='Controller cloud to use for tests',
    )
    parser.addoption(
        '--postgres',
        action='store_true',
        default=False,
        help='Run tests for Postgres Backend',
    )
    parser.addoption(
        '--no-resource-heavy',
        action='store_true',
        default=False,
        help='Skip tests marked as resource_heavy',
    )
    parser.addoption(
        '--helm-version',
        type=str,
        default='',
        help='Version of Helm to use for tests',
    )
    parser.addoption(
        '--helm-package',
        type=str,
        default='',
        help='Package name to use for Helm tests',
    )
    parser.addoption(
        '--jobs-consolidation',
        action='store_true',
        default=False,
        help=('If set, the tests will be run in jobs consolidation mode '
              '(The config change is made in buildkite so this is a flag to '
              'ensure the tests will not be skipped but no actual effect)'),
    )


def pytest_configure(config):
    config.addinivalue_line('markers', 'slow: mark test as slow to run')
    config.addinivalue_line('markers',
                            'local: mark test to run only on local API server')
    for cloud in all_clouds_in_smoke_tests:
        cloud_keyword = cloud_to_pytest_keyword[cloud]
        config.addinivalue_line(
            'markers', f'{cloud_keyword}: mark test as {cloud} specific')

    # Validate incompatible option combinations
    if config.getoption('--remote-server'):
        if config.getoption('--jobs-consolidation'):
            raise ValueError(
                '--remote-server and --jobs-consolidation are not compatible. '
                'Jobs consolidation mode is not supported with remote server testing.'
            )
        if config.getoption('--postgres'):
            raise ValueError(
                '--remote-server and --postgres are not compatible. '
                'Postgres backend is not supported with remote server testing.')

    pytest.terminate_on_failure = config.getoption('--terminate-on-failure')


def _get_cloud_to_run(config) -> List[str]:
    cloud_to_run = []

    for cloud in all_clouds_in_smoke_tests:
        if config.getoption(f'--{cloud}'):
            if cloud == 'cloudflare':
                cloud_to_run.append(default_clouds_to_run[0])
            else:
                cloud_to_run.append(cloud)

    generic_cloud_option = config.getoption('--generic-cloud')
    if generic_cloud_option is not None and generic_cloud_option not in cloud_to_run:
        cloud_to_run.append(generic_cloud_option)

    if len(cloud_to_run) == 0:
        cloud_to_run = default_clouds_to_run

    return cloud_to_run


def pytest_collection_modifyitems(config, items):
    skip_marks = {}
    skip_marks['slow'] = pytest.mark.skip(reason='need --runslow option to run')
    skip_marks['managed_jobs'] = pytest.mark.skip(
        reason='skipped, because --managed-jobs option is set')
    skip_marks['serve'] = pytest.mark.skip(
        reason='skipped, because --serve option is set')
    skip_marks['tpu'] = pytest.mark.skip(
        reason='skipped, because --tpu option is set')
    skip_marks['local'] = pytest.mark.skip(
        reason='test requires local API server')
    skip_marks['no_resource_heavy'] = pytest.mark.skip(
        reason='skipped, because --no-resource-heavy option is set')
    for cloud in all_clouds_in_smoke_tests:
        skip_marks[cloud] = pytest.mark.skip(
            reason=f'tests for {cloud} is skipped, try setting --{cloud}')
    skip_marks['postgres'] = pytest.mark.skip(
        reason='skipped, because --postgres option is set')

    cloud_to_run = _get_cloud_to_run(config)
    generic_cloud = _generic_cloud(config)
    generic_cloud_keyword = cloud_to_pytest_keyword[generic_cloud]

    for item in items:
        if 'smoke_tests' not in item.location[0]:
            # Only mark smoke test cases
            continue
        if 'slow' in item.keywords and not config.getoption('--runslow'):
            item.add_marker(skip_marks['slow'])
        if 'local' in item.keywords and not server_common.is_api_server_local():
            item.add_marker(skip_marks['local'])
        if _is_generic_test(
                item) and f'no_{generic_cloud_keyword}' in item.keywords:
            item.add_marker(skip_marks[generic_cloud])
        for cloud in all_clouds_in_smoke_tests:
            cloud_keyword = cloud_to_pytest_keyword[cloud]
            if (cloud_keyword in item.keywords and cloud not in cloud_to_run):
                # Need to check both conditions as the first default cloud is
                # added to cloud_to_run when tested for cloudflare
                if config.getoption('--cloudflare') and cloud == 'cloudflare':
                    continue
                item.add_marker(skip_marks[cloud])

        if (not 'managed_jobs'
                in item.keywords) and config.getoption('--managed-jobs'):
            item.add_marker(skip_marks['managed_jobs'])
        if (not 'tpu' in item.keywords) and config.getoption('--tpu'):
            item.add_marker(skip_marks['tpu'])
        if (not 'serve' in item.keywords) and config.getoption('--serve'):
            item.add_marker(skip_marks['serve'])
        if ('no_postgres' in item.keywords) and config.getoption('--postgres'):
            item.add_marker(skip_marks['postgres'])

        # Skip tests marked as resource_heavy if --no-resource-heavy is set
        marks = [mark.name for mark in item.iter_markers()]
        if 'resource_heavy' in marks and config.getoption(
                '--no-resource-heavy'):
            item.add_marker(skip_marks['no_resource_heavy'])

    # Check if tests need to be run serially for Kubernetes and Lambda Cloud
    # We run Lambda Cloud tests serially because Lambda Cloud rate limits its
    # launch API to one launch every 10 seconds.
    # We run Kubernetes tests serially because the Kubernetes cluster may have
    # limited resources (e.g., just 8 cpus).
    serial_mark = pytest.mark.xdist_group(
        name=f'serial_{generic_cloud_keyword}')
    # Handle generic tests
    if generic_cloud in ['lambda']:
        for item in items:
            if (_is_generic_test(item) and
                    f'no_{generic_cloud_keyword}' not in item.keywords):
                item.add_marker(serial_mark)
                # Adding the serial mark does not update the item.nodeid,
                # but item.nodeid is important for pytest.xdist_group, e.g.
                #   https://github.com/pytest-dev/pytest-xdist/blob/master/src/xdist/scheduler/loadgroup.py
                # This is a hack to update item.nodeid
                item._nodeid = f'{item.nodeid}@serial_{generic_cloud_keyword}'
    # Handle generic cloud specific tests
    for item in items:
        if generic_cloud in ['lambda', 'kubernetes']:
            if generic_cloud_keyword in item.keywords:
                item.add_marker(serial_mark)
                item._nodeid = f'{item.nodeid}@serial_{generic_cloud_keyword}'  # See comment on item.nodeid above

    if config.option.collectonly:
        for item in items:
            full_name = item.nodeid
            marks = [mark.name for mark in item.iter_markers()]
            print(f"Collected {full_name} with marks: {marks}")


def _is_generic_test(item) -> bool:
    for cloud in all_clouds_in_smoke_tests:
        if cloud_to_pytest_keyword[cloud] in item.keywords:
            return False
    return True


def _generic_cloud(config) -> str:
    generic_cloud_option = config.getoption('--generic-cloud')
    if generic_cloud_option is not None:
        return generic_cloud_option
    return _get_cloud_to_run(config)[0]


@pytest.fixture
def generic_cloud(request) -> str:
    return _generic_cloud(request.config)


@pytest.fixture(scope='session', autouse=True)
def setup_docker_container(request):
    """Setup Docker container for remote server testing if --remote-server is specified."""
    if not request.config.getoption('--remote-server'):
        yield
        return

    # Set environment variable to indicate we're using remote server
    os.environ['PYTEST_SKYPILOT_REMOTE_SERVER_TEST'] = '1'

    # Docker image and container names
    dockerfile_path = 'tests/smoke_tests/docker/Dockerfile_test'
    default_user = os.environ.get('USER', 'buildkite')

    # Create a lockfile and counter file in a temporary directory that all processes can access
    lock_file = os.path.join(tempfile.gettempdir(), 'sky_docker_setup.lock')
    counter_file = os.path.join(tempfile.gettempdir(),
                                'sky_docker_workers.txt')
    lock = filelock.FileLock(lock_file)

    try:
        with lock:
            # This synchronized block handles the initial setup.
            try:
                with open(counter_file, 'r') as f:
                    worker_count = int(f.read().strip())
            except (FileNotFoundError, ValueError):
                worker_count = 0

            worker_count += 1
            with open(counter_file, 'w') as f:
                f.write(str(worker_count))

            # Check if container is already running (another worker might have started it)
            try:
                # Use docker ps with filter to check for running container
                result = subprocess.run([
                    'docker', 'ps', '--filter',
                    f'name={docker_utils.get_container_name()}', '--format',
                    '{{.Names}}'
                ],
                    check=True,
                    capture_output=True,
                    text=True)
                if docker_utils.get_container_name() in result.stdout:
                    # Container is already up, no need to do more setup.
                    yield docker_utils.get_container_name()
                    return
            except subprocess.CalledProcessError:
                pass

            # Use docker images with filter to check for existing image
            result = subprocess.run([
                'docker', 'images', '--filter',
                f'reference={docker_utils.IMAGE_NAME}', '--format',
                '{{.Repository}}'
            ],
                check=True,
                capture_output=True,
                text=True)
            if docker_utils.IMAGE_NAME in result.stdout:
                logger.info(
                    f'Docker image {docker_utils.IMAGE_NAME} already exists')
            else:
                in_container = docker_utils.is_inside_docker()

                if in_container:
                    # We're inside a container, so we can't build the Docker image
                    raise Exception(
                        f'Docker image {docker_utils.IMAGE_NAME} must be built on '
                        f'the host first when running inside a container. Please '
                        f'run \'docker build -t {docker_utils.IMAGE_NAME} '
                        f'--build-arg USERNAME={default_user} -f '
                        f'tests/smoke_tests/docker/Dockerfile_test .\' on the host '
                        'machine.')
                else:
                    logger.info(
                        f'Docker image {docker_utils.IMAGE_NAME} not found, building...'
                    )
                    subprocess.run([
                        'docker', 'build', '-t', docker_utils.IMAGE_NAME,
                        '--build-arg', f'USERNAME={default_user}', '-f',
                        dockerfile_path, '.'
                    ],
                        check=True)
                    logger.info(
                        f'Successfully built Docker image {docker_utils.IMAGE_NAME}'
                    )

            # Start new container
            logger.info(
                f'Starting Docker container {docker_utils.get_container_name()}...'
            )

            # Use create_and_setup_new_container to create and start the container
            docker_utils.create_and_setup_new_container(
                target_container_name=docker_utils.get_container_name(),
                host_port=docker_utils.get_host_port(),
                container_port=46580,
                username=default_user)

            logger.info(
                f'Container {docker_utils.get_container_name()} started')

            # Wait for container to be ready
            logger.info('Waiting for container to be ready...')
            url = docker_utils.get_api_server_endpoint_inside_docker()
            health_endpoint = f'{url}/api/health'
            max_retries = 40
            retry_count = 0

            while retry_count < max_retries:
                try:
                    response = requests.get(health_endpoint)
                    response.raise_for_status()

                    # Parse JSON response
                    if response.json().get('status') == 'healthy':
                        logger.info('Container is ready!')
                        break

                    retry_count += 1
                    time.sleep(1)
                except Exception as e:
                    logger.error(
                        f'Error connecting to container: {e}, retrying...')
                    retry_count += 1
                    time.sleep(10)
            else:
                raise Exception(
                    'Container failed to start properly - health check did not pass'
                )

        # The lock is released before yielding to the tests.
        yield docker_utils.get_container_name()

    except Exception as e:
        logger.exception(f'Error in Docker setup: {e}')
        raise
    finally:
        # This synchronized block handles the final cleanup.
        with lock:
            # Decrement worker counter and cleanup if this is the last worker
            try:
                with open(counter_file, 'r') as f:
                    worker_count = int(f.read().strip())
            except (FileNotFoundError, ValueError):
                # If the counter file is gone, assume we are the last one.
                worker_count = 1

            worker_count -= 1
            with open(counter_file, 'w') as f:
                f.write(str(worker_count))

            if worker_count == 0:
                logger.info('Last worker finished, cleaning up container...')
                subprocess.run([
                    'docker', 'stop', '-t', '600',
                    docker_utils.get_container_name()
                ],
                    check=False)
                subprocess.run(
                    ['docker', 'rm',
                     docker_utils.get_container_name()],
                    check=False)
                try:
                    os.remove(counter_file)
                except OSError:
                    pass


@pytest.fixture(scope='session', autouse=True)
def setup_controller_cloud_env(request):
    """Setup controller cloud environment variable if --controller-cloud is specified."""
    if not request.config.getoption('--controller-cloud'):
        yield
        return

    # Set environment variable to indicate we're using remote server
    controller_cloud = request.config.getoption('--controller-cloud')
    os.environ['PYTEST_SKYPILOT_CONTROLLER_CLOUD'] = controller_cloud
    yield controller_cloud


@pytest.fixture(scope='session', autouse=True)
def setup_postgres_backend_env(request):
    """Setup Postgres Backend environment variable if --postgres is specified."""
    if not request.config.getoption('--postgres'):
        yield
        return
    os.environ['PYTEST_SKYPILOT_POSTGRES_BACKEND'] = '1'
    yield
