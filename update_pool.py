# -*- coding: utf-8 -*-
# 主版本：仅订阅 RM / RM_ADD_DEFAULTRT 事件（PPPoE 上线秒级响应）
# 定时轮询的逻辑已拆到 update_pool_polling.py，两个脚本独立部署，
# compare-then-update 幂等，互不干扰。
# 事件只当触发器用：新 IP 直接读 Dialer1 接口，不依赖 _para_Nexthop，
# 默认路由写不写下一跳都能用。
# 调试输出：display logbuffer | include update_pool
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
    cond = [
        {'name': 'Ifname', 'op': 'eq', 'value': DIALER},
        {'name': 'AfType', 'op': 'eq', 'value': 'IPv4'},
    ]
    _ops.event.subscribe("rtAdd", "RM", "RM_ADD_DEFAULTRT", cond)
    _ops.correlate("rtAdd")
    return 0


def ops_execute(_ops):
    hint, _ = _ops.environment.get("_para_Nexthop")
    _ops.set_model_type("YANG")
    handle, err = _ops.cli.open()
    if handle is None:
        _ops.syslog("update_pool cli.open failed: err=%r" % (err,))
        return 0

    try:
        new_ip = get_dialer_ip(_ops, handle)
        current = get_section_ip(_ops, handle)
        _ops.syslog("update_pool start: route nexthop=%r, %s=%r, section=%r" %
                    (hint, DIALER, new_ip, current))
        if not new_ip or current == new_ip:
            return 0

        ok, failed = write_section(_ops, handle, new_ip)
        if ok:
            _ops.syslog("update_pool done: %s %s -> %s" % (DIALER, current, new_ip))
        else:
            _ops.syslog("update_pool FAILED: %s %s -> %s, failed at %s" %
                        (DIALER, current, new_ip, failed or "verify"))
    finally:
        _ops.cli.close(handle)
    return 0
