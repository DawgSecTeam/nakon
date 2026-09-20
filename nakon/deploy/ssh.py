"""SSH transport: push a plan archive, run it, stream its output, bring the report back.

One exec per machine instead of one per configuration. That is a big win (a single sudo, a
single upload, no per-step round trip) but it introduces two problems this module solves:

1. **Silence.** A 20-minute apt run behind one blocking read looks hung. Output is streamed
   line by line as it arrives, with a heartbeat when a single step goes quiet.
2. **A stray password on stdin.** `sudo -S` reads the password from stdin, but under a
   NOPASSWD sudoers rule sudo never consumes it — and the line would then be inherited by
   whichever child reads stdin first. Mitigated three ways: `-p ''` so there is no prompt to
   match, `shutdown_write()` immediately after sending, and `exec </dev/null` at the top of
   the generated run.sh.
3. **A box that just booted.** sshd is socket-activated on cloud images and comes up a beat
   after the guest agent (or after a sshd_config change forces a restart), so the very first
   connect attempt right after a clone or a config push can lose the race — a bare TCP accept
   doesn't mean the daemon is ready for auth yet. `connect()` retries connection-level
   failures a few times with backoff.

The bootstrap is uploaded as a file rather than passed as a command string, so nothing has to
survive a trip through a remote shell's quoting — which also makes the Windows path (where
the default shell is cmd.exe) tractable.
"""

import posixpath
import select
import shlex
import socket
import time
import uuid

import paramiko

# Framing for the report, emitted by the bootstrap around the raw report.tsv contents.
REPORT_BEGIN = "##nakon report-begin"
REPORT_END = "##nakon report-end"
PLANDIR_MARKER = "##nakon plandir"

# How long a step may produce no output before we say it's still alive.
HEARTBEAT_SECONDS = 60

# Retries only cover a not-yet-ready daemon (connection refused/reset/timed out). A wrong
# password or host key must fail on the first attempt, not be retried into a slower failure.
CONNECT_ATTEMPTS = 4
CONNECT_RETRY_DELAYS = (2, 4, 8)

# Written by the generated run.sh (gen/bash.py NAKON_APT_CONF) and removed at the end of a clean run.
APT_CONF_FRAGMENT = "/etc/apt/apt.conf.d/99nakon-lock-timeout"

# Upper bound on the best-effort cleanup after a failed run, so it can never hang the deploy.
CLEANUP_TIMEOUT_SECONDS = 30


def connect(machine: dict, timeout: int = 30) -> paramiko.SSHClient:
    last_exc = None
    for attempt in range(CONNECT_ATTEMPTS):
        if attempt > 0:
            time.sleep(CONNECT_RETRY_DELAYS[attempt - 1])
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                hostname=machine["ip"],
                username=machine["user"],
                password=machine.get("password"),
                key_filename=machine.get("identity_file"),
                timeout=timeout,
                allow_agent=False,
                look_for_keys=bool(machine.get("identity_file")),
            )
        except (paramiko.AuthenticationException, paramiko.BadHostKeyException):
            # A credentials/host-key problem won't fix itself on a retry — fail fast.
            client.close()
            raise
        except (socket.timeout, socket.error, EOFError, paramiko.SSHException) as exc:
            # sshd not accepting connections yet (still booting / mid-restart). Retry.
            client.close()
            last_exc = exc
            continue
        else:
            # Team subnets route through the scoring engine's NAT; without keepalives a long
            # apt-get can outlive a conntrack entry and the channel dies silently.
            transport = client.get_transport()
            if transport is not None:
                transport.set_keepalive(30)
            return client
    raise last_exc


def put_file(client, local_path, remote_path, mode=0o600) -> None:
    """Upload a file and lock its permissions down.

    0600 matters: the archive is the complete list of vulnerabilities planted on this box,
    and it sits in a world-readable /tmp for the duration of the run.
    """
    sftp = client.open_sftp()
    try:
        sftp.put(str(local_path), remote_path)
        sftp.chmod(remote_path, mode)
    finally:
        sftp.close()


def render_bootstrap_sh(archive_remote: str, keep_remote: bool) -> str:
    """Generated bootstrap for a Linux target.

    Unpacks under /root (mode 0700) rather than somewhere durable like /opt/nakon: the plan
    is the answer key for this box, the competition's own setup grants the default user
    passwordless sudo, and team1's configured disk gets cloned to every other team. Leaving
    it behind would hand every blue team the list of planted vulnerabilities.
    """
    cleanup = "" if keep_remote else 'rm -rf -- "$D" ' + shlex.quote(archive_remote)
    kept = (
        f'echo "[nakon] plan kept at $D (--keep-remote)"'
        if keep_remote
        else 'echo "[nakon] plan directory removed"'
    )
    return f"""#!/bin/bash
# GENERATED BY nakon — bootstrap, deleted as it finishes.
exec </dev/null

ARCHIVE={shlex.quote(archive_remote)}

D="$(mktemp -d /root/nakon.XXXXXXXX)" || {{ echo "[nakon] cannot create plan dir" >&2; exit 1; }}
chmod 0700 "$D" || exit 1
echo "{PLANDIR_MARKER} $D"

if ! tar xzf "$ARCHIVE" -C "$D"; then
    echo "[nakon] failed to unpack $ARCHIVE" >&2
    rm -rf -- "$D"
    exit 1
fi

bash "$D/run.sh"
RUN_RC=$?

echo "{REPORT_BEGIN}"
[ -f "$D/report.tsv" ] && cat "$D/report.tsv"
echo "{REPORT_END}"

{cleanup}
{kept}
rm -f -- "$0"
exit $RUN_RC
"""


def render_bootstrap_ps1(archive_remote: str, keep_remote: bool) -> str:
    """Generated bootstrap for a Windows target.

    Verified end-to-end against real Windows Server 2022 boxes, including a full domain-join
    scenario (ADDS forest promotion + client join) driven through tezcatlipoca — see that
    project's `create-competition.py`/`deploy_windows_domain_configs()`.
    """
    cleanup = (
        "Write-Output \"[nakon] plan kept at $D (--keep-remote)\""
        if keep_remote
        else "Remove-Item -LiteralPath $D -Recurse -Force -ErrorAction SilentlyContinue\n"
             "Remove-Item -LiteralPath $Archive -Force -ErrorAction SilentlyContinue\n"
             "Write-Output \"[nakon] plan directory removed\""
    )
    return f"""# GENERATED BY nakon — bootstrap, deleted as it finishes.
$ErrorActionPreference = 'Continue'
$Archive = '{archive_remote}'
$D = Join-Path $env:TEMP ("nakon-" + [guid]::NewGuid().ToString('N').Substring(0,8))
New-Item -ItemType Directory -Path $D -Force | Out-Null
Write-Output "{PLANDIR_MARKER} $D"

try {{
    Expand-Archive -LiteralPath $Archive -DestinationPath $D -Force
}} catch {{
    Write-Error "[nakon] failed to unpack $Archive : $_"
    Remove-Item -LiteralPath $D -Recurse -Force -ErrorAction SilentlyContinue
    exit 1
}}

& powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $D 'run.ps1')
$RunRc = $LASTEXITCODE

Write-Output "{REPORT_BEGIN}"
$reportPath = Join-Path $D 'report.tsv'
if (Test-Path -LiteralPath $reportPath) {{ Get-Content -LiteralPath $reportPath }}
Write-Output "{REPORT_END}"

{cleanup}
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
exit $RunRc
"""


def remote_paths(platform: str) -> dict:
    """Where the archive and bootstrap land, per platform."""
    token = uuid.uuid4().hex[:12]
    if platform == "windows":
        base = "C:/Windows/Temp"
        return {
            "archive": f"{base}/nakon-{token}.zip",
            "bootstrap": f"{base}/nakon-boot-{token}.ps1",
        }
    return {
        "archive": posixpath.join("/tmp", f"nakon-{token}.tar.gz"),
        "bootstrap": posixpath.join("/tmp", f"nakon-boot-{token}.sh"),
    }


def bootstrap_command(platform: str, bootstrap_remote: str, use_sudo: bool = True) -> str:
    """The single command executed on the target."""
    if platform == "windows":
        # No sudo on Windows; run.ps1 refuses to proceed unless the SSH user is already
        # elevated, which is the only elevation mechanism available over SSH.
        return (
            "powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass "
            f"-File {bootstrap_remote}"
        )
    if use_sudo:
        # -p '' so there is no prompt string for anything downstream to mistake for input.
        return f"sudo -S -p '' bash {shlex.quote(bootstrap_remote)}"
    return f"bash {shlex.quote(bootstrap_remote)}"


def run_streaming(client, command, password, on_line, on_idle=None, idle_seconds=HEARTBEAT_SECONDS):
    """Execute a command, streaming output line by line. Returns its exit status.

    stderr is combined into stdout so the ordering an operator sees matches the ordering the
    marker parser sees.
    """
    transport = client.get_transport()
    channel = transport.open_session()
    channel.settimeout(None)
    channel.set_combine_stderr(True)
    channel.exec_command(command)

    if password is not None:
        try:
            channel.sendall(password + "\n")
        except Exception:
            # Under a NOPASSWD sudoers rule sudo never reads stdin, and the remote side may
            # have closed it already. Failing to deliver a password nobody asked for must not
            # fail the machine.
            pass
    # Close stdin immediately: whatever sudo didn't consume must not reach a child process.
    try:
        channel.shutdown_write()
    except Exception:
        pass

    buffer = b""
    last_output = time.monotonic()

    while True:
        readable, _, _ = select.select([channel], [], [], 1.0)
        if readable:
            try:
                chunk = channel.recv(65536)
            except Exception:
                break
            if not chunk:
                break
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            if lines:
                last_output = time.monotonic()
            for line in lines:
                on_line(line.decode("utf-8", "replace").rstrip("\r"))
        else:
            if on_idle is not None and time.monotonic() - last_output >= idle_seconds:
                on_idle(time.monotonic() - last_output)
                last_output = time.monotonic()
            if channel.exit_status_ready() and not channel.recv_ready():
                break

    if buffer:
        on_line(buffer.decode("utf-8", "replace").rstrip("\r"))

    return channel.recv_exit_status()


def force_cleanup(client, platform, paths, password, plan_dir=None):
    """Best-effort removal after a failed or interrupted run.

    The bootstrap deletes its own artifacts on a normal exit; this covers the case where the
    channel died first and the answer key would otherwise be left on the box.
    """
    targets = [paths["archive"], paths["bootstrap"]]
    try:
        if platform == "windows":
            items = ",".join(f"'{t}'" for t in targets + ([plan_dir] if plan_dir else []))
            command = (
                "powershell.exe -NoProfile -NonInteractive -Command "
                f"\"foreach ($p in @({items})) {{ Remove-Item -LiteralPath $p -Recurse -Force "
                "-ErrorAction SilentlyContinue }}\""
            )
            # Wait for it: the caller closes the client right after this returns, and an
            # exec_command that is still in flight when the transport goes away may never
            # run on the box — leaving the plan (the answer key) in C:\Windows\Temp.
            _, stdout, _ = client.exec_command(command, timeout=CLEANUP_TIMEOUT_SECONDS)
            stdout.channel.recv_exit_status()
            return
        # run.sh drops an apt.conf.d fragment for its own duration and removes it on a clean
        # finish; an interrupted run leaves it behind, so take it here too.
        quoted = " ".join(shlex.quote(t) for t in targets + [APT_CONF_FRAGMENT])
        plan_part = f" {shlex.quote(plan_dir)}" if plan_dir else ""
        stdin, stdout, _ = client.exec_command(
            f"sudo -S -p '' rm -rf -- {quoted}{plan_part}", timeout=CLEANUP_TIMEOUT_SECONDS
        )
        if password is not None:
            stdin.write(password + "\n")
            stdin.flush()
        stdin.channel.shutdown_write()
        stdout.read()
    except Exception:
        # Cleanup is best-effort by definition — never mask the original failure.
        pass
