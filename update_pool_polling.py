# -*- coding: utf-8 -*-
# 兜底：每 60 秒比对一次 Dialer1 当前 IP 与地址池 section，不一致才更新。
# 比对只用两条用户视图 display（只读），平时不进 system-view。
# 与 update_pool.py 独立部署，compare-then-update 幂等。
# 只在真正写入（成功或失败）后才打 syslog，平时静默。
import ops
import re

POOL_NAME  = "p2p_pool"
SECTION_ID = "0"
DIALER     = "Dialer1"
SECTION_RE = re.compile(r"section\s+%s\s+(\d+\.\d+\.\d+\.\d+)" % SECTION_ID,
                        re.IGNORECASE)
DIALER_RE  = re.compile(r"%s\s+(\d+\.\d+\.\d+\.\d+)" % DIALER)


def get_dialer_ip(_ops, handle):
    out, _, _ = _ops.cli.execute(handle, "display ip interface brief %s" % DIALER)
    m = DIALER_RE.search(out or "")
    ip = m.group(1) if m else None
    return None if ip in (None, "0.0.0.0") else ip


def get_section_ip(_ops, handle):
    out, _, _ = _ops.cli.execute(handle, "display nat address-group name %s" % POOL_NAME)
    m = SECTION_RE.search(out or "")
    return m.group(1) if m else None


def write_section(_ops, handle, new_ip):
    """Rewrite the section, then read it back. Returns (ok, failed_cmd_or_None)."""
    cmds = ["system-view",
            "nat address-group %s" % POOL_NAME,
            "undo section %s" % SECTION_ID,
            "section %s %s %s" % (SECTION_ID, new_ip, new_ip),
            "return"]
    for cmd in cmds:
        _, _, desc = _ops.cli.execute(handle, cmd)
        if desc != "Success" and not cmd.startswith("undo section"):
            _ops.cli.execute(handle, "return")
            return False, "%s (%s)" % (cmd, desc)
    return get_section_ip(_ops, handle) == new_ip, None


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

        ok, failed = write_section(_ops, handle, new_ip)
        if ok:
            _ops.syslog("update_poll done: %s %s -> %s" % (DIALER, current, new_ip))
        else:
            _ops.syslog("update_poll FAILED: %s %s -> %s, failed at %s" %
                        (DIALER, current, new_ip, failed or "verify"))
    finally:
        _ops.cli.close(handle)
    return 0
