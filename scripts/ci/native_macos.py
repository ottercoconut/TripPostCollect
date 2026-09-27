"""一次性 GitHub macOS VM 的原生测试控制层；绝不在私人宿主部署。"""

from __future__ import annotations

from contextlib import ExitStack
import json
import os
import re
import signal
import socket
import subprocess
import sys


def hosted_only():
    if not (sys.platform == "darwin" and os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
            and os.environ.get("GITHUB_RUN_ID") and os.environ.get("ImageOS", "").startswith("macos")):
        raise RuntimeError("管理员操作仅允许一次性 GitHub-hosted macOS VM")


def admin(*arguments):
    hosted_only()  # Check at every privileged operation, including restoration.
    return subprocess.run(["/usr/bin/sudo", "-n", *arguments], check=True,
                          capture_output=True, text=True, timeout=30).stdout


def pf(*arguments):
    return admin("/sbin/pfctl", *arguments)


def prepare_anchor(stack, output, anchor):
    """只复用已加载的 dispatcher，或在证明空的 VM 上临时加载最小规则。"""
    root = pf("-sr")
    if 'anchor "com.apple/*"' in root:
        return False
    if root.strip() or any(pf(*query).strip() for query in
                           (("-sn",), ("-s", "Anchors"), ("-s", "Tables"),
                            ("-s", "states"))):
        raise RuntimeError("PF 存在未知策略或状态，拒绝替换")
    empty = output / "original-empty.pf"
    empty.write_text("")
    dispatcher = output / "dispatcher.pf"
    dispatcher.write_text(f'anchor "{anchor}" all\n')
    pf("-n", "-f", str(dispatcher))
    stack.callback(pf, "-f", str(empty))
    pf("-f", str(dispatcher))
    if f'anchor "{anchor}"' not in pf("-sr"):
        raise RuntimeError("临时 PF dispatcher 未加载")
    return True


def verify_connected(servers):
    observations = []
    for family, server in servers:
        with socket.socket(family, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(server.getsockname())
            connection, _ = server.accept()
            connection.close()
        observations.append({"family": family.name, "connected": True})
    return observations


def checked_counters(anchor):
    rules = pf("-a", anchor, "-vvsr")
    counters = re.findall(r"Packets:\s*(\d+)", rules)
    if not counters or not any(int(value) > 0 for value in counters):
        raise RuntimeError("PF 无实际阻断计数，不能把超时当成成功")
    return rules


def listeners(stack):
    result = []
    for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        server = stack.enter_context(socket.socket(family, socket.SOCK_STREAM))
        server.bind((address, 0))
        server.listen(16)
        server.settimeout(2)
        # Baseline is required: ECONNREFUSED/timeouts without a known live
        # destination do not prove PF filtering.
        with socket.socket(family, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(server.getsockname())
            connection, _ = server.accept()
            connection.close()
        result.append((family, server))
    return result


def verify_drop(servers):
    observations = []
    for family, server in servers:
        with socket.socket(family, socket.SOCK_STREAM) as client:
            client.settimeout(1)
            try:
                client.connect(server.getsockname())
            except TimeoutError:
                observations.append({"family": family.name, "baseline": "connected",
                                     "address": list(server.getsockname()),
                                     "restricted": "timeout", "listener_alive": True})
            else:
                raise RuntimeError("PF 未阻止已验证监听者的连接")
    return observations


def verify_active(evidence):
    """Recheck the active kernel rule in the lane child."""
    hosted_only()
    rules = pf("-a", "com.apple/trippostcollect-tests", "-vvsr")
    if "tpc_test_deny" not in rules or "block drop" not in rules:
        raise RuntimeError("执行期 PF 规则不存在")
    if "Status: Enabled" not in pf("-s", "info"):
        raise RuntimeError("执行期 PF 未启用")
    for probe in evidence["probes"]:
        with socket.socket(getattr(socket, probe["family"]), socket.SOCK_STREAM) as client:
            client.settimeout(1)
            try:
                client.connect(tuple(probe["address"]))
            except TimeoutError:
                continue
            raise RuntimeError("执行期 PF 负例连接成功")


def run_test_command(command, source, environment, log):
    child = subprocess.Popen(command, cwd=source, env=environment, stdout=log,
                             stderr=subprocess.STDOUT, start_new_session=True)
    try:
        return child.wait(timeout=1800)
    finally:
        # Stop our own still-live process group before releasing the controls.
        # Never use a supplied PID or signal an unrelated host process.
        if child.poll() is None:
            if os.getpgid(child.pid) != child.pid:
                raise RuntimeError("CI child process group changed unexpectedly")
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)


def run_isolated(command, source, output, environment, log):
    hosted_only()
    anchor = "com.apple/trippostcollect-tests"
    evidence = {"mechanism": "pf+python-execution-guard",
                "run_id": os.environ["GITHUB_RUN_ID"], "restored": False}
    evidence_path = output / "os-policy.json"
    original_root = pf("-sr")
    original_enabled = "Status: Enabled" in pf("-s", "info")
    if pf("-a", anchor, "-sr").strip():
        raise RuntimeError("临时 PF anchor 已被占用")
    policy = output / "native.pf"
    policy.write_text('block drop quick all label "tpc_test_deny"\n')
    pf("-n", "-a", anchor, "-f", str(policy))
    # ExitStack restores every completed mutation even when the next step fails.
    # SIGTERM from Actions cancellation enters this same finally path.
    def interrupted(signum, frame):
        raise RuntimeError(f"CI supervisor interrupted: {signum}")

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        with ExitStack() as sockets, ExitStack() as stack:
            servers = listeners(sockets)
            evidence["temporary_dispatcher"] = prepare_anchor(stack, output, anchor)
            # -E supplies a reference token; -X releases only our enable request.
            hosted_only()
            enabled = subprocess.run(["/usr/bin/sudo", "-n", "/sbin/pfctl", "-E"],
                                     capture_output=True, text=True, check=True, timeout=30)
            match = re.search(r"Token\s*:\s*(\d+)", enabled.stdout + enabled.stderr)
            if not match:
                raise RuntimeError("PF 未提供 enable token，无法安全恢复")
            stack.callback(pf, "-X", match.group(1))
            stack.callback(pf, "-a", anchor, "-F", "rules")
            pf("-a", anchor, "-f", str(policy))
            evidence["probes"] = verify_drop(servers)
            evidence["pf_rules_before"] = checked_counters(anchor)
            evidence["verified_live_listeners"] = True
            evidence_path.write_text(json.dumps(evidence, indent=2))
            try:
                returncode = run_test_command(command, source, environment, log)
                evidence["post_probes"] = verify_drop(servers)
                evidence["pf_rules_after"] = checked_counters(anchor)
                guard = json.loads((output / "execution-guard.json").read_text())
                if guard.get("mechanism") != "python-execution-guard" or len(guard.get("observations", [])) != 6:
                    raise RuntimeError("缺少 pytest 执行守卫实测记录")
                evidence["execution_guard"] = guard
            finally:
                stack.close()
                evidence["restored_probes"] = verify_connected(servers)
                if pf("-sr") != original_root or pf("-a", anchor, "-sr").strip():
                    raise RuntimeError("PF 规则未恢复原状态")
                if ("Status: Enabled" in pf("-s", "info")) != original_enabled:
                    raise RuntimeError("PF enable 状态未恢复")
                evidence["restored"] = True
        return returncode
    finally:
        signal.signal(signal.SIGTERM, previous)
        evidence_path.write_text(json.dumps(evidence, indent=2))
