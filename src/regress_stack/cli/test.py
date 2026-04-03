# Copyright 2025 - Canonical Ltd
# SPDX-License-Identifier: GPL-3.0-only

import click
import importlib.util
import logging
import os
import json
import pathlib
import subprocess
import sys
from configparser import ConfigParser

import regress_stack.modules
from regress_stack.core import apt as core_apt
from regress_stack.core import utils
from regress_stack.core.modules import get_execution_order
from regress_stack.modules import keystone
from regress_stack.modules import utils as module_utils
from regress_stack.cli.utils import collect_logs

LOG = logging.getLogger(__name__)


TEMPESTCONF_USERS = ("demo_tempestconf", "alt_demo_tempestconf")
DISCOVER_TEMPEST_OVERRIDES = (
    "volume.catalog_type",
    "volumev3",
)


def _tool_cmd(name: str, module: str | None = None) -> tuple[str, list[str]]:
    """Prefer executables from the active interpreter environment."""
    tool_path = pathlib.Path(sys.executable).with_name(name)
    if tool_path.exists():
        return str(tool_path), []
    if module is not None:
        return sys.executable, ["-m", module]
    return name, []


def _pip_install_cmd() -> tuple[str, list[str]]:
    """Return a working pip invocation for installing a compatibility wheel."""
    if importlib.util.find_spec("pip") is not None:
        return sys.executable, [
            "-m",
            "pip",
            "install",
            "python-tempestconf",
            "--break-system-packages",
        ]

    try:
        utils.run(sys.executable, ["-m", "ensurepip", "--upgrade"])
    except (subprocess.CalledProcessError, FileNotFoundError):
        LOG.info("Could not bootstrap pip with ensurepip for %s", sys.executable)
    else:
        if importlib.util.find_spec("pip") is not None:
            return sys.executable, [
                "-m",
                "pip",
                "install",
                "python-tempestconf",
                "--break-system-packages",
            ]

    return "/usr/bin/python3", [
        "-m",
        "pip",
        "install",
        "python-tempestconf",
        "--break-system-packages",
    ]


def _ensure_tempestconf_compat():
    """Install python-tempestconf from PyPI if it triggers an argparse conflict.

    The Ubuntu package of config_tempest calls openstack.connect(argparse=...).
    Newer openstacksdk removed 'argparse' from get_cloud_region()'s explicit
    parameters, so it lands in **kwargs. When get_cloud_region() then calls
    get_one(argparse=parsed_options, **kwargs), Python raises
    "multiple values for keyword argument 'argparse'".

    The fix is a newer python-tempestconf that no longer passes argparse= to
    openstack.connect().
    """
    import inspect
    import openstack.config as osc_config

    if "argparse" not in inspect.signature(osc_config.get_cloud_region).parameters:
        utils.warn_workaround(
            "python-tempestconf argparse conflict",
            "installing python-tempestconf from PyPI to fix openstack.connect argparse conflict",
        )
        cmd, args = _pip_install_cmd()
        utils.run(cmd, args)


def _cleanup_tempestconf_users() -> None:
    """Remove stale TempestConf users left by a partial previous run."""
    conn = keystone.o7k()
    domain_id = keystone.default_domain()
    for username in TEMPESTCONF_USERS:
        users = list(conn.identity.users(name=username, domain_id=domain_id))
        for user in users:
            utils.warn_workaround(
                "stale TempestConf identity resources",
                f"deleting leftover user {username} before regenerating tempest config",
            )
            conn.identity.delete_user(user, ignore_missing=True)


def _sync_tempest_workspace(workspace_dir: pathlib.Path) -> None:
    """Keep an existing Tempest workspace aligned with the active venv."""
    import tempest.cmd.init as tempest_init

    stestr_conf = workspace_dir / ".stestr.conf"
    top_dir = pathlib.Path(tempest_init.__file__).resolve().parent.parent
    expected = {
        "test_path": str(top_dir / "test_discover"),
        "top_dir": str(top_dir),
        "group_regex": r"([^\.]*\.)*",
    }

    if not stestr_conf.exists():
        parser = ConfigParser()
        parser["DEFAULT"] = expected
        with stestr_conf.open("w") as fh:
            parser.write(fh)
        return

    parser = ConfigParser()
    parser.read(stestr_conf)
    current = parser["DEFAULT"]
    if all(current.get(key) == value for key, value in expected.items()):
        return

    LOG.info("Repairing Tempest workspace config in %s", stestr_conf)
    parser["DEFAULT"] = expected
    with stestr_conf.open("w") as fh:
        parser.write(fh)


@click.command()
@click.option(
    "--concurrency",
    type=str,
    default="1",
    callback=lambda ctx, param, value: utils.concurrency_cb(value)
    if value != "1"
    else 1,
    help="The number of workers to use, defaults to 1. The value 'auto' sets concurrency to number of cpus / 3.",
)
@click.option(
    "--retry-failed",
    type=int,
    default=0,
    help="Number of times to retry failed tests, defaults to 0 (no retries).",
)
@utils.measure_time
def test(concurrency, retry_failed):
    """Run the regression tests using Tempest."""

    # NOTE(freyes): use PPA to fix http://pad.lv/2141604 if needed.
    if core_apt.PkgVersionCompare("python3-tempestconf") < "3.5.1-1ubuntu1~cloud0":
        core_apt.add_ppa("ppa:freyes/lp2141604")
        utils.run("apt", ["install", "-yq", "--only-upgrade", "python3-tempestconf"])
    _ensure_tempestconf_compat()
    _cleanup_tempestconf_users()
    env = os.environ.copy()
    env.update(keystone.auth_env())
    dir_name = "mycloud01"
    tempest_cmd, tempest_prefix = _tool_cmd("tempest")
    discover_cmd, discover_prefix = _tool_cmd(
        "discover-tempest-config", "config_tempest.main"
    )
    stestr_cmd, stestr_prefix = _tool_cmd("stestr")
    workspaces = json.loads(
        utils.run(tempest_cmd, [*tempest_prefix, "workspace", "list", "--format", "json"])
    )
    workspaces = [ws["Name"] for ws in workspaces]
    if dir_name in workspaces:
        LOG.info("Tempest workspace %s already exists, skipping init", dir_name)
    else:
        utils.run(tempest_cmd, [*tempest_prefix, "init", dir_name])
    _sync_tempest_workspace(pathlib.Path(dir_name))

    image_url = utils.ubuntu_cloud_image_url()
    LOG.info("Using Tempest image %s", image_url)
    utils.run(
        discover_cmd,
        [
            *discover_prefix,
            "--create",
            "--flavor-min-mem",
            "1024",
            "--flavor-min-disk",
            "5",
            "--image",
            image_url,
            *DISCOVER_TEMPEST_OVERRIDES,
        ],
        env=env,
        cwd=dir_name,
    )
    tempest_conf = pathlib.Path(dir_name) / "etc" / "tempest.conf"
    module_utils.cfg_set(
        str(tempest_conf),
        ("validation", "image_ssh_user", "ubuntu"),
        ("validation", "image_alt_ssh_user", "ubuntu"),
    )

    test_regexes = []
    for mod in get_execution_order(regress_stack.modules):
        if not utils.is_setup_done(mod.name):
            LOG.info("Skipping %s", mod.name)
            continue
        if configure := getattr(mod.module, "configure_tempest", None):
            with utils.measure("configure_tempest " + mod.name):
                configure(tempest_conf)
        includes_regexes = getattr(mod.module, "TEST_INCLUDE_REGEXES", [])
        exclude_regexes = getattr(mod.module, "TEST_EXCLUDE_REGEXES", [])
        test_regexes.append((includes_regexes, exclude_regexes))

    test_regexes.append(
        (
            os.environ.get("TEST_INCLUDE_REGEXES", "").split("|"),
            os.environ.get("TEST_EXCLUDE_REGEXES", "").split("|"),
        )
    )

    LOG.info("Building test list")
    global_include_regex = ["smoke"]
    global_exclude_regex = []

    for include_regexes, exclude_regexes in test_regexes:
        if include_regexes and include_regexes[0]:
            global_include_regex.append("|".join(include_regexes))
        if exclude_regexes and exclude_regexes[0]:
            global_exclude_regex.append("|".join(exclude_regexes))

    regress_tests = utils.run(
        tempest_cmd,
        [
            *tempest_prefix,
            "run",
            "--list",
            "--regex",
            "|".join(global_include_regex),
            "--exclude-regex",
            "|".join(global_exclude_regex),
        ],
        env=env,
        cwd=dir_name,
    )

    regress_list = pathlib.Path(dir_name) / "regress_tests.txt"
    regress_list.write_text(regress_tests)

    load_list = str(regress_list.relative_to(dir_name))
    subprocess.run(
        [
            tempest_cmd,
            *tempest_prefix,
            "run",
            "--load-list",
            load_list,
            "--concurrency",
            str(concurrency),
        ],
        check=True,
        env=env,
        cwd=dir_name,
    )

    retries = 0
    successful_run = False
    while retry_failed >= retries and not successful_run:
        try:
            with utils.banner("Fetching failing tests"):
                utils.run(stestr_cmd, [*stestr_prefix, "failing", "--list"], cwd=dir_name)
                successful_run = True
        except subprocess.CalledProcessError:
            retries += 1
            # Collect logs after the last retry to avoid collecting logs
            # multiple times in case of multiple retries.
            if retries > retry_failed:
                collect_logs()
                raise
            else:
                LOG.warning(
                    "Failed to fetch failing tests, retrying (%d/%d)",
                    retries,
                    retry_failed,
                )
                utils.system(
                    f"stestr run --failing --concurrency {concurrency}",
                    env,
                    dir_name,
                )
