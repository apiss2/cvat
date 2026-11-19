# SPDX-License-Identifier: MIT

import argparse
import ast
from pathlib import Path

import pytest
from cvat_cli._internal.commands_all import COMMANDS

MODELS_PATH = Path(__file__).resolve().parents[3] / "cvat/apps/engine/models.py"


def server_choices(enum_name):
    # Read the server's source without importing Django or starting the server.
    module = ast.parse(MODELS_PATH.read_text(encoding="utf-8"), filename=str(MODELS_PATH))
    enum_class, = (
        node for node in module.body if isinstance(node, ast.ClassDef) and node.name == enum_name
    )
    values = {
        ast.literal_eval(node.value)
        for node in enum_class.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
    }
    assert values and all(isinstance(value, str) for value in values), enum_name
    return values


@pytest.mark.parametrize(
    ("option", "enum_name"), [("--stage", "StageChoice"), ("--state", "StateChoice")]
)
def test_job_update_choices_match_server(option, enum_name):
    parser = argparse.ArgumentParser()
    COMMANDS.commands["job"].commands["update"].configure_parser(parser)
    action, = (action for action in parser._actions if option in action.option_strings)

    server = server_choices(enum_name)
    cli = set(action.choices)
    assert cli == server, (
        f"{option}: missing from CLI: {sorted(server - cli)}; "
        f"not supported by server: {sorted(cli - server)}"
    )
