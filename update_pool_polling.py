# -*- coding: utf-8 -*-
# 兜底：每 60 秒比对一次 Dialer1 当前 IP 与地址池 section，不一致才更新。
# 比对只用两条用户视图 display（只读），平时不进 system-view。
# 与 update_pool.py 独立部署，compare-then-update 幂等。
# 平时静默；只有真正要写入时（很少，通常是事件脚本错过了，比如开机时）才逐步记日志：
# more logfile/log.log | include update_poll
import ops
import re

POOL_NAME  = "p2p_pool"
SECTION_ID = "0"
DIALER     = "Dialer1"
LOG        = "update_poll"
SECTION_RE = re.compile(r"section\s+%s\s+(\d+\.\d+\.\d+\.\d+)" % SECTION_ID,
                        re.IGNORECASE)
DIALER_RE  = re.compile(r"%s\s+(\d+\.\d+\.\d+\.\d+)" % DIALER)


def run(_ops, handle, cmd, trace):
    out, nxt, desc = _ops.cli.execute(handle, cmd)
    out = out or ""
    if trace:
        _ops.syslog("%s cmd=%r desc=%r next=%r out=%r" %
                    (LOG, cmd, desc, nxt, out.strip()[-200:]))
    return out, desc


def read_value(_ops, handle, cmd, regex, trace):
    """Run a display command and extract the first regex group; log the matched line."""
    out, desc = run(_ops, handle, cmd, False)
    m = regex.search(out)
    if trace:
        line = next((l.strip() for l in out.splitlines() if regex.search(l)), None)
        _ops.syslog("%s read %r: desc=%r line=%r" %
                    (LOG, cmd, desc, line or "no match, out=%r" % out.strip()[-300:]))
    return m.group(1) if m else None


def get_dialer_ip(_ops, handle, trace=False):
    ip = read_value(_ops, handle, "display ip interface brief %s" % DIALER, DIALER_RE, trace)
    return None if ip in (None, "0.0.0.0") else ip


def get_section_ip(_ops, handle, trace=False):
    return read_value(_ops, handle, "display nat address-group name %s" % POOL_NAME,
                      SECTION_RE, trace)


def write_section(_ops, handle, new_ip, trace):
    """Rewrite the section and read it back; the read-back decides the result.
    Returns (ok, error) where error is the first failed command, if any."""
    error = None
    for cmd in ("system-view",
                "nat address-group %s" % POOL_NAME,
                "undo section %s" % SECTION_ID,
                "section %s %s %s" % (SECTION_ID, new_ip, new_ip),
                "return"):
        _, desc = run(_ops, handle, cmd, trace)
        if desc != "Success" and not cmd.startswith("undo section"):
            error = "%s (%s)" % (cmd, desc)
            run(_ops, handle, "return", trace)
            break
    # 以回读结果为准：另一个脚本恰好同时在写时，本次的命令可能报错，但 section 已经是对的
    return get_section_ip(_ops, handle, trace) == new_ip, error


def ops_condition(_ops):
    _ops.timer.relative("tick", 60)
    _ops.correlate("tick")
    return 0


def ops_execute(_ops):
    _ops.set_model_type("YANG")
    handle, _ = _ops.cli.open()
    if handle is None:
        return 0

    try:
        new_ip = get_dialer_ip(_ops, handle)
        if not new_ip:
            return 0
        current = get_section_ip(_ops, handle)
        if current == new_ip:
            return 0

        _ops.syslog("%s start: section=%r %s=%r" % (LOG, current, DIALER, new_ip))
        ok, error = write_section(_ops, handle, new_ip, True)
        if ok:
            _ops.syslog("%s done: %s %s -> %s%s" %
                        (LOG, DIALER, current, new_ip,
                         " (%s failed, but the section is already correct)" % error if error else ""))
        else:
            _ops.syslog("%s FAILED: %s %s -> %s, failed at %s" %
                        (LOG, DIALER, current, new_ip, error or "verify"))
    finally:
        _ops.cli.close(handle)
    return 0
