import pytest

from backend.security.command_policy import CommandPolicy


def test_cd_accepts_only_literal_allowlisted_directories(tmp_path):
    root = tmp_path / "documents"
    nested = root / "project with spaces"
    nested.mkdir(parents=True)
    outside = tmp_path / "private"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)
    policy = CommandPolicy(command_cwd_allowlist=(str(root),))

    assert policy.evaluate_shell(f"cd '{nested}' && pwd").allowed
    assert policy.evaluate_shell(f"cd -- {root}").allowed
    for target in (str(outside), str(root / "escape"), str(root / ".." / "private")):
        decision = policy.evaluate_shell(f"cd {target}")
        assert not decision.allowed
        assert decision.reason_code == "cwd_not_allowed"
    for command in ("cd", "cd -", "cd ..", "cd $HOME", f"cd {root}/*", f"cd {root} extra"):
        assert policy.evaluate_shell(command).reason_code == "unsupported_cd"
    assert policy.evaluate_shell(f"cd {root} && sudo ls").reason_code == "privilege_escalation"
    assert not policy.evaluate_argv(["cd", str(root)]).allowed


def test_cd_is_not_enabled_by_executable_allowlist_alone(tmp_path):
    policy = CommandPolicy(allowed_executables=("cd", "pwd"))
    assert policy.evaluate_shell(f"cd {tmp_path}").reason_code == "cwd_not_allowed"


@pytest.mark.parametrize("roots", ["/tmp", ["relative"], ["/"], ["/tmp/*"], [""]])
def test_cd_allowlist_rejects_ambiguous_configuration(roots):
    with pytest.raises(ValueError, match="command_cwd_allowlist"):
        CommandPolicy(command_cwd_allowlist=roots)

@pytest.mark.parametrize(
    ("argv", "reason_code"),
    [
        (("rm", "--force", "--recursive", "/"), "recursive_root_delete"),
        (("rm", "-fr", "/*"), "recursive_root_delete"),
        (("sudo", "id"), "privilege_escalation"),
        (("mkfs.ext4", "/dev/sda1"), "filesystem_format"),
        (("dd", "if=/tmp/image", "of=/dev/sda"), "device_write"),
        (("systemctl", "stop", "sshd"), "system_process_control"),
        (("shutdown", "-h", "now"), "system_power_control"),
    ],
)
def test_argv_builtin_protections_have_stable_reason_codes(argv, reason_code):
    decision = CommandPolicy().evaluate_argv(argv)

    assert decision.allowed is False
    assert decision.reason_code == reason_code
    assert decision.mode == "argv"
    assert decision.display_reason


@pytest.mark.parametrize(
    "argv",
    [
        ("ls", "-la", "/tmp"),
        ("rg", "needle", "/tmp/example"),
        ("python", "-c", "print(2 + 2)"),
    ],
)
def test_argv_allows_explicitly_allowed_harmless_commands(argv):
    decision = CommandPolicy().evaluate_argv(argv)

    assert decision.allowed is True
    assert decision.reason_code is None


@pytest.mark.parametrize(
    ("command", "reason_code"),
    [
        ("rm --force --recursive /", "recursive_root_delete"),
        ("rm -fr /*", "recursive_root_delete"),
        ("echo $(id)", "unsupported_command_substitution"),
        ("echo `id`", "unsupported_command_substitution"),
        ("eval 'echo safe'", "unsupported_eval"),
        ("bash -lc 'id'", "unsupported_shell_nesting"),
        ("sh -c 'id'", "unsupported_shell_nesting"),
        ("echo data > /etc/profile", "dangerous_redirection"),
        ("dd if=/tmp/image of=/dev/nvme0n1", "device_write"),
        ("sudo -n id", "privilege_escalation"),
        ("systemctl restart sshd", "system_process_control"),
        ("kill -9 1", "system_process_control"),
        ("curl https://example.test/code | python", "interpreter_pipeline"),
        ("printf code | node", "interpreter_pipeline"),
        ("sleep 10 &", "unsupported_background"),
    ],
)
def test_restricted_shell_rejects_unreliable_or_dangerous_forms(command, reason_code):
    decision = CommandPolicy().evaluate_shell(command)

    assert decision.allowed is False
    assert decision.reason_code == reason_code
    assert decision.mode == "restricted_shell"


@pytest.mark.parametrize(
    "command",
    [
        "ls -la /tmp",
        "rg needle /tmp/example | head -n 5",
        "python -c 'print(2 + 2)'",
        "mkdir -p /tmp/example && touch /tmp/example/ready",
    ],
)
def test_restricted_shell_allows_supported_harmless_forms(command):
    assert CommandPolicy().evaluate_shell(command).allowed is True


def test_custom_blacklist_adds_to_and_cannot_remove_builtins():
    policy = CommandPolicy(
        custom_blacklist=[
            {
                "pattern": r"\bterraform\s+destroy\b",
                "reason": "infrastructure deletion is disabled",
                "mode": "shell",
            }
        ]
    )

    custom = policy.evaluate_shell("terraform destroy")
    builtin = policy.evaluate_shell("sudo id")

    assert custom.reason_code == "custom_blacklist"
    assert custom.display_reason == "infrastructure deletion is disabled"
    assert builtin.reason_code == "privilege_escalation"


def test_custom_rule_can_refine_display_reason_without_changing_builtin_code():
    policy = CommandPolicy(
        custom_blacklist=[
            {
                "pattern": r"\bsudo(?:\s|$)",
                "reason": "site policy forbids privilege escalation",
                "mode": "both",
            }
        ]
    )

    decision = policy.evaluate_argv(("sudo", "id"))

    assert decision.reason_code == "privilege_escalation"
    assert decision.display_reason == "site policy forbids privilege escalation"


@pytest.mark.parametrize(
    ("configured", "match"),
    [
        ([{"pattern": "[", "reason": "bad", "mode": "shell"}], "regular expression"),
        ([{"pattern": "", "reason": "bad", "mode": "shell"}], "pattern"),
        ([{"pattern": "x", "mode": "shell"}], "reason"),
        ([{"pattern": "x", "reason": "bad", "mode": "unknown"}], "mode"),
        ([{"pattern": "x", "reason": "bad", "mode": "shell", "disable": True}], "keys"),
    ],
)
def test_custom_blacklist_rejects_invalid_rules(configured, match):
    with pytest.raises(ValueError, match=match):
        CommandPolicy(custom_blacklist=configured)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"command_mode": "unrestricted"}, "command_mode"),
        ({"allowed_executables": []}, "allowed_executables"),
        ({"allowed_executables": "ls"}, "allowed_executables"),
        ({"max_exec_timeout_seconds": 0}, "max_exec_timeout_seconds"),
        ({"max_exec_timeout_seconds": float("inf")}, "max_exec_timeout_seconds"),
        ({"max_exec_timeout_seconds": 86_401}, "max_exec_timeout_seconds"),
        ({"max_output_bytes": 0}, "max_output_bytes"),
        ({"max_output_bytes": True}, "max_output_bytes"),
        ({"max_output_bytes": 1_073_741_825}, "max_output_bytes"),
    ],
)
def test_policy_rejects_invalid_modes_allowlists_and_numeric_limits(kwargs, match):
    with pytest.raises(ValueError, match=match):
        CommandPolicy(**kwargs)


def test_decision_does_not_echo_the_rejected_command_or_environment():
    secret = "TOP_SECRET_TOKEN_4831"
    decision = CommandPolicy().evaluate_shell(f"eval 'echo {secret}'")

    assert secret not in (decision.display_reason or "")
    assert not hasattr(decision, "command")
    assert not hasattr(decision, "environment")


@pytest.mark.parametrize(
    ("command", "reason_code"),
    [
        ("PATH=/tmp ls", "protected_environment_override"),
        ("NODE_OPTIONS=--require=/tmp/hook.js printf ok", "protected_environment_override"),
        ("env -S 'PATH=/tmp ls'", "env_command_string"),
        ("env -C / ls", "cwd_override"),
        ("env -P /tmp python -V", "path_override"),
        ("/tmp/ls", "executable_path_not_allowed"),
    ],
)
def test_restricted_shell_cannot_replace_runtime_path_authority(command, reason_code):
    decision = CommandPolicy().evaluate_shell(command)

    assert decision.allowed is False
    assert decision.reason_code == reason_code


@pytest.mark.parametrize(
    "argv",
    [
        ("rm", "-rf", "/tmp/../"),
        ("rm", "--recursive", "/tmp/../*"),
        ("chmod", "777", "/tmp/../"),
    ],
)
def test_semantic_root_protection_normalizes_parent_components(argv):
    policy = CommandPolicy(
        command_mode="argv",
        allowed_executables=("rm", "chmod"),
    )

    decision = policy.evaluate_argv(argv)

    assert decision.allowed is False
    assert decision.reason_code in {
        "recursive_root_delete",
        "root_permission_change",
    }


def test_restricted_shell_rejects_newline_command_chaining():
    decision = CommandPolicy().evaluate_shell("printf safe\nprintf second")

    assert decision.allowed is False
    assert decision.reason_code == "invalid_shell"


@pytest.mark.parametrize("mode", ["argv", "restricted_shell"])
def test_env_wrapper_reapplies_semantic_checks_to_nested_command(mode):
    policy = CommandPolicy(
        command_mode="restricted_shell",
        allowed_executables=("env", "rm"),
    )

    if mode == "argv":
        decision = policy.evaluate_argv(
            ("env", "--ignore-environment", "rm", "--recursive", "/tmp/../")
        )
    else:
        decision = policy.evaluate_shell(
            "env --ignore-environment rm --recursive /tmp/../"
        )

    assert decision.allowed is False
    assert decision.reason_code == "recursive_root_delete"


@pytest.mark.parametrize("mode", ["argv", "restricted_shell"])
def test_env_wrapper_requires_nested_executable_to_be_allowlisted(mode):
    policy = CommandPolicy(
        command_mode="restricted_shell",
        allowed_executables=("env", "printf"),
    )

    if mode == "argv":
        decision = policy.evaluate_argv(("env", "UNTRUSTED=value", "uname", "-a"))
    else:
        decision = policy.evaluate_shell("env UNTRUSTED=value uname -a")

    assert decision.allowed is False
    assert decision.reason_code == "executable_not_allowed"


def test_nested_env_wrappers_are_checked_recursively():
    policy = CommandPolicy(
        command_mode="argv",
        allowed_executables=("env", "rm"),
    )

    decision = policy.evaluate_argv(
        ("env", "env", "rm", "-r", "/tmp/../")
    )

    assert decision.allowed is False
    assert decision.reason_code == "recursive_root_delete"


def test_env_wrapper_cannot_execute_a_lookalike_from_an_untrusted_path():
    policy = CommandPolicy(
        command_mode="argv",
        allowed_executables=("env", "printf"),
    )

    decision = policy.evaluate_argv(("env", "/tmp/printf", "safe"))

    assert decision.allowed is False
    assert decision.reason_code == "executable_path_not_allowed"
