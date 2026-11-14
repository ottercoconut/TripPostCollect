"""一次性 GitHub macOS VM 的原生测试控制层；绝不在私人宿主部署。"""

from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import pwd
import re
import signal
import socket
import stat
import subprocess
import sys
import time


# OS lane 的本地 Chromium 离线回放：headless shell 在联网准备阶段装入 PLAYWRIGHT_BROWSERS_PATH，
# 这里只把它的位置与回放开关交给测试进程，不转交其他宿主环境。
BROWSER_REPLAY_KEYS = ("PLAYWRIGHT_BROWSERS_PATH", "TPC_BROWSER_REPLAY")

# 一次性测试用户。59998 远离 macOS 系统账号（通常 < 500）和从 501 起的交互账号
# （GitHub 托管镜像上 runner 为 501）。创建前用 dscl 核对未被占用；占用则失败，不改选、不复用。
# staff 是 20，admin 是 80。不加入 admin / wheel，也不写 sudoers。
TEST_USER = "_tpcci"
TEST_UID = 59998
TEST_GID = 20
ANCHOR = "com.apple/trippostcollect-tests"
_USER_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SUDO_DENIAL = ("sudoers", "password", "not permitted", "not allowed", "operation not permitted")

# 目录项带继承，新建文件才会把读权限留给 runner；继承位加在普通文件上会被 chmod 拒绝。
_ACL_TRAVERSE = "execute"
_ACL_READ_DIR = "read,execute,file_inherit,directory_inherit"
_ACL_READ_FILE = "read,execute"
_ACL_WRITE_DIR = "read,write,execute,delete,add_file,add_subdirectory,file_inherit,directory_inherit"
_ACL_WRITE_FILE = "read,write,execute,delete"

# 子进程探针：0 连上，10 超时，11 其他套接字错误。超时以外的失败不能当成 PF 阻断。
_CONNECT_PROBE = (
    "import socket, sys\n"
    "family, host, port, timeout = sys.argv[1:5]\n"
    "client = socket.socket(int(family), socket.SOCK_STREAM)\n"
    "try:\n"
    "    client.settimeout(float(timeout))\n"
    "    client.connect((host, int(port)))\n"
    "except TimeoutError:\n"
    "    raise SystemExit(10)\n"
    "except OSError as exc:\n"
    "    print(type(exc).__name__, getattr(exc, 'errno', ''), file=sys.stderr)\n"
    "    raise SystemExit(11)\n"
    "else:\n"
    "    raise SystemExit(0)\n"
    "finally:\n"
    "    client.close()\n"
)


def browser_replay_environment():
    return {key: os.environ[key] for key in BROWSER_REPLAY_KEYS if os.environ.get(key)}


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


def supervisor_identity():
    uid = os.getuid()
    return {"uid": uid, "name": pwd.getpwuid(uid).pw_name}


def pf_policy(uid):
    """TCP/UDP 只匹配测试用户；ICMP 无法按用户匹配，只全局丢弃出站 echo 请求。"""
    return (
        f'block drop quick proto {{ tcp udp }} all user {uid} label "tpc_test_deny"\n'
        'block drop quick out inet proto icmp icmp-type echoreq label "tpc_icmp_echo_deny"\n'
        'block drop quick out inet6 proto icmp6 icmp6-type echoreq label "tpc_icmp_echo_deny"\n'
    )


def _rule_lines(text):
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if line.startswith("@") and " " in line:
            prefix, rest = line.split(None, 1)
            if prefix[1:].isdigit():
                line = rest.strip()
        if line.startswith("block"):
            yield line


def _is_echo_rule(line):
    return any(marker in line for marker in ("echoreq", "icmp-type 8", "icmp6-type 128"))


def assert_user_scoped(text, uid):
    """每条 block ... all 都必须带上这个 uid；出站 echo 是唯一允许的无 user 例外。"""
    marks = (f"user {uid}", f"user = {uid}", f"user={uid}")
    saw_user_rule = False
    for line in _rule_lines(text):
        head = line.split(" label ", 1)[0]
        has_user = any(mark in head for mark in marks)
        if re.search(r"\ball\b", head) and not has_user and not _is_echo_rule(line):
            raise RuntimeError(f"PF 含不带 user 的整机阻断: {line}")
        if has_user:
            saw_user_rule = True
            if "tpc_test_deny" not in line:
                raise RuntimeError(f"用户阻断规则缺少 tpc_test_deny: {line}")
            if "tcp" not in head and "udp" not in head:
                raise RuntimeError(f"用户阻断规则不是 TCP/UDP: {line}")
    if not saw_user_rule:
        raise RuntimeError("PF 未加载按用户收窄的阻断规则")


def assert_echo_only_icmp(text):
    icmp_lines = [line for line in _rule_lines(text) if "icmp" in line]
    if not any("icmp-type echoreq" in line or "icmp-type 8" in line for line in icmp_lines):
        raise RuntimeError("PF 缺少 IPv4 出站 echo 请求阻断")
    if not any("icmp6" in line and _is_echo_rule(line) for line in icmp_lines):
        raise RuntimeError("PF 缺少 IPv6 出站 echo 请求阻断")
    for line in icmp_lines:
        if not _is_echo_rule(line):
            raise RuntimeError(f"ICMP 规则超出 echo 请求: {line}")
        if " out " not in f" {line} ":
            raise RuntimeError(f"ICMP echo 阻断必须只针对出站: {line}")


def directory_uids(listing):
    found = {}
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lstrip("-").isdigit():
            found[parts[0]] = int(parts[-1])
    return found


def assert_no_sudo(user):
    """测试用户执行 sudo -n 必须失败。外层 sudo -u 已由调用方证明可用。"""
    try:
        admin("/usr/bin/sudo", "-n", "-u", user, "/usr/bin/sudo", "-n", "/usr/bin/true")
    except subprocess.CalledProcessError as exc:
        detail = f"{exc.stderr or ''}{exc.stdout or ''}"
        if any(marker in detail.lower() for marker in _SUDO_DENIAL):
            return
        raise RuntimeError(f"无法确认测试用户没有 sudo: {detail.strip()[:200]}") from exc
    raise RuntimeError("测试用户仍能 sudo")


def create_test_user():
    """创建一次性非登录用户。同名或目标 UID 已占用则失败，不复用，结束时不删除。

    用 dscl 写本地目录节点，不用 sysadminctl。sysadminctl -addUser 面向可登录账户：
    要口令、会在 /Users 下建家目录，无窗口的托管镜像上还可能牵涉 secure token。
    dscl 能直接写 UniqueID、PrimaryGroupID=staff、UserShell=/usr/bin/false 和禁用口令；
    root 的 sudo -u 不需要目标用户有登录 shell 或口令。
    """
    hosted_only()
    names = admin("/usr/bin/dscl", ".", "-list", "/Users").split()
    if TEST_USER in names:
        raise RuntimeError(f"测试用户 {TEST_USER} 已存在，拒绝复用")
    used = directory_uids(admin("/usr/bin/dscl", ".", "-list", "/Users", "UniqueID"))
    if TEST_UID in used.values():
        owner = next(name for name, uid in used.items() if uid == TEST_UID)
        raise RuntimeError(f"UID {TEST_UID} 已被 {owner} 占用")
    node = f"/Users/{TEST_USER}"
    attributes = (
        ("UniqueID", str(TEST_UID)),
        ("PrimaryGroupID", str(TEST_GID)),
        ("UserShell", "/usr/bin/false"),
        ("RealName", "TripPostCollect CI"),
        ("NFSHomeDirectory", "/var/empty"),
        ("Password", "*"),
    )
    admin("/usr/bin/dscl", ".", "-create", node)
    for name, value in attributes:
        admin("/usr/bin/dscl", ".", "-create", node, name, value)
    # chmod 的 user: 只认账户名。刷新目录缓存，避免刚写入的用户还解析不到。
    admin("/usr/bin/dscacheutil", "-flushcache")
    switched = int(admin("/usr/bin/sudo", "-n", "-u", TEST_USER, "/usr/bin/id", "-u").strip())
    gid = int(admin("/usr/bin/sudo", "-n", "-u", TEST_USER, "/usr/bin/id", "-g").strip())
    groups = admin("/usr/bin/sudo", "-n", "-u", TEST_USER, "/usr/bin/id", "-Gn").split()
    if switched != TEST_UID or gid != TEST_GID or "admin" in groups or "wheel" in groups:
        raise RuntimeError(f"测试用户身份不符合预期: uid={switched} gid={gid} groups={groups}")
    assert_no_sudo(TEST_USER)
    return {
        "name": TEST_USER,
        "uid": switched,
        "gid": gid,
        "groups": groups,
        "shell": "/usr/bin/false",
        "home": "/var/empty",
        "deleted": False,
        "deletion": "not_attempted_vm_disposable",
    }


def acl_skipped(path):
    """uchg 文件改不了 ACL。已经对其他用户可读则跳过，否则失败。"""
    info = Path(path).lstat()
    immutable = getattr(stat, "UF_IMMUTABLE", 0)
    if not immutable or not (getattr(info, "st_flags", 0) & immutable):
        return False
    if info.st_mode & stat.S_IROTH and (not stat.S_ISDIR(info.st_mode) or info.st_mode & stat.S_IXOTH):
        return True
    raise RuntimeError(f"不可变文件对测试用户不可读: {path}")


def iter_acl_paths(root):
    """不跟随符号链接，避免把 ACL 加到 $TPC_WORK 之外的解释器或浏览器目标上。"""
    root = Path(root)
    if root.is_symlink():
        return
    if not acl_skipped(root):
        yield root
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        kept = []
        for name in dirnames:
            path = Path(current) / name
            if path.is_symlink():
                continue
            kept.append(name)
            if not acl_skipped(path):
                yield path
        dirnames[:] = kept
        for name in filenames:
            path = Path(current) / name
            if path.is_symlink() or acl_skipped(path):
                continue
            yield path


def change_acl(user, path, permissions):
    subprocess.run(
        ["/bin/chmod", "+a", f"user:{user} allow {permissions}", os.fspath(path)],
        check=True, capture_output=True, text=True, timeout=30,
    )


def _acl_permissions(path, kind):
    directory = Path(path).is_dir()
    if kind == "traverse":
        return _ACL_TRAVERSE
    if kind == "read":
        return _ACL_READ_DIR if directory else _ACL_READ_FILE
    if kind == "write":
        return _ACL_WRITE_DIR if directory else _ACL_WRITE_FILE
    raise RuntimeError(f"未知授权类型: {kind}")


def _require_inside(path, work):
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(work):
        raise RuntimeError(f"授权路径越出 $TPC_WORK: {resolved}")
    return resolved


def grant_test_user_access(user, command, source, output, environment):
    """只改 $TPC_WORK 之内的 ACL。mktemp -d 的工作区是 0700，测试用户否则进不去。"""
    source_path = _require_inside(source, Path(source).resolve().parent)
    output_path = Path(output).resolve()
    work = source_path.parent
    if output_path.parent.parent != work:
        raise RuntimeError("源码副本与报告目录不在同一 $TPC_WORK")
    python = Path(command[0])
    if not python.is_absolute():
        raise RuntimeError("测试解释器必须是绝对路径")
    # parent 不解析符号链接：venv/bin/python 常指向工作区外的 uv Python，不能跟着走。
    venv = python.absolute().parent.parent
    browsers = Path(environment["PLAYWRIGHT_BROWSERS_PATH"])
    home = Path(environment["HOME"])
    tmpdir = Path(environment["TMPDIR"])
    readable = (source_path, _require_inside(venv, work), _require_inside(browsers, work))
    writable = (
        _require_inside(output_path, work),
        _require_inside(home, work),
        _require_inside(tmpdir, work),
    )
    if not home.resolve().is_relative_to(output_path) or not tmpdir.resolve().is_relative_to(output_path):
        raise RuntimeError("HOME 与 TMPDIR 必须在 OS lane 输出目录内")
    for path in (*readable, *writable):
        if not path.exists():
            raise RuntimeError(f"授权路径不存在: {path}")
    planned = {}

    def remember(account, path, kind):
        resolved = _require_inside(path, work)
        if Path(path).is_symlink():
            raise RuntimeError(f"拒绝跟随符号链接授权: {path}")
        key = (str(account), resolved)
        rank = {"traverse": 1, "read": 2, "write": 3}[kind]
        current = planned.get(key)
        if current is None or rank > current[0]:
            planned[key] = (rank, kind, resolved)

    remember(user, work, "traverse")
    for root in (*readable, *writable):
        cursor = root.parent
        while cursor != work:
            remember(user, cursor, "traverse")
            cursor = cursor.parent
    for root in readable:
        for path in iter_acl_paths(root):
            remember(user, path, "read")
    for root in writable:
        for path in iter_acl_paths(root):
            remember(user, path, "write")
    # chmod 不接受数字 uid（Unable to translate '<uid>' to a UUID），用账户名。
    runner = supervisor_identity()["name"]
    for root in writable:
        for path in iter_acl_paths(root):
            remember(runner, path, "read")
    for (account, _), (_, kind, path) in planned.items():
        change_acl(account, path, _acl_permissions(path, kind))
    interpreter = python.resolve()
    return {
        "work": str(work),
        "readable": [str(path) for path in readable],
        "writable": [str(path) for path in writable],
        "entries": len(planned),
        "interpreter": str(python),
        "interpreter_outside_work": not interpreter.is_relative_to(work),
    }


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


def _accept(server):
    try:
        connection, _ = server.accept()
    except TimeoutError as exc:
        raise RuntimeError("监听者未接到已报告的连接") from exc
    connection.close()


def verify_connected(servers):
    observations = []
    for family, server in servers:
        with socket.socket(family, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(server.getsockname())
            _accept(server)
        observations.append({"family": family.name, "connected": True})
    return observations


def checked_counters(anchor):
    """按用户收窄的 tpc_test_deny 必须有包计数。ICMP 规则上的计数不能代替。"""
    rules = pf("-a", anchor, "-vvsr")
    on_user_rule = False
    counted = False
    for raw in rules.splitlines():
        line = raw.strip()
        if line.startswith("@") and " " in line:
            prefix, rest = line.split(None, 1)
            if prefix[1:].isdigit():
                line = rest.strip()
        if "tpc_test_deny" in line and "user" in line:
            on_user_rule = True
        elif line.startswith("block") or line.startswith("pass"):
            on_user_rule = False
        match = re.search(r"Packets:\s*(\d+)", line)
        if on_user_rule and match and int(match.group(1)) > 0:
            counted = True
    if not counted:
        raise RuntimeError("PF 无实际阻断计数，不能把超时当成成功")
    return rules


def listeners(stack):
    """只建立监听者。连通基线必须由测试用户发起，supervisor 自己连上不能证明阻断。"""
    result = []
    for family, address in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        server = stack.enter_context(socket.socket(family, socket.SOCK_STREAM))
        server.bind((address, 0))
        server.listen(16)
        server.settimeout(2)
        result.append((family, server))
    return result


def as_user_command(user, command, environment):
    if not _USER_NAME.fullmatch(user or ""):
        raise RuntimeError("测试用户名非法")
    assignments = []
    for key, value in environment.items():
        if not _ENV_NAME.fullmatch(str(key)):
            raise RuntimeError(f"非法环境变量名: {key}")
        text = str(value)
        if "\n" in text or "\0" in text:
            raise RuntimeError(f"环境变量含换行: {key}")
        assignments.append(f"{key}={text}")
    return ["/usr/bin/sudo", "-n", "-u", user, "/usr/bin/env", "-i", *assignments, *map(str, command)]


def run_as_user(user, argv, environment, *, timeout):
    hosted_only()
    return subprocess.run(
        as_user_command(user, argv, environment),
        check=False, capture_output=True, text=True, timeout=timeout,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
    )


def _probe_outcome(user, environment, family, server, *, timeout):
    host, port = server.getsockname()[:2]
    result = run_as_user(
        user,
        [sys.executable, "-c", _CONNECT_PROBE, str(int(family)), host, str(port), str(timeout)],
        environment,
        timeout=timeout + 15,
    )
    if result.returncode == 0:
        return "connected"
    if result.returncode == 10:
        return "timeout"
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    tail = detail[-1] if detail else ""
    raise RuntimeError(f"测试用户探针失败 exit={result.returncode} {tail}")


def _probe_record(user, family, server, **fields):
    return {"family": family.name, "user": user, "address": list(server.getsockname()), **fields}


def verify_user_open(user, environment, servers):
    """部署前基线：测试用户必须能连上仍存活的回环监听者。"""
    observations = []
    for family, server in servers:
        outcome = _probe_outcome(user, environment, family, server, timeout=2)
        if outcome != "connected":
            raise RuntimeError("部署前测试用户未能连上已建立的监听者")
        _accept(server)
        observations.append(_probe_record(user, family, server, connected=True))
    return observations


def verify_user_drop(user, environment, servers):
    """负例只能从测试用户发起。ECONNREFUSED 和其他错误不能当成 drop。"""
    observations = []
    for family, server in servers:
        outcome = _probe_outcome(user, environment, family, server, timeout=1)
        if outcome != "timeout":
            raise RuntimeError("PF 未阻止测试用户连接已验证监听者")
        observations.append(_probe_record(
            user, family, server, baseline="connected", restricted="timeout", listener_alive=True,
        ))
    return observations


def verify_runner_open(servers):
    """规则生效期间，supervisor（runner 用户）连接同一监听者必须成功。"""
    identity = supervisor_identity()
    observations = []
    for family, server in servers:
        with socket.socket(family, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            try:
                client.connect(server.getsockname())
            except TimeoutError as exc:
                raise RuntimeError("规则生效期间 runner 用户连接被阻断") from exc
            _accept(server)
        observations.append({
            "family": family.name,
            "uid": identity["uid"],
            "user": identity["name"],
            "connected": True,
            "address": list(server.getsockname()),
        })
    return observations


def assert_pf_enabled():
    info = pf("-s", "info")
    if "Status: Enabled" not in info:
        raise RuntimeError("执行期 PF 未启用")
    return info


def verify_active(evidence):
    """子进程只证明当前用户连不上监听者。规则文本、启用状态和计数由 supervisor 核对。"""
    expected = evidence.get("test_user", {}).get("uid")
    if expected is None or os.getuid() != expected:
        raise RuntimeError("执行期负例探针不在测试用户中")
    probes = evidence.get("probes") or []
    if not probes:
        raise RuntimeError("执行期负例探针缺少监听者")
    for probe in probes:
        with socket.socket(getattr(socket, probe["family"]), socket.SOCK_STREAM) as client:
            client.settimeout(1)
            try:
                client.connect(tuple(probe["address"]))
            except TimeoutError:
                continue
            raise RuntimeError("执行期 PF 负例连接成功")


def test_process_snapshot(timeout):
    """Strict, bounded observation; an unreadable table is never an empty group."""
    result = subprocess.run(["/bin/ps", "-axo", "pid=,pgid=,lstart="],
                            check=True, capture_output=True, text=True, timeout=timeout,
                            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
    rows = {}
    for line in result.stdout.splitlines():
        pid, pgid, started = line.split(maxsplit=2)
        rows[int(pid)] = (int(pgid), started)
    if not rows:
        raise RuntimeError("CI process observation returned no processes")
    return rows


def signal_group(pgid, signum):
    """跨用户收束不能用 os.killpg（EPERM）。经 admin() 发信号，从而保留 hosted_only()。"""
    name = signal.Signals(signum).name.removeprefix("SIG")
    try:
        admin("/bin/kill", "-s", name, "--", f"-{int(pgid)}")
    except subprocess.CalledProcessError as exc:
        detail = f"{exc.stderr or ''}{exc.stdout or ''}"
        if "No such process" in detail:
            return
        raise


def run_test_command(command, source, environment, log, *, user, cleanup=None,
                     observe=None, clock=None, sleep=None):
    """以测试用户启动。sudo 会清空环境，所以用 env -i 显式传入 lane 环境。

    start_new_session 让直接子进程（sudo）成为会话和进程组首领。身份核对仍要求
    ps 里该 PID 的 PGID 等于 PID。sudo 是否把测试用户命令留在同一 PGID，只能在
    托管 VM 上用这次核对证明；不成立时收束失败并保留 PF。
    """
    hosted_only()
    observe = observe or test_process_snapshot
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    cleanup = cleanup if cleanup is not None else {}
    cleanup.update(confirmed=False, status="unconfirmed")
    child = subprocess.Popen(
        as_user_command(user, command, environment),
        cwd=source,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
    )
    owner = None
    try:
        owner = observe(5).get(child.pid)
        if owner is None or owner[0] != child.pid:
            raise RuntimeError("Cannot establish owned CI process group identity")
        return child.wait(timeout=1800)
    finally:
        try:
            if owner is None or owner[0] != child.pid:
                raise RuntimeError("CI process group ownership unconfirmed")

            def members(deadline):
                child.poll()  # Reap the direct child, but do not infer group exit.
                rows = observe(max(0.001, deadline - clock()))
                if child.pid in rows and rows[child.pid] != owner:
                    raise RuntimeError("CI process group identity changed")
                return [pid for pid, identity in rows.items() if identity[0] == child.pid]

            deadline = clock() + 5
            remaining = members(deadline)
            for signum in (signal.SIGTERM, signal.SIGKILL):
                if not remaining:
                    break
                # Only the session/PGID created above is owned. In this trusted
                # ephemeral VM descendants must remain in that test group.
                signal_group(child.pid, signum)
                deadline = clock() + 5
                while True:
                    remaining = members(deadline)
                    if not remaining or clock() >= deadline:
                        break
                    sleep(min(0.1, deadline - clock()))
            if remaining:
                raise RuntimeError("CI test group still present after SIGKILL")
            cleanup.update(confirmed=True, status="group_exited")
        except BaseException as exc:
            cleanup.update(status="cleanup_failed", error_type=type(exc).__name__)
            raise


def _remember_failure(evidence, exc):
    evidence["supervisor_error"] = type(exc).__name__
    detail = str(exc)
    extra = f"{getattr(exc, 'stderr', None) or ''}{getattr(exc, 'stdout', None) or ''}".strip()
    if extra:
        detail = f"{detail} {extra}"
    evidence["supervisor_error_summary"] = detail[:300]


def run_isolated(command, source, output, environment, log):
    hosted_only()
    environment = {**environment, **browser_replay_environment()}
    anchor = ANCHOR
    evidence = {
        "mechanism": "pf+python-execution-guard",
        "run_id": os.environ["GITHUB_RUN_ID"],
        "restored": False,
        "runner_user": supervisor_identity(),
    }
    evidence_path = Path(output) / "os-policy.json"
    # ExitStack restores every completed mutation even when the next step fails.
    # SIGTERM from Actions cancellation enters this same finally path.
    def interrupted(signum, frame):
        raise RuntimeError(f"CI supervisor interrupted: {signum}")

    previous = signal.signal(signal.SIGTERM, interrupted)
    try:
        try:
            user = create_test_user()
            evidence["test_user"] = user
            if user["uid"] == evidence["runner_user"]["uid"]:
                raise RuntimeError("测试用户与 runner 用户是同一个 uid")
            evidence["access_grants"] = grant_test_user_access(
                user["name"], command, source, output, environment)
            original_root = pf("-sr")
            original_enabled = "Status: Enabled" in pf("-s", "info")
            if pf("-a", anchor, "-sr").strip():
                raise RuntimeError("临时 PF anchor 已被占用")
            submitted = pf_policy(user["uid"])
            assert_user_scoped(submitted, user["uid"])
            assert_echo_only_icmp(submitted)
            evidence["pf_policy_submitted"] = submitted
            policy = Path(output) / "native.pf"
            policy.write_text(submitted)
            pf("-n", "-a", anchor, "-f", str(policy))
            with ExitStack() as sockets, ExitStack() as stack:
                servers = listeners(sockets)
                evidence["test_user_baseline"] = verify_user_open(user["name"], environment, servers)
                evidence["temporary_dispatcher"] = prepare_anchor(stack, output, anchor)
                # -E supplies a reference token; -X releases only our enable request.
                hosted_only()
                enabled = subprocess.run(
                    ["/usr/bin/sudo", "-n", "/sbin/pfctl", "-E"],
                    capture_output=True, text=True, check=True, timeout=30,
                )
                match = re.search(r"Token\s*:\s*(\d+)", enabled.stdout + enabled.stderr)
                if not match:
                    raise RuntimeError("PF 未提供 enable token，无法安全恢复")
                stack.callback(pf, "-X", match.group(1))
                stack.callback(pf, "-a", anchor, "-F", "rules")
                pf("-a", anchor, "-f", str(policy))
                loaded = pf("-a", anchor, "-sr")
                assert_user_scoped(loaded, user["uid"])
                assert_echo_only_icmp(loaded)
                evidence["pf_policy"] = loaded
                evidence["test_user_negative"] = verify_user_drop(user["name"], environment, servers)
                evidence["probes"] = evidence["test_user_negative"]
                evidence["runner_positive"] = verify_runner_open(servers)
                if any(item["uid"] == user["uid"] for item in evidence["runner_positive"]):
                    raise RuntimeError("runner 正例与测试用户是同一个 uid")
                evidence["pf_enabled_before"] = "Status: Enabled" in assert_pf_enabled()
                evidence["pf_rules_before"] = checked_counters(anchor)
                evidence["verified_live_listeners"] = True
                evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
                try:
                    evidence["test_cleanup"] = {"confirmed": False, "status": "unconfirmed"}
                    returncode = run_test_command(
                        command, source, environment, log,
                        user=user["name"], cleanup=evidence["test_cleanup"],
                    )
                    evidence["returncode"] = returncode
                    evidence["test_user_negative_after"] = verify_user_drop(
                        user["name"], environment, servers)
                    evidence["post_probes"] = evidence["test_user_negative_after"]
                    evidence["runner_positive_after"] = verify_runner_open(servers)
                    evidence["pf_enabled_after"] = "Status: Enabled" in assert_pf_enabled()
                    evidence["pf_rules_after"] = checked_counters(anchor)
                    guard = json.loads((Path(output) / "execution-guard.json").read_text())
                    if (guard.get("mechanism") != "python-execution-guard"
                            or len(guard.get("observations", [])) != 6):
                        raise RuntimeError("缺少 pytest 执行守卫实测记录")
                    evidence["execution_guard"] = guard
                finally:
                    if not evidence["test_cleanup"]["confirmed"]:
                        # 收束未确认时保留 PF。规则只匹配测试用户，runner 与 GitHub 的连接不受影响。
                        stack.pop_all()
                        evidence["restoration_status"] = "blocked_until_vm_destruction"
                        raise RuntimeError("CI cleanup unconfirmed; PF retained until VM destruction")
                    stack.close()
                    evidence["restored_probes"] = verify_connected(servers)
                    if pf("-sr") != original_root or pf("-a", anchor, "-sr").strip():
                        raise RuntimeError("PF 规则未恢复原状态")
                    if ("Status: Enabled" in pf("-s", "info")) != original_enabled:
                        raise RuntimeError("PF enable 状态未恢复")
                    evidence["restored"] = True
            return returncode
        except BaseException as exc:
            _remember_failure(evidence, exc)
            raise
    finally:
        signal.signal(signal.SIGTERM, previous)
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
